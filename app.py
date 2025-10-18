# ---------- app.py ----------
import os, io, json, time, hashlib, zipfile, shutil, tempfile, subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import xml.etree.ElementTree as ET
import trimesh, numpy as np

# ------------------------------------------------------------------------------
# FastAPI + CORS
# ------------------------------------------------------------------------------
app = FastAPI(title="3D Print – Fixed Profiles Slicing API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # später auf deine Domain(s) begrenzen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

# INI-first bevorzugen
INI_FIRST = True

# Bekannte gute Minimal-INIs (als Notanker)
KNOWN_GOOD_DIR = Path("/app/profiles/_known_good")
KNOWN_GOOD_PRINTER_INI  = KNOWN_GOOD_DIR / "printer.ini"
KNOWN_GOOD_PROCESS_INI  = KNOWN_GOOD_DIR / "process.ini"
KNOWN_GOOD_FILAMENT_INI = KNOWN_GOOD_DIR / "filament.ini"

# Headless
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp")
os.environ.setdefault("LD_LIBRARY_PATH", "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH", ""))
os.environ.setdefault("PATH", "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH", ""))

XVFB = shutil.which("xvfb-run") or "xvfb-run"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------
def sha256_of_bytes(b: bytes) -> str:
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

def slicer_exists() -> bool:
    return (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK)) or \
           (shutil.which(os.path.basename(SLICER_BIN)) is not None)

def run(cmd: List[str], timeout=900) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def np_to_list(x) -> List[float]:
    return list(map(float, x))

def find_profiles() -> Dict[str, List[str]]:
    base = Path("/app/profiles")
    res = {"printer": [], "process": [], "filament": []}
    for key, sub in [("printer", "printers"), ("process", "process"), ("filament", "filaments")]:
        d = base / sub
        if d.exists():
            res[key] = sorted(str(p) for p in d.glob("*.json"))
    return res

def must_pick(paths: List[str], label: str, wanted: Optional[str]) -> str:
    if wanted:
        for p in paths:
            n = Path(p).name
            if n == wanted or wanted in Path(p).stem:
                return p
        raise HTTPException(400, f"{label}-Profil '{wanted}' nicht gefunden.")
    if not paths:
        raise HTTPException(500, f"Kein {label}-Profil gefunden (/app/profiles/{label}s/*.json).")
    return paths[0]

def pick_filament_for_material(paths: List[str], material: str) -> Optional[str]:
    if not paths:
        return None
    m = (material or "").strip().upper()
    for p in paths:
        if m in Path(p).name.upper():
            return p
    return paths[0]

def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))

def save_json(p: Path, obj: dict):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2))

def parse_infill_to_pct(v) -> int:
    if v is None: return 35
    s = str(v).strip().replace("%", "").replace(",", ".")
    try:
        x = float(s)
    except:
        raise HTTPException(400, f"Ungültiger Infill: {v!r}")
    if x <= 1.0:
        x *= 100.0
    return int(round(max(0.0, min(100.0, x))))

# ------------------------------------------------------------------------------
# JSON → INI: flache, kompatible Configs erzeugen
# ------------------------------------------------------------------------------
def to_bed_shape_polygon(printer_json: dict) -> str:
    area = printer_json.get("printable_area")
    if isinstance(area, list) and len(area) >= 3:
        pts = []
        for e in area:
            s = str(e).lower().replace(" ", "")
            if "x" in s:
                pts.append(s)
        if len(pts) >= 3:
            return ",".join(pts[:4] if len(pts) >= 4 else pts + [pts[-1]])
    # Fallback: Quadrat 400x400
    return "0x0,400x0,400x400,0x400"

def first_num(val, default: float) -> float:
    if isinstance(val, list) and val:
        try: return float(val[0])
        except: return default
    try: return float(val)
    except: return default

def write_printer_ini(printer_json: dict, dst: Path) -> Path:
    nozzle = printer_json.get("nozzle_diameter") or ["0.4"]
    nozzle_str = ",".join(str(x) for x in nozzle) if isinstance(nozzle, list) else str(nozzle)
    max_h = printer_json.get("printable_height") or 300
    ini = [
        f"bed_shape = {to_bed_shape_polygon(printer_json)}",
        f"max_print_height = {first_num(max_h, 300)}",
        f"nozzle_diameter = {nozzle_str}",
        "extruders = 1",
        "printer_technology = FFF",
        "gcode_flavor = marlin",
        "use_firmware_retraction = 0",
    ]
    dst.write_text("\n".join(ini) + "\n", encoding="utf-8")
    return dst

def write_process_ini(process_json: dict, infill_pct: int, dst: Path) -> Path:
    lh  = process_json.get("layer_height") or "0.2"
    flh = process_json.get("initial_layer_height") or process_json.get("first_layer_height") or "0.3"
    lw  = process_json.get("line_width") or "0.45"
    ow  = process_json.get("outer_wall_speed") or "60"
    iw  = process_json.get("inner_wall_speed") or "80"
    travel = process_json.get("travel_speed") or "120"
    rel_e = process_json.get("use_relative_e_distances", "0")
    ini = [
        f"layer_height = {lh}",
        f"first_layer_height = {flh}",
        f"fill_density = {int(max(0, min(100, infill_pct)))}",
        f"perimeter_extrusion_width = {lw}",
        f"external_perimeter_extrusion_width = {lw}",
        f"infill_extrusion_width = {lw}",
        f"perimeter_speed = {ow}",
        f"external_perimeter_speed = {ow}",
        f"infill_speed = {iw}",
        f"travel_speed = {travel}",
        f"use_relative_e_distances = {rel_e}",
        "perimeters = 2",
        "solid_layers = 4",
        "top_solid_layers = 4",
        "bottom_solid_layers = 4",
        "avoid_crossing_perimeters = 1",
        "z_seam_type = aligned",
    ]
    dst.write_text("\n".join(ini) + "\n", encoding="utf-8")
    return dst

def write_filament_ini(filament_json: dict, dst: Path) -> Path:
    noz      = filament_json.get("nozzle_temperature") or ["200"]
    noz0     = filament_json.get("nozzle_temperature_initial_layer") or [noz[0] if isinstance(noz, list) and noz else "205"]
    bed      = filament_json.get("bed_temperature") or ["0"]
    bed0     = filament_json.get("bed_temperature_initial_layer") or bed
    dia      = filament_json.get("filament_diameter") or ["1.75"]
    density  = filament_json.get("filament_density") or ["1.25"]  # g/cm³
    flow     = filament_json.get("filament_flow_ratio") or ["1.0"]

    def first_str(v, dft="0"):
        if isinstance(v, list) and v: return str(v[0])
        if isinstance(v, (int, float)): return str(v)
        if isinstance(v, str): return v
        return dft

    ini = [
        f"temperature = {first_str(noz, '200')}",
        f"first_layer_temperature = {first_str(noz0, '205')}",
        f"bed_temperature = {first_str(bed, '0')}",
        f"first_layer_bed_temperature = {first_str(bed0, '0')}",
        f"filament_diameter = {first_str(dia, '1.75')}",
        f"filament_density = {first_str(density, '1.25')}",
        f"filament_flow_ratio = {first_str(flow, '1.0')}",
    ]
    dst.write_text("\n".join(ini) + "\n", encoding="utf-8")
    return dst

# ------------------------------------------------------------------------------
# JSON-Härtung (nur Fallback)
# ------------------------------------------------------------------------------
def harden_printer_json(src: str, wd: Path) -> Tuple[str, dict, str]:
    j = load_json(Path(src))
    j["type"] = "machine"
    nd = j.get("nozzle_diameter")
    if isinstance(nd, list): j["nozzle_diameter"] = [str(x) for x in nd]
    elif nd is not None:    j["nozzle_diameter"] = [str(nd)]
    j.setdefault("name", Path(src).stem)
    j.setdefault("printer_model", j["name"].split(" 0.")[0])
    j.setdefault("printer_variant", "0.4")
    j.setdefault("printer_technology", "FFF")
    if "version" in j and not isinstance(j["version"], str): j["version"] = str(j["version"])
    out = wd / "printer_hardened.json"; save_json(out, j); return str(out), j, j["name"]

def harden_process_json(src: str, wd: Path, *, infill_pct: int, printer_name: str, printer_json: dict) -> Tuple[str, dict]:
    p = load_json(Path(src))
    p["type"] = "process"; p.setdefault("version", "1")
    if not isinstance(p["version"], str): p["version"] = str(p["version"])
    p.pop("fill_density", None)
    p["sparse_infill_density"] = f"{int(max(0, min(100, infill_pct)))}%"
    p["before_layer_gcode"] = (p.get("before_layer_gcode") or "").replace("G92 E0", "")
    # Harter Match
    p["printer_technology"] = "FFF"
    p["printer_model"] = printer_json.get("printer_model") or printer_name
    p["printer_variant"] = printer_json.get("printer_variant") or "0.4"
    p["nozzle_diameter"] = printer_json.get("nozzle_diameter") or ["0.4"]
    compat = p.get("compatible_printers")
    if not isinstance(compat, list): compat = []
    base = printer_name.split(" (")[0].strip()
    p["compatible_printers"] = list({*([x for x in compat if isinstance(x, str)]), "*", printer_name, base})
    p["compatible_printers_condition"] = ""
    out = wd / "process_hardened.json"; save_json(out, p); return str(out), p

def harden_filament_json(src: str, wd: Path) -> Tuple[str, dict]:
    f = load_json(Path(src))
    f["type"] = "filament"
    if "version" in f and not isinstance(f["version"], str): f["version"] = str(f["version"])
    f["compatible_printers"] = ["*"]; f["compatible_printers_condition"] = ""
    out = wd / "filament_hardened.json"; save_json(out, f); return str(out), f

# ------------------------------------------------------------------------------
# Analyse (STL/3MF)
# ------------------------------------------------------------------------------
def analyze_stl(data: bytes) -> Dict[str, Any]:
    mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(tuple(g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)))
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("STL enthält keine gültige Geometrie.")
    mesh.remove_unreferenced_vertices(); mesh.process(validate=True)
    tri = int(mesh.faces.shape[0]); vol = float(abs(mesh.volume)) if mesh.is_volume else None
    area = float(mesh.area) if mesh.area is not None else None
    b = mesh.bounds; size = b[1] - b[0]
    return {
        "mesh_is_watertight": bool(mesh.is_watertight),
        "triangles": tri,
        "volume_mm3": vol,
        "surface_area_mm2": area,
        "bbox_min_mm": np_to_list(b[0]),
        "bbox_max_mm": np_to_list(b[1]),
        "bbox_size_mm": np_to_list(size),
        "units_assumed": "mm",
    }

def analyze_3mf(data: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        model = next((c for c in ("3D/3dmodel.model", "3d/3dmodel.model", "3D/Model.model") if c in names), None)
        res = {"zip_entries": len(names), "has_model_xml": bool(model), "objects": [], "units": None, "triangles_total": 0, "objects_count": 0}
        if not model: return res
        root = ET.fromstring(zf.read(model))
        ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        res["units"] = root.attrib.get("unit") or root.attrib.get("units") or "mm"
        objs = root.findall(".//m:object", ns) if ns else root.findall(".//object")
        res["objects_count"] = len(objs)
        tri_total = 0; out = []
        for obj in objs:
            oid = obj.attrib.get("id"); typ = obj.attrib.get("type")
            mesh_el = obj.find("m:mesh", ns) if ns else obj.find("mesh")
            if mesh_el is None:
                out.append({"id": oid, "type": typ, "triangles": 0}); continue
            tris = mesh_el.find("m:triangles", ns) if ns else mesh_el.find("triangles")
            t = len(tris.findall("m:triangle", ns) if ns else tris.findall("triangle")) if tris is not None else 0; tri_total += t
            bbox = None; verts = mesh_el.find("m:vertices", ns) if ns else mesh_el.find("vertices")
            if verts is not None:
                coords = []
                for v in (verts.findall("m:vertex", ns) if ns else verts.findall("vertex")):
                    try: coords.append((float(v.attrib.get("x", "0")), float(v.attrib.get("y", "0")), float(v.attrib.get("z", "0"))))
                    except: pass
                if coords:
                    arr = np.array(coords, dtype=float); vmin = arr.min(axis=0); vmax = arr.max(axis=0)
                    bbox = {"bbox_min": np_to_list(vmin), "bbox_max": np_to_list(vmax), "bbox_size": np_to_list(vmax - vmin)}
            out.append({"id": oid, "type": typ, "triangles": t, **({"bbox": bbox} if bbox else {})})
        res["triangles_total"] = tri_total; res["objects"] = out; return res

def parse_slicedata_folder(folder: Path) -> Dict[str, Any]:
    out = {"duration_s": None, "filament_mm": None, "filament_g": None, "files": []}
    for jf in sorted(folder.glob("*.json")):
        try:
            j = json.loads(jf.read_text()[:2_000_000])
            out["duration_s"] = out["duration_s"] or j.get("print_time_sec") or j.get("time_sec")
            out["filament_mm"] = out["filament_mm"] or j.get("filament_used_mm") or j.get("filament_mm")
            out["filament_g"]  = out["filament_g"]  or j.get("filament_used_g")  or j.get("filament_g")
            out["files"].append(jf.name)
        except: pass
    return out

# ------------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html><meta charset="utf-8"><title>3D Print – Fixed Profiles Slicing</title>
<style>:root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
body{font-family:system-ui,Segoe UI,Arial;margin:24px;line-height:1.45;color:var(--fg)}
h1{margin:0 0 16px;font-size:22px}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
.card{border:1px solid var(--line);border-radius:12px;padding:16px;background:#fff}
button,input[type=submit]{padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#111827;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#111827}label{font-weight:600;margin-right:8px}
input[type=number],select,input[type=text]{padding:8px 10px;border:1px solid var(--line);border-radius:10px}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:360px;overflow:auto}
small{color:var(--muted)}</style>
<h1>3D Print – Fixed Profiles Slicing</h1>
<div class="grid">
  <div class="card">
    <button class="secondary" onclick="openDocs()">Swagger (API-Doku)</button>
    <button onclick="hit('/health','#out')">Health</button>
    <button onclick="hit('/slicer_env','#out')">Slicer-Env</button>
    <button onclick="hit('/selftest','#out')">Selftest</button>
    <button onclick="hit('/preset_dump','#out')">Preset-Dump</button>
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
    <h3>Druckzeit (/estimate_time)</h3>
    <form onsubmit="return sendTime(event)">
      <input type="file" name="file" accept=".stl,.3mf" required>
      <div style="margin:8px 0">
        <label>Material</label>
        <select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select>
        <label>Infill</label><input name="infill" type="text" value="0.35" style="width:120px">
      </div>
      <div style="margin:8px 0"><label>Printer-Profil</label><input name="printer_profile" placeholder="optional: Dateiname"></div>
      <div style="margin:8px 0"><label>Process-Profil</label><input name="process_profile" placeholder="optional: Dateiname"></div>
      <div style="margin:8px 0"><label>Filament-Profil</label><input name="filament_profile" placeholder="optional: Dateiname"></div>
      <div style="margin:8px 0"><label>Known-good</label><input name="force_known_good" type="checkbox" value="1"></div>
      <input type="submit" value="Zeit ermitteln">
      <div><small>INI-first (Preset-Checks umgangen), JSON-Fallback bei Bedarf.</small></div>
    </form>
  </div>
</div>
<pre id="out">Output erscheint hier …</pre>
<script>
const base=location.origin;
async function hit(path,sel){const out=document.querySelector(sel||'#out');out.textContent='Lade '+path+' …';
  try{const r=await fetch(base+path);const isJson=(r.headers.get('content-type')||'').includes('json');
      out.textContent=isJson?JSON.stringify(await r.json(),null,2):await r.text();}catch(e){out.textContent='Fehler: '+e;}}
async function sendAnalyze(e){e.preventDefault();const fd=new FormData(e.target);const out=document.querySelector('#out');out.textContent='Analysiere …';
  try{const r=await fetch(base+'/analyze_upload',{method:'POST',body:fd});out.textContent=JSON.stringify(await r.json(),null,2);}catch(err){out.textContent='Fehler: '+err;}return false;}
async function sendTime(e){e.preventDefault();const fd=new FormData(e.target);const out=document.querySelector('#out');out.textContent='Slicen …';
  try{const r=await fetch(base+'/estimate_time',{method:'POST',body:fd});out.textContent=JSON.stringify(await r.json(),null,2);}catch(err){out.textContent='Fehler: '+err;}return false;}
function openDocs(){window.open(base+'/docs','_blank');}
</script>
"""

# ------------------------------------------------------------------------------
# Health & Env
# ------------------------------------------------------------------------------
@app.get("/health", response_class=JSONResponse)
def health():
    return {"ok": True, "slicer_bin": SLICER_BIN, "slicer_present": slicer_exists(), "profiles": find_profiles()}

@app.get("/slicer_env", response_class=JSONResponse)
def slicer_env():
    which = shutil.which(os.path.basename(SLICER_BIN)) or SLICER_BIN
    info = {"ok": True, "bin_exists": slicer_exists(), "which": which}
    try:
        code, out, err = run([which, "--help"], timeout=8)
        info["return_code"] = code
        info["help_snippet"] = (out or err or "")[:2000]  # größerer Ausschnitt hilft
        # Version, falls unterstützt
        try:
            vcode, vout, verr = run([which, "--version"], timeout=4)
            info["version"] = (vout or verr or "").strip()
        except Exception:
            pass
    except Exception as e:
        info["return_code"] = None
        info["help_snippet"] = f"(help not available) {e}"
    return info

# ------------------------------------------------------------------------------
# Preset-Dump (Transparenz)
# ------------------------------------------------------------------------------
@app.get("/preset_dump", response_class=JSONResponse)
def preset_dump():
    prof = find_profiles()
    out = {"printer": [], "process": [], "filament": []}
    for k in out.keys():
        for p in prof[k][:3]:
            try:
                j = load_json(Path(p))
                s = json.dumps(j, ensure_ascii=False)
            except Exception as e:
                s = f"ERR: {e}"
            out[k].append({"file": p, "sample": s[:2000]})
    # known good vorhanden?
    out["known_good_present"] = {
        "printer": KNOWN_GOOD_PRINTER_INI.exists(),
        "process": KNOWN_GOOD_PROCESS_INI.exists(),
        "filament": KNOWN_GOOD_FILAMENT_INI.exists(),
    }
    return out

# ------------------------------------------------------------------------------
# Upload-Analyse
# ------------------------------------------------------------------------------
@app.post("/analyze_upload", response_class=JSONResponse)
async def analyze_upload(file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    if not (name.endswith(".stl") or name.endswith(".3mf")):
        raise HTTPException(400, "Nur STL/3MF.")
    data = await file.read()
    if not data: raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_FILE_BYTES: raise HTTPException(413, f"Datei > {MAX_FILE_BYTES//(1024*1024)} MB.")
    base = {"ok": True, "filename": file.filename, "filesize_bytes": len(data), "sha256": sha256_of_bytes(data),
            "filetype": "stl" if name.endswith(".stl") else "3mf", "generated_at": int(time.time())}
    try:
        if name.endswith(".stl"): return {**base, "stl": analyze_stl(data)}
        else: return {**base, "three_mf": analyze_3mf(data)}
    except Exception as e:
        raise HTTPException(500, f"Analyse fehlgeschlagen: {e}")

# ------------------------------------------------------------------------------
# Selftest (End-zu-Ende)
# ------------------------------------------------------------------------------
@app.get("/selftest", response_class=JSONResponse)
def selftest():
    # Mini-Würfel (10 mm) – absichtlich sehr klein/sauber
    CUBE_STL = b"""
solid cube
facet normal 0 0 -1
 outer loop
  vertex 0 0 0
  vertex 10 10 0
  vertex 10 0 0
 endloop
endfacet
facet normal 0 0 -1
 outer loop
  vertex 0 0 0
  vertex 0 10 0
  vertex 10 10 0
 endloop
endfacet
endsolid
"""
    which = shutil.which(os.path.basename(SLICER_BIN)) or SLICER_BIN
    if not slicer_exists():
        return {"ok": False, "error": "Slicer nicht gefunden", "slicer_bin": SLICER_BIN}

    wd = Path(tempfile.mkdtemp(prefix="selftest_"))
    try:
        inp = wd / "cube.stl"; inp.write_bytes(CUBE_STL)
        out_meta = wd / "slicedata"; out_meta.mkdir(exist_ok=True)
        out_3mf  = wd / "out.3mf"
        datadir  = wd / "cfg"; datadir.mkdir(exist_ok=True)

        # Known-good INIs (lokal erzeugen, falls nicht vorhanden)
        if not KNOWN_GOOD_DIR.exists():
            KNOWN_GOOD_DIR.mkdir(parents=True, exist_ok=True)
        if not KNOWN_GOOD_PRINTER_INI.exists():
            KNOWN_GOOD_PRINTER_INI.write_text("bed_shape = 0x0,200x0,200x200,0x200\nmax_print_height = 200\nnozzle_diameter = 0.4\nextruders = 1\nprinter_technology = FFF\ngcode_flavor = marlin\n", encoding="utf-8")
        if not KNOWN_GOOD_PROCESS_INI.exists():
            KNOWN_GOOD_PROCESS_INI.write_text("layer_height = 0.2\nfirst_layer_height = 0.2\nfill_density = 10\nperimeter_extrusion_width = 0.45\nexternal_perimeter_extrusion_width = 0.45\ninfill_extrusion_width = 0.45\nperimeters = 2\nsolid_layers = 3\nz_seam_type = aligned\nuse_relative_e_distances = 0\ntravel_speed = 120\n", encoding="utf-8")
        if not KNOWN_GOOD_FILAMENT_INI.exists():
            KNOWN_GOOD_FILAMENT_INI.write_text("temperature = 200\nfirst_layer_temperature = 205\nbed_temperature = 0\nfirst_layer_bed_temperature = 0\nfilament_diameter = 1.75\nfilament_density = 1.25\nfilament_flow_ratio = 1.0\n", encoding="utf-8")

        attempts = []

        # Try-INI 1
        cmd1 = [XVFB, "-a", which, "--debug", "1", "--datadir", str(datadir),
                "--load-settings", f"{KNOWN_GOOD_PRINTER_INI};{KNOWN_GOOD_PROCESS_INI}",
                "--load-filaments", str(KNOWN_GOOD_FILAMENT_INI),
                "--export-slicedata", str(out_meta),
                str(inp), "--slice", "1", "--export-3mf", str(out_3mf)]
        c1, o1, e1 = run(cmd1, timeout=300)
        attempts.append({"try":"ini-1","code":c1,"stderr_tail":(e1 or o1)[-2000:], "cmd":" ".join(cmd1)})

        if c1 != 0:
            cmd2 = [XVFB, "-a", which, "--debug", "1", "--datadir", str(datadir),
                    "--load-settings", str(KNOWN_GOOD_PRINTER_INI),
                    "--load-settings", str(KNOWN_GOOD_PROCESS_INI),
                    "--load-filaments", str(KNOWN_GOOD_FILAMENT_INI),
                    str(inp), "--slice", "1", "--export-3mf", str(out_3mf),
                    "--export-slicedata", str(out_meta)]
            c2, o2, e2 = run(cmd2, timeout=300)
            attempts.append({"try":"ini-2-split","code":c2,"stderr_tail":(e2 or o2)[-2000:], "cmd":" ".join(cmd2)})
        else:
            c2 = 0

        ok_ini = (c1 == 0) or (c2 == 0)
        result = {"ok": ok_ini, "attempts": attempts}

        if ok_ini:
            meta = parse_slicedata_folder(out_meta)
            result["duration_s"] = meta.get("duration_s")
            result["filament_mm"] = meta.get("filament_mm")
            result["filament_g"] = meta.get("filament_g")
        return result
    finally:
        shutil.rmtree(wd, ignore_errors=True)

# ------------------------------------------------------------------------------
# Slicing / Zeitabschätzung
# ------------------------------------------------------------------------------
@app.post("/estimate_time", response_class=JSONResponse)
async def estimate_time(
    file: UploadFile = File(...),
    printer_profile: str = Form(None),
    process_profile: str = Form(None),
    filament_profile: str = Form(None),
    material: str = Form("PLA"),
    infill: str = Form("0.35"),
    debug: int = Form(0),
    force_known_good: int = Form(0),
    matrix: int = Query(0, description="Wenn 1: testet mehrere Slicing-Varianten und liefert die Matrix zurück."),
):
    if not slicer_exists():
        raise HTTPException(500, "OrcaSlicer CLI nicht verfügbar.")

    name = (file.filename or "").lower()
    if not (name.endswith(".stl") or name.endswith(".3mf")):
        raise HTTPException(400, "Nur STL/3MF.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Datei > {MAX_FILE_BYTES//(1024*1024)} MB.")
    pct = parse_infill_to_pct(infill)

    # Profile identifizieren
    prof = find_profiles()
    pick_printer  = must_pick(prof["printer"],  "printer",  printer_profile)
    pick_process0 = must_pick(prof["process"],  "process",  process_profile)

    if filament_profile:
        pick_filament = must_pick(prof["filament"], "filament", filament_profile)
    else:
        pick_filament = pick_filament_for_material(prof["filament"], material)
        if not pick_filament:
            raise HTTPException(500, "Kein Filament-Profil gefunden.")

    wd = Path(tempfile.mkdtemp(prefix="fixedp_"))
    try:
        is_3mf = name.endswith(".3mf")
        inp = wd / ("input.3mf" if is_3mf else "input.stl"); inp.write_bytes(data)
        out_meta = wd / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
        out_3mf  = wd / "out.3mf"
        datadir  = wd / "cfg"; datadir.mkdir(parents=True, exist_ok=True)

        which = shutil.which(os.path.basename(SLICER_BIN)) or SLICER_BIN

        # INI Pfad vorbereiten
        if force_known_good:
            printer_ini  = KNOWN_GOOD_PRINTER_INI
            process_ini  = KNOWN_GOOD_PROCESS_INI
            filament_ini = KNOWN_GOOD_FILAMENT_INI
            # Falls nicht vorhanden: generieren (wie bei /selftest)
            if not KNOWN_GOOD_PRINTER_INI.exists():
                KNOWN_GOOD_DIR.mkdir(parents=True, exist_ok=True)
                KNOWN_GOOD_PRINTER_INI.write_text("bed_shape = 0x0,200x0,200x200,0x200\nmax_print_height = 200\nnozzle_diameter = 0.4\nextruders = 1\nprinter_technology = FFF\ngcode_flavor = marlin\n", encoding="utf-8")
            if not KNOWN_GOOD_PROCESS_INI.exists():
                KNOWN_GOOD_PROCESS_INI.write_text("layer_height = 0.2\nfirst_layer_height = 0.2\nfill_density = 10\nperimeter_extrusion_width = 0.45\nexternal_perimeter_extrusion_width = 0.45\ninfill_extrusion_width = 0.45\nperimeters = 2\nsolid_layers = 3\nz_seam_type = aligned\nuse_relative_e_distances = 0\ntravel_speed = 120\n", encoding="utf-8")
            if not KNOWN_GOOD_FILAMENT_INI.exists():
                KNOWN_GOOD_FILAMENT_INI.write_text("temperature = 200\nfirst_layer_temperature = 205\nbed_temperature = 0\nfirst_layer_bed_temperature = 0\nfilament_diameter = 1.75\nfilament_density = 1.25\nfilament_flow_ratio = 1.0\n", encoding="utf-8")
        else:
            # Eigene JSON → INI
            printer_json  = load_json(Path(pick_printer))
            process_json0 = load_json(Path(pick_process0))
            filament_json = load_json(Path(pick_filament))
            printer_ini  = write_printer_ini(printer_json,  wd / "printer.ini")
            process_ini  = write_process_ini(process_json0, pct, wd / "process.ini")
            filament_ini = write_filament_ini(filament_json, wd / "filament.ini")

        # Gehärtete JSONs (nur Fallback)
        hardened_printer, hp_json, printer_name = harden_printer_json(pick_printer, wd)
        hardened_process, _ = harden_process_json(pick_process0, wd, infill_pct=pct, printer_name=printer_name, printer_json=hp_json)
        hardened_filament, _ = harden_filament_json(pick_filament, wd)

        attempts = []
        result_matrix = []

        def cmd_ini_join():
            return [XVFB, "-a", which, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                    "--datadir", str(datadir),
                    "--load-settings", f"{printer_ini};{process_ini}",
                    "--load-filaments", str(filament_ini),
                    "--export-slicedata", str(out_meta),
                    str(inp), "--slice", "1", "--export-3mf", str(out_3mf)]

        def cmd_ini_split():
            return [XVFB, "-a", which, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                    "--datadir", str(datadir),
                    "--load-settings", str(printer_ini),
                    "--load-settings", str(process_ini),
                    "--load-filaments", str(filament_ini),
                    str(inp), "--slice", "1", "--export-3mf", str(out_3mf),
                    "--export-slicedata", str(out_meta)]

        def cmd_json_join():
            return [XVFB, "-a", which, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                    "--datadir", str(datadir),
                    "--load-settings", f"{hardened_printer};{hardened_process}",
                    "--load-filaments", str(hardened_filament),
                    "--export-slicedata", str(out_meta),
                    str(inp), "--slice", "1", "--export-3mf", str(out_3mf)]

        def cmd_json_split():
            return [XVFB, "-a", which, "--debug", str(int(debug) if isinstance(debug, int) else 0),
                    "--datadir", str(datadir),
                    "--load-settings", str(hardened_printer),
                    "--load-settings", str(hardened_process),
                    "--load-filaments", str(hardened_filament),
                    str(inp), "--slice", "1", "--export-3mf", str(out_3mf),
                    "--export-slicedata", str(out_meta)]

        # Matrix-Steuerung
        plan = []
        if INI_FIRST:
            plan += [("try-1-ini-join", cmd_ini_join), ("try-2-ini-split", cmd_ini_split)]
            plan += [("try-3-json-join", cmd_json_join), ("try-4-json-split", cmd_json_split)]
        else:
            plan += [("try-1-json-join", cmd_json_join), ("try-2-json-split", cmd_json_split)]
            plan += [("try-3-ini-join", cmd_ini_join), ("try-4-ini-split", cmd_ini_split)]

        last_code, last_out, last_err = None, "", ""
        finished = False

        for tag, factory in plan:
            cmd = factory()
            code, out, err = run(cmd, timeout=900)
            attempts.append({"tag": tag, "cmd": " ".join(cmd), "stderr_tail": (err or out)[-2000:], "code": code})
            result_matrix.append({"tag": tag, "ok": code == 0})
            last_code, last_out, last_err = code, out, err
            if code == 0 and not matrix:  # beim ersten Erfolg abbrechen, außer Matrix-Mode
                finished = True
                break

        if matrix:
            return {"ok": any(x["ok"] for x in result_matrix), "matrix": result_matrix, "attempts": attempts}

        if last_code != 0:
            # Diagnose erweitern
            diag = {
                "message": "Slicing fehlgeschlagen (alle Strategien).",
                "attempts": attempts,
            }
            # füge INIs / gehärtete Profile (gekürzt) bei
            def safe_read(p: Path, n=1200):
                try: return p.read_text()[:n]
                except: return None
            diag["printer_ini"]  = safe_read(Path(printer_ini) if isinstance(printer_ini, Path) else Path(str(printer_ini)))
            diag["process_ini"]  = safe_read(Path(process_ini) if isinstance(process_ini, Path) else Path(str(process_ini)))
            diag["filament_ini"] = safe_read(Path(filament_ini) if isinstance(filament_ini, Path) else Path(str(filament_ini)))
            diag["printer_hardened_json"]  = safe_read(Path(hardened_printer))
            diag["process_hardened_json"]  = safe_read(Path(hardened_process))
            diag["filament_hardened_json"] = safe_read(Path(hardened_filament))
            raise HTTPException(500, detail=diag)

        meta = parse_slicedata_folder(out_meta)
        if not meta.get("duration_s"):
            raise HTTPException(500, detail="Keine Druckzeit in Slicedata gefunden.")

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
            "notes": f"Sliced via {'INI-first' if INI_FIRST else 'JSON-first'}; setze force_known_good=1 für festen Notanker; matrix=1 zeigt Varianten."
        }
    finally:
        shutil.rmtree(wd, ignore_errors=True)
