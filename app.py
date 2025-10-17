# ---------- app.py ----------
import os
import io
import json
import time
import hashlib
import zipfile
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import xml.etree.ElementTree as ET
import trimesh
import numpy as np

app = FastAPI(title="3D Print – Fixed Profiles Slicing API")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# --- Slicer / Env ---
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp")
os.environ.setdefault(
    "LD_LIBRARY_PATH",
    "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH", "")
)
os.environ.setdefault(
    "PATH",
    "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH", "")
)

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
XVFB = shutil.which("xvfb-run") or "xvfb-run"

# -----------------------------------------------------------------------------#
#                                     Utils                                    #
# -----------------------------------------------------------------------------#
def sha256_of_bytes(buf: bytes) -> str:
    h = hashlib.sha256(); h.update(buf); return h.hexdigest()

def slicer_exists() -> bool:
    return (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK)) or \
           (shutil.which(os.path.basename(SLICER_BIN)) is not None)

def run(cmd: List[str], timeout: int = 900) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def np_to_list(x) -> List[float]:
    return list(map(float, x))

def find_profiles() -> Dict[str, List[str]]:
    """Finde Profil-Dateien im Container (/app/profiles)."""
    base = Path("/app/profiles")
    res = {"printer": [], "process": [], "filament": []}
    for key, sub in [("printer","printers"), ("process","process"), ("filament","filaments")]:
        d = base / sub
        if d.exists():
            res[key] = sorted(str(p) for p in d.glob("*.json"))
    return res

def must_pick(profile_paths: List[str], label: str, wanted_name: Optional[str]) -> str:
    """Wähle ein Profil. Wenn wanted_name übergeben wurde, muss es matchen."""
    if wanted_name:
        for p in profile_paths:
            n = Path(p).name
            if n == wanted_name or wanted_name in Path(p).stem:
                return p
        raise HTTPException(400, f"{label}-Profil '{wanted_name}' nicht gefunden.")
    if not profile_paths:
        raise HTTPException(500, f"Kein {label}-Profil vorhanden. Bitte /app/profiles/{label}s/*.json bereitstellen.")
    return profile_paths[0]

def pick_filament_for_material(filament_paths: List[str], material: str) -> Optional[str]:
    """Nimmt das Filament-Profil, dessen Dateiname den Materialstring enthält (PLA/PETG/ASA/PC)."""
    if not filament_paths:
        return None
    m = (material or "").strip().upper()
    for p in filament_paths:
        if m in Path(p).name.upper():
            return p
    return filament_paths[0]

def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))

def save_json(p: Path, obj: dict):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2))

def percent_from_frac(x: float) -> Optional[int]:
    """0..1 → 0..100 (gerundet, begrenzt)."""
    try:
        f = float(x)
    except Exception:
        return None
    f = max(0.0, min(1.0, f))
    return int(round(f * 100))

def _parse_infill_to_fraction(infill_val: Optional[str], infill_pct_val: Optional[str]) -> float:
    """
    Nimmt entweder 'infill' (Bruch 0..1) oder 'infill_pct' (0..100).
    Akzeptiert deutsches Komma. Clampt am Ende in [0,1].
    """
    def _to_float(s: Optional[str]) -> Optional[float]:
        if s is None:
            return None
        s = str(s).strip().replace(',', '.')
        if s == '':
            return None
        return float(s)

    # Prozent gewinnt
    pct = _to_float(infill_pct_val)
    if pct is not None:
        return max(0.0, min(1.0, pct / 100.0))

    # sonst Bruch
    frac = _to_float(infill_val)
    if frac is None:
        raise ValueError("Kein gültiger Infill-Wert.")
    if frac > 1.0:  # „35“ als 35%
        frac = frac / 100.0
    return max(0.0, min(1.0, frac))

def _ensure_list(v, default_first: float) -> List[float]:
    """Orca speichert manche Felder als Einzelwert oder Liste. Wir wollen immer eine 1-Element-Liste."""
    if isinstance(v, list):
        return v if v else [default_first]
    if v is None:
        return [default_first]
    return [float(v)]

def harden_printer_profile(src_path: str, workdir: Path) -> Tuple[str, dict]:
    """Printer-Profil minimal härten (z. B. nozzle als Liste absichern)."""
    try:
        prn = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Printer-Profil ungültig: {e}")

    # nozzle_diameter als Liste einklappen
    nozzle = prn.get("nozzle_diameter", 0.4)
    prn["nozzle_diameter"] = _ensure_list(nozzle, 0.4)

    out = workdir / "printer_hardened.json"
    save_json(out, prn)
    return str(out), prn

def harden_process_profile(
    src_path: str, workdir: Path,
    *, fill_density_pct: Optional[int] = None,
    printer_json: Optional[dict] = None
) -> str:
    """
    Process-Profil minimal härten + optional Infill-Dichte (in %) überschreiben.
    Entfernt/neutralisiert bekannte Problemfelder.
    """
    try:
        proc = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Process-Profil ungültig: {e}")

    def set_if_missing(obj, key, val):
        if key not in obj or obj[key] in (None, "", -1):
            obj[key] = val

    # Robustheit:
    # Absolute E erzwingen, Layer-G92 nicht nötig wenn absolut
    proc["use_relative_e_distances"] = False
    proc["before_layer_gcode"] = (proc.get("before_layer_gcode") or "").replace("G92 E0", "")
    proc["layer_gcode"] = (proc.get("layer_gcode") or "").replace("G92 E0", "")

    # Negative Defaults neutralisieren
    for k in ("tree_support_wall_count", "raft_first_layer_expansion"):
        v = proc.get(k, None)
        if isinstance(v, (int, float)) and v < 0:
            proc[k] = 0

    # Infill-Dichte setzen
    if fill_density_pct is not None:
        proc["fill_density"] = int(max(0, min(100, fill_density_pct)))

    # Mit Printer kompatible Felder „entschärfen“, falls nötig
    if printer_json:
        # Beispiel: falls process eine Düse als Skalar hat, in Liste wandeln, Länge an Printer anpassen
        noz_list = _ensure_list(printer_json.get("nozzle_diameter", 0.4), 0.4)
        # (je nach Orca-Version sind hier keine weiteren Felder nötig; wir lassen es simpel)

    out = workdir / "process_hardened.json"
    save_json(out, proc)
    return str(out)

def harden_filament_profile(src_path: str, workdir: Path, *, material: str) -> str:
    """Filament-Profil ggf. minimal korrigieren (keine harten Eingriffe nötig)."""
    try:
        fila = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Filament-Profil ungültig: {e}")

    # Optional könnte man hier material-/durchmesser-Felder überprüfen/normalisieren.
    out = workdir / "filament_hardened.json"
    save_json(out, fila)
    return str(out)

# -----------------------------------------------------------------------------#
#                               Analyse (STL/3MF)                              #
# -----------------------------------------------------------------------------#
def analyze_stl(data: bytes) -> Dict[str, Any]:
    mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(tuple(
            g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)
        ))
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("STL enthält keine gültige Geometrie.")

    mesh.remove_unreferenced_vertices()
    mesh.process(validate=True)

    tri_count = int(mesh.faces.shape[0])
    volume_mm3 = float(abs(mesh.volume)) if mesh.is_volume else None
    area_mm2 = float(mesh.area) if mesh.area is not None else None
    bounds = mesh.bounds
    size = bounds[1] - bounds[0]

    return {
        "mesh_is_watertight": bool(mesh.is_watertight),
        "triangles": tri_count,
        "volume_mm3": volume_mm3,
        "surface_area_mm2": area_mm2,
        "bbox_min_mm": np_to_list(bounds[0]),
        "bbox_max_mm": np_to_list(bounds[1]),
        "bbox_size_mm": np_to_list(size),
        "units_assumed": "mm",
    }

def analyze_3mf(data: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        namelist = zf.namelist()
        model_xml_name = next((c for c in ("3D/3dmodel.model", "3d/3dmodel.model", "3D/Model.model") if c in namelist), None)

        res: Dict[str, Any] = {
            "zip_entries": len(namelist),
            "has_model_xml": bool(model_xml_name),
            "objects": [],
            "units": None,
            "triangles_total": 0,
            "objects_count": 0
        }
        if not model_xml_name:
            return res

        xml_bytes = zf.read(model_xml_name)
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return res

        ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        units = root.attrib.get("unit") or root.attrib.get("units")
        res["units"] = units or "mm"

        objects = root.findall(".//m:object", ns) if ns else root.findall(".//object")
        res["objects_count"] = len(objects)

        tri_total = 0; out_objects = []
        for obj in objects:
            oid = obj.attrib.get("id"); typ = obj.attrib.get("type")
            mesh_elem = obj.find("m:mesh", ns) if ns else obj.find("mesh")
            if mesh_elem is None:
                out_objects.append({"id": oid, "type": typ, "triangles": 0}); continue

            tris = mesh_elem.find("m:triangles", ns) if ns else mesh_elem.find("triangles")
            tri_count = len(tris.findall("m:triangle", ns) if ns else tris.findall("triangle")) if tris is not None else 0
            tri_total += tri_count

            bbox = None
            verts_elem = mesh_elem.find("m:vertices", ns) if ns else mesh_elem.find("vertices")
            if verts_elem is not None:
                vs = (verts_elem.findall("m:vertex", ns) if ns else verts_elem.findall("vertex"))
                coords = []
                for v in vs:
                    try:
                        x = float(v.attrib.get("x", "0")); y = float(v.attrib.get("y", "0")); z = float(v.attrib.get("z", "0"))
                        coords.append((x, y, z))
                    except Exception:
                        pass
                if coords:
                    arr = np.array(coords, dtype=float)
                    vmin = arr.min(axis=0); vmax = arr.max(axis=0)
                    bbox = {"bbox_min": np_to_list(vmin), "bbox_max": np_to_list(vmax), "bbox_size": np_to_list(vmax - vmin)}

            out_objects.append({"id": oid, "type": typ, "triangles": tri_count, **({"bbox": bbox} if bbox else {})})

        res["triangles_total"] = tri_total
        res["objects"] = out_objects
        return res

def parse_slicedata_folder(folder: Path) -> Dict[str, Any]:
    out = {"duration_s": None, "filament_mm": None, "filament_g": None, "files": []}
    for jf in sorted(folder.glob("*.json")):
        try:
            j = json.loads(jf.read_text()[:2_000_000])
            out["duration_s"] = out["duration_s"] or j.get("print_time_sec") or j.get("time_sec")
            out["filament_mm"] = out["filament_mm"] or j.get("filament_used_mm") or j.get("filament_mm")
            out["filament_g"]  = out["filament_g"]  or j.get("filament_used_g")  or j.get("filament_g")
            out["files"].append(jf.name)
        except Exception:
            pass
    return out

# -----------------------------------------------------------------------------#
#                                UI / Health / Env                             #
# -----------------------------------------------------------------------------#
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<meta charset="utf-8">
<title>3D Print – Fixed Profiles Slicing</title>
<style>
  :root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
  body{font-family:system-ui,Segoe UI,Arial;margin:24px;line-height:1.45;color:var(--fg)}
  h1{margin:0 0 16px;font-size:22px}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px;background:#fff}
  button,input[type=submit]{padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#111827;color:#fff;cursor:pointer}
  button.secondary{background:#fff;color:#111827}
  label{font-weight:600;margin-right:8px}
  input[type=number],select,input[type=text]{padding:8px 10px;border:1px solid var(--line);border-radius:10px}
  pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:360px;overflow:auto}
  small{color:var(--muted)}
</style>

<h1>3D Print – Fixed Profiles Slicing</h1>
<div class="grid">
  <div class="card">
    <button class="secondary" onclick="openDocs()">Swagger (API-Doku)</button>
    <button onclick="hit('/health','#out')">Health</button>
    <button onclick="hit('/slicer_env','#out')">Slicer-Env</button>
  </div>

  <div class="card">
    <h3>Upload analysieren (/analyze_upload)</h3>
    <form onsubmit="return sendAnalyze(event)">
      <input type="file" name="file" accept=".stl,.3mf" required>
      <input type="submit" value="Analysieren">
      <div><small>bis 50 MB</small></div>
    </form>
  </div>

  <div class="card">
    <h3>Druckzeit mit festen Profilen (/estimate_time)</h3>
    <form onsubmit="return sendTime(event)">
      <input type="file" name="file" accept=".stl,.3mf" required>
      <div style="margin:8px 0">
        <label>Material</label>
        <select name="material">
          <option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option>
        </select>
        <label>Infill</label><input name="infill" type="text" value="0.35" style="width:120px" placeholder="0.35 oder 35%">
      </div>
      <div style="margin:8px 0">
        <label>Printer-Profil</label><input name="printer_profile" placeholder="optional: Dateiname">
      </div>
      <div style="margin:8px 0">
        <label>Process-Profil</label><input name="process_profile" placeholder="optional: Dateiname">
      </div>
      <div style="margin:8px 0">
        <label>Filament-Profil</label><input name="filament_profile" placeholder="optional: Dateiname">
      </div>
      <input type="hidden" name="debug" value="1">
      <input type="submit" value="Zeit ermitteln">
      <div><small>Profile kommen aus /app/profiles. Infill & Material aus Kundeneingabe.</small></div>
    </form>
  </div>
</div>

<pre id="out">Output erscheint hier …</pre>

<script>
const base = location.origin;
async function hit(path, sel){
  const out = document.querySelector(sel || '#out');
  out.textContent = 'Lade ' + path + ' …';
  try{
    const r = await fetch(base + path);
    const isJson = (r.headers.get('content-type')||'').includes('json');
    out.textContent = isJson ? JSON.stringify(await r.json(), null, 2) : await r.text();
  }catch(e){ out.textContent = 'Fehler: ' + e; }
}
async function sendAnalyze(e){
  e.preventDefault();
  const fd = new FormData(e.target);
  const out = document.querySelector('#out'); out.textContent='Analysiere …';
  try{ const r = await fetch(base+'/analyze_upload',{method:'POST',body:fd});
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}
async function sendTime(e){
  e.preventDefault();
  const fd = new FormData(e.target);
  const out = document.querySelector('#out'); out.textContent='Slicen …';
  try{ const r = await fetch(base+'/estimate_time',{method:'POST',body:fd});
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}
function openDocs(){ window.open(base + '/docs', '_blank'); }
</script>
"""

@app.get("/health", response_class=JSONResponse)
def health():
    return {
        "ok": True,
        "slicer_bin": SLICER_BIN,
        "slicer_present": slicer_exists(),
        "profiles": find_profiles()
    }

@app.get("/slicer_env", response_class=JSONResponse)
def slicer_env():
    which = shutil.which(os.path.basename(SLICER_BIN)) or SLICER_BIN
    info = {"ok": True, "bin_exists": slicer_exists(), "which": which}
    try:
        code, out, err = run([which, "--help"], timeout=8)
        info["return_code"] = code
        info["help_snippet"] = (out or err or "")[:1000]
    except Exception as e:
        info["return_code"] = None
        info["help_snippet"] = f"(help not available) {e}"
    return info

# ----------------------------- Upload Analyse ---------------------------------
@app.post("/analyze_upload", response_class=JSONResponse)
async def analyze_upload(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()
    if not (filename.endswith(".stl") or filename.endswith(".3mf")):
        raise HTTPException(400, "Nur STL- oder 3MF-Dateien werden akzeptiert.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Datei > {MAX_FILE_BYTES // (1024*1024)} MB.")

    base_meta: Dict[str, Any] = {
        "ok": True,
        "filename": file.filename,
        "filesize_bytes": len(data),
        "sha256": sha256_of_bytes(data),
        "filetype": "stl" if filename.endswith(".stl") else "3mf",
        "generated_at": int(time.time())
    }

    try:
        if filename.endswith(".stl"):
            stl_meta = analyze_stl(data)
            return {**base_meta, "stl": stl_meta}
        else:
            info = analyze_3mf(data)
            return {**base_meta, "three_mf": info}
    except Exception as e:
        raise HTTPException(500, f"Analyse fehlgeschlagen: {e}")

# ----------------------------- Slicing/Timing --------------------------------
@app.post("/estimate_time", response_class=JSONResponse)
async def estimate_time(
    file: UploadFile = File(...),
    printer_profile: str = Form(None),
    process_profile: str = Form(None),
    filament_profile: str = Form(None),
    material: str = Form("PLA"),  # PLA|PETG|ASA|PC
    infill: str = Form("0.35"),   # String, damit „0,35“ akzeptiert wird
    infill_pct: str = Form(None), # optional 0..100
    debug: int = Form(0),
):
    if not slicer_exists():
        raise HTTPException(500, "OrcaSlicer CLI nicht verfügbar.")

    filename = (file.filename or "").lower()
    if not (filename.endswith(".stl") or filename.endswith(".3mf")):
        raise HTTPException(400, "Nur STL- oder 3MF-Dateien werden akzeptiert.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Datei > {MAX_FILE_BYTES // (1024*1024)} MB.")

    # Infill robuster parsen
    try:
        infill_frac = _parse_infill_to_fraction(infill, infill_pct)
    except Exception as e:
        raise HTTPException(400, f"Ungültiger Infill-Wert: {e}")
    infill_pct_int = percent_from_frac(infill_frac)
    if infill_pct_int is None:
        raise HTTPException(400, "Ungültiger Infill-Wert.")

    prof = find_profiles()
    pick_printer_path  = must_pick(prof["printer"],  "printer",  printer_profile)
    pick_process_path  = must_pick(prof["process"],  "process",  process_profile)

    if filament_profile:
        pick_filament_path = must_pick(prof["filament"], "filament", filament_profile)
    else:
        auto_fil = pick_filament_for_material(prof["filament"], material)
        if not auto_fil:
            raise HTTPException(500, "Kein Filament-Profil vorhanden. Bitte /app/profiles/filaments/*.json bereitstellen.")
        pick_filament_path = auto_fil

    work = Path(tempfile.mkdtemp(prefix="fixedp_"))
    attempts: List[Dict[str, Any]] = []
    last_cmd: Optional[str] = None
    try:
        is_3mf = filename.endswith(".3mf")
        inp = work / ("input.3mf" if is_3mf else "input.stl")
        inp.write_bytes(data)
        out_meta = work / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
        out_3mf  = work / "out.3mf"
        datadir  = work / "cfg"; datadir.mkdir(parents=True, exist_ok=True)

        # Hardened Kopien erstellen
        printer_hard_path, printer_json = harden_printer_profile(pick_printer_path, work)
        process_hard_path = harden_process_profile(
            pick_process_path, work,
            fill_density_pct=infill_pct_int,
            printer_json=printer_json
        )
        filament_hard_path = harden_filament_profile(pick_filament_path, work, material=material)

        # Strategy 1: Printer + Process (gehärtet)
        settings_chain_1 = [str(printer_hard_path), str(process_hard_path)]
        cmd1 = [
            SLICER_BIN, "--debug", "4",
            "--datadir", str(datadir),
            "--load-settings", ";".join(settings_chain_1),
            "--load-filaments", str(filament_hard_path),
            "--export-slicedata", str(out_meta),
            inp.as_posix(),
            "--slice", "0", "--export-3mf", str(out_3mf)
        ]
        attempts.append({"tag": "try-1", "cmd": " ".join([XVFB, "-a", *cmd1])})
        code, out, err = run([XVFB, "-a"] + cmd1, timeout=900)
        if code == 0:
            last_cmd = " ".join([XVFB, "-a", *cmd1])
        else:
            attempts[-1]["stderr_tail"] = (err or out)[-800:]

        # Strategy 2: Printer + Process(hard) + (falls nötig) neutraler Arrange-Versuch
        if last_cmd is None:
            cmd2 = [
                SLICER_BIN, "--debug", "4",
                "--datadir", str(datadir),
                "--load-settings", ";".join(settings_chain_1),
                "--load-filaments", str(filament_hard_path),
                "--export-slicedata", str(out_meta),
                inp.as_posix(),
                "--slice", "0", "--export-3mf", str(out_3mf)
            ]
            attempts.append({"tag": "try-2", "cmd": " ".join([XVFB, "-a", *cmd2])})
            code2, out2, err2 = run([XVFB, "-a"] + cmd2, timeout=900)
            if code2 == 0:
                last_cmd = " ".join([XVFB, "-a", *cmd2])
            else:
                attempts[-1]["stderr_tail"] = (err2 or out2)[-800:]

        # Strategy 3 (synthetic): Printer + Process(synthetic)
        if last_cmd is None:
            proc_syn = work / "process_synthetic.json"
            try:
                ph = load_json(Path(process_hard_path))
            except Exception as e:
                raise HTTPException(500, f"Process lädt nicht: {e}")
            # aggressive Neutralisierung einzelner Felder, die oft inkompatibel sind:
            for k in ("before_layer_gcode", "layer_gcode"):
                if k in ph:
                    ph[k] = ""
            # sicherstellen, dass fill_density integer 0..100 ist
            try:
                ph["fill_density"] = int(max(0, min(100, ph.get("fill_density", infill_pct_int))))
            except Exception:
                ph["fill_density"] = int(infill_pct_int)
            save_json(proc_syn, ph)

            settings_chain_last = [str(printer_hard_path), str(proc_syn)]
            cmd3 = [
                SLICER_BIN, "--debug", "4",
                "--datadir", str(datadir),
                "--load-settings", ";".join(settings_chain_last),
                "--load-filaments", str(filament_hard_path),
                "--export-slicedata", str(out_meta),
                inp.as_posix(),
                "--slice", "0", "--export-3mf", str(out_3mf)
            ]
            attempts.append({"tag": "try-3", "cmd": " ".join([XVFB, "-a", *cmd3])})
            code3, out3, err3 = run([XVFB, "-a"] + cmd3, timeout=900)
            if code3 == 0:
                last_cmd = " ".join([XVFB, "-a", *cmd3])
            else:
                attempts[-1]["stderr_tail"] = (err3 or out3)[-800:]

        if last_cmd is None:
            raise HTTPException(
                500,
                detail={
                    "message": "Slicing fehlgeschlagen (alle Strategien).",
                    "last_cmd": attempts[-1]["cmd"] if attempts else None,
                    "attempts": attempts if debug else "set debug=1 to see attempts"
                }
            )

        meta = parse_slicedata_folder(out_meta)
        if not meta.get("duration_s"):
            raise HTTPException(
                500,
                detail={
                    "message": "Keine Druckzeit in Slicedata gefunden.",
                    "last_cmd": last_cmd,
                    "attempts": attempts if debug else "set debug=1 to see attempts"
                }
            )

        return {
            "ok": True,
            "input_ext": ".3mf" if is_3mf else ".stl",
            "profiles_used": {
                "printer": Path(pick_printer_path).name,
                "process": Path(pick_process_path).name,
                "filament": Path(pick_filament_path).name
            },
            "material": material.upper(),
            "infill_pct": infill_pct_int,
            "duration_s": float(meta["duration_s"]),
            "filament_mm": meta.get("filament_mm"),
            "filament_g": meta.get("filament_g"),
            **({"attempts": attempts} if debug else {})
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            500,
            detail={
                "message": str(e),
                "last_cmd": last_cmd,
                "note": "siehe Render-Logs für vollen Stacktrace"
            }
        )
    finally:
        try: shutil.rmtree(work, ignore_errors=True)
        except Exception: pass
