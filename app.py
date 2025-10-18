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

# ------------------------------------------------------------------------------
# FastAPI Grundgerüst
# ------------------------------------------------------------------------------
app = FastAPI(title="3D Print – Fixed Profiles Slicing API")

# ⚠️ CORS aktuell offen – später auf deine Domain(s) beschränken
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Slicer / Umgebung
# ------------------------------------------------------------------------------
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

XVFB = shutil.which("xvfb-run") or "xvfb-run"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# ------------------------------------------------------------------------------
# Hilfsfunktionen
# ------------------------------------------------------------------------------
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
    """Suche Profil-Dateien im Container (/app/profiles)."""
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

def parse_infill_to_pct(infill_str_or_float: Any) -> int:
    """
    Akzeptiert 0.35, "0.35", "0,35", "35", "35%" → 35
    """
    if infill_str_or_float is None:
        return 35
    s = str(infill_str_or_float).strip()
    s = s.replace("%", "").strip()
    s = s.replace(",", ".").strip()
    try:
        v = float(s)
    except Exception:
        raise HTTPException(400, f"Ungültiger Infill-Wert: {infill_str_or_float!r}")
    if v <= 1.0:  # 0..1 → Prozent
        v = v * 100.0
    v = max(0.0, min(100.0, v))
    return int(round(v))

# ------------------------------------------------------------------------------
# Profile härten / normalisieren
# ------------------------------------------------------------------------------
def harden_printer_profile(src_path: str, workdir: Path) -> Tuple[str, dict, str]:
    """
    Normalisiert das Printer-Profil minimal und liefert auch den 'name' zurück.
    """
    try:
        j = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Printer-Profil ungültig: {e}")

    j["type"] = "machine"

    # ✅ Orca erwartet STRINGS in nozzle_diameter
    nd = j.get("nozzle_diameter")
    if isinstance(nd, list):
        j["nozzle_diameter"] = [str(x) for x in nd]
    elif nd is not None:
        j["nozzle_diameter"] = [str(nd)]

    if not j.get("name"):
        j["name"] = Path(src_path).stem

    # optional: version als String absichern
    if "version" in j and not isinstance(j["version"], str):
        j["version"] = str(j["version"])

    out = workdir / "printer_hardened.json"
    save_json(out, j)
    return str(out), j, str(j.get("name", Path(src_path).stem))

def harden_process_profile(src_path: str, workdir: Path, *, infill_pct: int, printer_name: str) -> Tuple[str, dict]:
    """
    Process-Profil härten:
      - type=process, version als String
      - Infill als sparse_infill_density: "NN%"
      - G92 E0 aus before_layer_gcode entfernen
      - compatible_printers maximal öffnen (inkl. "*") + Condition neutralisieren
    """
    try:
        p = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Process-Profil ungültig: {e}")

    p["type"] = "process"
    if "version" in p and not isinstance(p["version"], str):
        p["version"] = str(p["version"])
    else:
        p.setdefault("version", "1")

    p.pop("fill_density", None)
    p["sparse_infill_density"] = f"{int(max(0, min(100, infill_pct)))}%"

    blg = (p.get("before_layer_gcode") or "")
    p["before_layer_gcode"] = blg.replace("G92 E0", "")

    base_name = printer_name.split(" (")[0].strip()
    compat = p.get("compatible_printers")
    if not isinstance(compat, list):
        compat = []
    compat = list({*(x for x in compat if isinstance(x, str)), printer_name, base_name, "*"})
    p["compatible_printers"] = compat
    # ❗ Wichtig: Bedingung leeren, damit kein interner Filter greift
    p["compatible_printers_condition"] = ""

    out = workdir / "process_hardened.json"
    save_json(out, p)
    return str(out), p

def harden_filament_profile(src_path: str, workdir: Path) -> Tuple[str, dict]:
    """
    Filament-Profil normalisieren, global kompatibel + Condition neutralisieren.
    """
    try:
        f = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Filament-Profil ungültig: {e}")
    f["type"] = "filament"
    if "version" in f and not isinstance(f["version"], str):
        f["version"] = str(f["version"])

    f["compatible_printers"] = ["*"]
    f["compatible_printers_condition"] = ""

    out = workdir / "filament_hardened.json"
    save_json(out, f)
    return str(out), f

# ------------------------------------------------------------------------------
# Analyse (STL/3MF)
# ------------------------------------------------------------------------------
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

# ------------------------------------------------------------------------------
# UI / Health / Env
# ------------------------------------------------------------------------------
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
        <label>Infill</label><input name="infill" type="text" value="0.35" style="width:120px">
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
      <input type="submit" value="Zeit ermitteln">
      <div><small>Es werden ausschließlich eure Profile geladen. Infill & Material kommen aus der Kundeneingabe.</small></div>
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

# ------------------------------------------------------------------------------
# Upload-Analyse
# ------------------------------------------------------------------------------
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

# ------------------------------------------------------------------------------
# INI-Fallback: aus JSON-Profilen eine flache .ini erzeugen
# ------------------------------------------------------------------------------
def write_merged_ini(printer: dict, process: dict, filament: dict, dst: Path) -> Path:
    """
    Erzeugt eine minimal-kompatible .ini (Prusa/Orca Stil), die
    direkt via `--load` eingelesen wird. Dadurch entfällt jede
    Preset-Kompatibilitätsprüfung.
    """
    bed_poly = printer.get("printable_area") or ["0x0","250x0","250x250","0x250"]
    if isinstance(bed_poly, list):
        bed_shape = ",".join(bed_poly)
    else:
        bed_shape = "0x0,250x0,250x250,0x250"

    nozzle = printer.get("nozzle_diameter") or ["0.4"]
    if isinstance(nozzle, list):
        nozzle_str = ",".join(str(x) for x in nozzle)
    else:
        nozzle_str = str(nozzle)

    layer_height = process.get("layer_height") or "0.2"
    first_layer = process.get("initial_layer_height") or process.get("first_layer_height") or "0.3"
    infill = process.get("sparse_infill_density") or "20%"
    if isinstance(infill, str) and infill.endswith("%"):
        infill = infill[:-1]
    try:
        infill_num = float(str(infill).replace(",", "."))
    except Exception:
        infill_num = 20.0

    noz = (filament.get("nozzle_temperature") or ["200"])
    noz_first = (filament.get("nozzle_temperature_initial_layer") or [noz[0] if isinstance(noz, list) and noz else "205"])
    bed_temp = filament.get("bed_temperature") or ["0"]

    def first_val(x, default="0"):
        if isinstance(x, list) and x:
            return str(x[0])
        if isinstance(x, (int, float)):
            return str(x)
        if isinstance(x, str):
            return x
        return default

    ini_lines = [
        # --- Printer ---
        f"bed_shape = {bed_shape}",
        f"nozzle_diameter = {nozzle_str}",
        # --- Heights / Infill (Process) ---
        f"layer_height = {layer_height}",
        f"first_layer_height = {first_layer}",
        f"fill_density = {infill_num}",            # Prozent (ohne %)
        # --- Filament basics ---
        f"temperature = {first_val(noz, '200')}",
        f"first_layer_temperature = {first_val(noz_first, '205')}",
        f"bed_temperature = {first_val(bed_temp, '0')}",
        f"first_layer_bed_temperature = {first_val(bed_temp, '0')}",
        # --- Misc ---
        f"use_relative_e_distances = {process.get('use_relative_e_distances','0')}",
        f"perimeter_extrusion_width = {process.get('line_width','0.45')}",
        f"external_perimeter_extrusion_width = {process.get('line_width','0.45')}",
        f"infill_extrusion_width = {process.get('line_width','0.45')}",
    ]

    dst.write_text("\n".join(ini_lines) + "\n", encoding="utf-8")
    return dst

# ------------------------------------------------------------------------------
# Slicing / Druckzeit
# ------------------------------------------------------------------------------
@app.post("/estimate_time", response_class=JSONResponse)
async def estimate_time(
    file: UploadFile = File(...),
    printer_profile: str = Form(None),
    process_profile: str = Form(None),
    filament_profile: str = Form(None),
    material: str = Form("PLA"),  # PLA|PETG|ASA|PC
    infill: str = Form("0.35"),   # "0.35" (35) oder "35" oder "35%"
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

    # Infill robust parsen
    pct = parse_infill_to_pct(infill)

    # Profile finden/auswählen
    prof = find_profiles()
    pick_printer  = must_pick(prof["printer"],  "printer",  printer_profile)
    pick_process0 = must_pick(prof["process"],  "process",  process_profile)

    if filament_profile:
        pick_filament = must_pick(prof["filament"], "filament", filament_profile)
    else:
        auto_fil = pick_filament_for_material(prof["filament"], material)
        if not auto_fil:
            raise HTTPException(500, "Kein Filament-Profil vorhanden. Bitte /app/profiles/filaments/*.json bereitstellen.")
        pick_filament = auto_fil

    work = Path(tempfile.mkdtemp(prefix="fixedp_"))
    try:
        is_3mf = filename.endswith(".3mf")
        inp = work / ("input.3mf" if is_3mf else "input.stl")
        inp.write_bytes(data)
        out_meta = work / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
        out_3mf  = work / "out.3mf"
        datadir  = work / "cfg"; datadir.mkdir(parents=True, exist_ok=True)

        # Profile härten – Reihenfolge wichtig!
        hardened_printer, printer_json, printer_name = harden_printer_profile(pick_printer, work)
        hardened_process, process_json = harden_process_profile(pick_process0, work, infill_pct=pct, printer_name=printer_name)
        hardened_filament, filament_json = harden_filament_profile(pick_filament, work)

        # Synthetic-Fallback-Process (nur Infill + Kompatibilität)
        process_synth = {
            "type": "process",
            "version": "1",
            "from": "user",
            "name": "synthetic_infill_only",
            "sparse_infill_density": f"{pct}%",
            "before_layer_gcode": "",
            "compatible_printers": list({printer_name, printer_name.split(" (")[0].strip(), "*"}),
            "compatible_printers_condition": ""
        }
        synth_path = work / "process_synthetic.json"
        save_json(synth_path, process_synth)

        # ---------- CLI-Kommandos ----------
        # Ein --load-settings mit Semikolonliste, --slice 1 (keine --orient/--arrange)
        def base_cmd(load_proc: str) -> List[str]:
            load_list = f"{hardened_printer};{load_proc}"
            return [
                SLICER_BIN,
                "--debug", str(int(debug) if isinstance(debug, int) else 0),
                "--datadir", str(datadir),
                "--load-settings", load_list,
                "--load-filaments", str(hardened_filament),
                "--export-slicedata", str(out_meta),
                inp.as_posix(),
                "--slice", "1",
                "--export-3mf", str(out_3mf),
            ]

        attempts = []

        # Versuch 1: gehärtetes Process-Profil
        cmd1 = [XVFB, "-a"] + base_cmd(hardened_process)
        code, out, err = run(cmd1, timeout=900)
        attempts.append({"tag": "try-1", "cmd": " ".join(cmd1), "stderr_tail": (err or out)[-800:]})

        # Versuch 2: export-slicedata ans Ende
        if code != 0:
            cmd2 = [XVFB, "-a"] + [
                SLICER_BIN, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                "--datadir", str(datadir),
                "--load-settings", f"{hardened_printer};{hardened_process}",
                "--load-filaments", str(hardened_filament),
                inp.as_posix(),
                "--slice", "1",
                "--export-3mf", str(out_3mf),
                "--export-slicedata", str(out_meta),
            ]
            code2, out2, err2 = run(cmd2, timeout=900)
            attempts.append({"tag": "try-2", "cmd": " ".join(cmd2), "stderr_tail": (err2 or out2)[-800:]})
            code, out, err = code2, out2, err2

        # Versuch 3: Synthetic Process
        if code != 0:
            cmd3 = [XVFB, "-a"] + base_cmd(str(synth_path))
            code3, out3, err3 = run(cmd3, timeout=900)
            attempts.append({"tag": "try-3", "cmd": " ".join(cmd3), "stderr_tail": (err3 or out3)[-800:]})
            code, out, err = code3, out3, err3

        # Versuch 4: getrennte --load-settings (manche Builds mögen das)
        if code != 0:
            cmd4 = [XVFB, "-a"] + [
                SLICER_BIN, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                "--datadir", str(datadir),
                "--load-settings", str(hardened_printer),
                "--load-settings", str(hardened_process),
                "--load-filaments", str(hardened_filament),
                inp.as_posix(),
                "--slice", "1",
                "--export-3mf", str(out_3mf),
                "--export-slicedata", str(out_meta),
            ]
            code4, out4, err4 = run(cmd4, timeout=900)
            attempts.append({"tag": "try-4", "cmd": " ".join(cmd4), "stderr_tail": (err4 or out4)[-800:]})
            code, out, err = code4, out4, err4

        # Versuch 5: INI-Fallback – umgeht Preset-Checks komplett
        if code != 0:
            merged_ini = work / "merged_fallback.ini"
            write_merged_ini(printer_json, process_json, filament_json, merged_ini)
            cmd5 = [XVFB, "-a"] + [
                SLICER_BIN, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                "--datadir", str(datadir),
                "--load", str(merged_ini),           # << .ini statt Presets
                inp.as_posix(),
                "--slice", "1",
                "--export-3mf", str(out_3mf),
                "--export-slicedata", str(out_meta),
            ]
            code5, out5, err5 = run(cmd5, timeout=900)
            attempts.append({"tag": "try-5-ini", "cmd": " ".join(cmd5), "stderr_tail": (err5 or out5)[-800:]})
            code, out, err = code5, out5, err5

        if code != 0:
            # gehärtete JSONs anhängen (gekürzt) → schnelle Diagnose
            def tail(p: Path, n=1200):
                try:
                    s = p.read_text(encoding="utf-8")
                    return s[:n]
                except Exception:
                    return None

            diag = {
                "message": "Slicing fehlgeschlagen (alle Strategien).",
                "last_cmd": " ".join(attempts[-1]["cmd"].split()) if attempts else None,
                "attempts": attempts,
                "printer_hardened_json": tail(Path(hardened_printer)),
                "process_hardened_json": tail(Path(hardened_process)),
                "filament_hardened_json": tail(Path(hardened_filament)),
                "synthetic_process_json": tail(Path(synth_path)),
            }
            raise HTTPException(500, detail=diag)

        meta = parse_slicedata_folder(out_meta)
        if not meta.get("duration_s"):
            raise HTTPException(500, detail=f"Keine Druckzeit in Slicedata gefunden.")

        return {
            "ok": True,
            "input_ext": ".3mf" if is_3mf else ".stl",
            "profiles_used": {
                "printer": Path(pick_printer).name,
                "process": Path(pick_process0).name,
                "filament": Path(pick_filament).name
            },
            "material": material.upper(),
            "infill_pct": pct,
            "duration_s": float(meta["duration_s"]),
            "filament_mm": meta.get("filament_mm"),
            "filament_g": meta.get("filament_g"),
            "notes": "Gesliced mit festen Profilen (--slice 1). Fallback Try-5 lädt eine zusammengeführte .ini (kein Preset-Check)."
        }
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass
