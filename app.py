from __future__ import annotations

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

# -----------------------------------------------------------------------------
# App & CORS
# -----------------------------------------------------------------------------
app = FastAPI(title="3D Print – Fixed Profiles Slicing API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Env / Binaries
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def sha256_of_bytes(buf: bytes) -> str:
    h = hashlib.sha256()
    h.update(buf)
    return h.hexdigest()

def slicer_exists() -> bool:
    return (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK)) or \
           (shutil.which(os.path.basename(SLICER_BIN)) is not None)

def run_cmd(cmd: List[str], timeout: int = 900) -> Tuple[int, str, str]:
    """Run a command and return (code, stdout, stderr)."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def np_to_list(x) -> List[float]:
    return list(map(float, x))

def find_profiles() -> Dict[str, List[str]]:
    """Find profile files under /app/profiles."""
    base = Path("/app/profiles")
    res = {"printer": [], "process": [], "filament": []}
    for key, sub in [("printer","printers"), ("process","process"), ("filament","filaments")]:
        d = base / sub
        if d.exists():
            res[key] = sorted(str(p) for p in d.glob("*.json"))
    return res

def must_pick(profile_paths: List[str], label: str, wanted_name: Optional[str]) -> str:
    """Pick a profile path. If wanted_name provided, try to match by filename."""
    if wanted_name:
        for p in profile_paths:
            n = Path(p).name
            if n == wanted_name or wanted_name.lower() in Path(p).name.lower():
                return p
        raise HTTPException(400, f"{label}-Profil '{wanted_name}' nicht gefunden.")
    if not profile_paths:
        raise HTTPException(500, f"Kein {label}-Profil vorhanden. Erwartet Dateien unter /app/profiles/{label}s/*.json.")
    return profile_paths[0]

def pick_filament_for_material(filament_paths: List[str], material: str) -> Optional[str]:
    """Pick filament preset whose filename contains material (PLA/PETG/ASA/PC)."""
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

def _collect_orca_result_json(base_dir: Path) -> dict | None:
    """Search result.json recursively (Orca writes details there on failures)."""
    for jf in base_dir.rglob("result.json"):
        try:
            return json.loads(jf.read_text()[:1_000_000])
        except Exception:
            pass
    return None

def _scrub_g92(txt: str) -> str:
    """Remove G92 E0 lines."""
    if not txt:
        return txt
    return "\n".join(
        ln for ln in txt.splitlines()
        if "G92 E0" not in ln and "G92E0" not in ln
    )

def _ensure_g92_per_layer(txt: str) -> str:
    """Ensure G92 E0 appears at least once in the layer hook text."""
    txt = txt or ""
    return txt if ("G92 E0" in txt or "G92E0" in txt) else (txt + ("\n" if txt and not txt.endswith("\n") else "") + "G92 E0")

def parse_infill(infill: Optional[str], infill_pct: Optional[str]) -> int:
    """
    Accept either 'infill' (0..1, "." or ",") or 'infill_pct' (0..100).
    Returns percentage int 0..100.
    """
    if infill is not None and str(infill).strip() != "":
        s = str(infill).strip().replace(",", ".")
        try:
            f = float(s)
        except Exception as e:
            raise HTTPException(400, f"Ungültiger Infill-Wert: {e}")
        f = max(0.0, min(1.0, f))
        return int(round(f * 100))

    if infill_pct is not None and str(infill_pct).strip() != "":
        s = str(infill_pct).strip().replace(",", ".")
        try:
            p = float(s)
        except Exception as e:
            raise HTTPException(400, f"Ungültiger Infill-Prozentwert: {e}")
        p = max(0.0, min(100.0, p))
        return int(round(p))

    # default 35%
    return 35

# -----------------------------------------------------------------------------
# Profile hardening (Process only; Printer stays original)
# -----------------------------------------------------------------------------
def harden_process_profile(src_path: str, workdir: Path, *, fill_density_pct: int | None = None) -> str:
    """
    Process preset: je nach E-Modus (rel./abs.) G92 setzen/entfernen,
    negative Platzhalter neutralisieren, optional fill_density überschreiben.
    """
    try:
        proc = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Process-Profil ungültig: {e}")

    # RELATIVE E -> G92 E0 pro Layer SICHERSTELLEN
    if proc.get("use_relative_e_distances") in (True, 1, "1", "true", "True"):
        changed = False
        for key in ("layer_gcode", "before_layer_gcode", "before_layer_change_gcode"):
            if key in proc:
                newtxt = _ensure_g92_per_layer(proc.get(key) or "")
                if newtxt != proc.get(key, ""):
                    proc[key] = newtxt
                    changed = True
        if not changed:
            # Setze layer_gcode minimal, falls gar nichts vorhanden war
            proc.setdefault("layer_gcode", "G92 E0")
    else:
        # ABSOLUTE E -> G92 E0 in layer hooks ENTFERNEN
        for key in ("layer_gcode", "before_layer_gcode", "before_layer_change_gcode", "toolchange_gcode", "printing_by_object_gcode"):
            if key in proc and proc[key]:
                proc[key] = _scrub_g92(proc[key])

    # Problematische negative Defaults neutralisieren
    for k in ("tree_support_wall_count", "raft_first_layer_expansion"):
        if isinstance(proc.get(k), (int, float)) and proc[k] < 0:
            proc[k] = 0

    # Infill % überschreiben, falls angegeben
    if fill_density_pct is not None:
        proc["fill_density"] = int(max(0, min(100, fill_density_pct)))

    out = workdir / "process_hardened.json"
    save_json(out, proc)
    return str(out)

def harden_filament_profile(src_path: str, workdir: Path, *, material: str) -> str:
    """
    Filament preset: sicherstellen, dass "type" korrekt ist; sonst unverändert.
    """
    try:
        fil = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Filament-Profil ungültig: {e}")

    fil.setdefault("type", "filament")

    out = workdir / "filament_hardened.json"
    save_json(out, fil)
    return str(out)

# -----------------------------------------------------------------------------
# Analyze (STL/3MF)
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# UI / Health / Env
# -----------------------------------------------------------------------------
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
        <small>(0..1 mit Punkt) oder unten als Prozent)</small>
      </div>
      <div style="margin:8px 0">
        <label>Infill %</label><input name="infill_pct" type="text" placeholder="z.B. 35" style="width:120px">
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
        code, out, err = run_cmd([which, "--help"], timeout=8)
        info["return_code"] = code
        info["help_snippet"] = (out or err or "")[:1000]
    except Exception as e:
        info["return_code"] = None
        info["help_snippet"] = f"(help not available) {e}"
    return info

# -----------------------------------------------------------------------------
# Upload Analyse
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Slicing / Estimate Time
# -----------------------------------------------------------------------------
@app.post("/estimate_time", response_class=JSONResponse)
async def estimate_time(
    file: UploadFile = File(...),
    printer_profile: str = Form(None),
    process_profile: str = Form(None),
    filament_profile: str = Form(None),
    material: str = Form("PLA"),        # PLA|PETG|ASA|PC
    infill: str = Form(None),           # 0..1 (e.g. "0.35")
    infill_pct: str = Form(None),       # 0..100 (e.g. "35")
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

    # Profiles
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

    # Infill → percent
    infill_pct_val = parse_infill(infill, infill_pct)

    work = Path(tempfile.mkdtemp(prefix="fixedp_"))
    attempts: List[Dict[str, Any]] = []
    last_cmd_list: Optional[List[str]] = None
    try:
        is_3mf = filename.endswith(".3mf")
        inp = work / ("input.3mf" if is_3mf else "input.stl")
        inp.write_bytes(data)
        out_meta = work / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
        out_3mf  = work / "out.3mf"
        datadir  = work / "cfg"; datadir.mkdir(parents=True, exist_ok=True)

        # Harden only process + filament. Printer stays as-is to avoid format issues.
        hardened_process = harden_process_profile(pick_process0, work, fill_density_pct=infill_pct_val)
        hardened_filament = harden_filament_profile(pick_filament, work, material=material)

        # Build args (Try 1)
        base_args = [
            SLICER_BIN,
            "--debug", str(4 if debug else 0),
            "--datadir", str(datadir),
            "--load-settings", ";".join([hardened_process, pick_printer]),
            "--load-filaments", hardened_filament,
            "--export-slicedata", str(out_meta),
            inp.as_posix(),
            "--slice", "0",
            "--export-3mf", str(out_3mf),
        ]
        last_cmd_list = [XVFB, "-a"] + base_args
        code, out, err = run_cmd(last_cmd_list, timeout=900)
        attempts.append({"tag": "try-1", "cmd": " ".join(last_cmd_list), "stderr_tail": (err or out)[-800:]})

        # Try 2 (Reihenfolge-Variante)
        if code != 0:
            base_args2 = [
                SLICER_BIN,
                "--debug", str(4 if debug else 0),
                "--datadir", str(datadir),
                "--load-settings", ";".join([hardened_process, pick_printer]),
                "--load-filaments", hardened_filament,
                inp.as_posix(),
                "--slice", "0",
                "--export-3mf", str(out_3mf),
                "--export-slicedata", str(out_meta),
            ]
            last_cmd_list = [XVFB, "-a"] + base_args2
            code2, out2, err2 = run_cmd(last_cmd_list, timeout=900)
            attempts.append({"tag": "try-2", "cmd": " ".join(last_cmd_list), "stderr_tail": (err2 or out2)[-800:]})
            code, out, err = code2, out2, err2

        # Try 3 (synthetischer Minimal-Process; kompatibel zu allen Printern)
        if code != 0:
            proc_syn = {
                "type": "process",
                "name": "synthetic_min",
                "version": 1,
                "compatible_printers": ["*"],
                "layer_height": 0.2,
                "fill_density": int(infill_pct_val),
                # Standard: absolute E; falls ihr RELATIVE E wollt, auf True setzen
                "use_relative_e_distances": False,
            }
            syn_path = work / "process_synthetic.json"
            save_json(syn_path, proc_syn)

            base_args3 = [
                SLICER_BIN,
                "--debug", str(4 if debug else 0),
                "--datadir", str(datadir),
                "--load-settings", ";".join([str(syn_path), pick_printer]),
                "--load-filaments", hardened_filament,
                "--export-slicedata", str(out_meta),
                inp.as_posix(),
                "--slice", "0",
                "--export-3mf", str(out_3mf),
            ]
            last_cmd_list = [XVFB, "-a"] + base_args3
            code3, out3, err3 = run_cmd(last_cmd_list, timeout=900)
            attempts.append({"tag": "try-3", "cmd": " ".join(last_cmd_list), "stderr_tail": (err3 or out3)[-800:]})
            code, out, err = code3, out3, err3

        if code != 0:
            err_payload = {
                "message": "Slicing fehlgeschlagen (alle Strategien).",
                "last_cmd": " ".join(last_cmd_list) if last_cmd_list else None,
                "attempts": attempts
            }
            orca_result = _collect_orca_result_json(work)
            if orca_result:
                err_payload["orca_result_json"] = orca_result
            raise HTTPException(status_code=500, detail=err_payload)

        # success → read slicedata
        meta = parse_slicedata_folder(out_meta)
        if not meta.get("duration_s"):
            err_payload = {
                "message": "Keine Druckzeit in Slicedata gefunden.",
                "last_cmd": " ".join(last_cmd_list) if last_cmd_list else None,
                "attempts": attempts
            }
            orca_result = _collect_orca_result_json(work)
            if orca_result:
                err_payload["orca_result_json"] = orca_result
            raise HTTPException(status_code=500, detail=err_payload)

        return {
            "ok": True,
            "input_ext": ".3mf" if is_3mf else ".stl",
            "profiles_used": {
                "printer": Path(pick_printer).name,
                "process": Path(pick_process0).name,
                "filament": Path(pick_filament).name
            },
            "material": (material or "").upper(),
            "infill_pct": int(infill_pct_val),
            "duration_s": float(meta["duration_s"]),
            "filament_mm": meta.get("filament_mm"),
            "filament_g": meta.get("filament_g"),
            "notes": "Gesliced mit festen Profilen (--slice 0)."
        }
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass
