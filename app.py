# ---------- app.py ----------
import os, io, json, time, hashlib, zipfile, shutil, tempfile, subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
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

# known-good JSON Dateien (statt INI)
KNOWN_GOOD_DIR = Path("/app/profiles/_known_good")
KG_MACHINE_JSON  = KNOWN_GOOD_DIR / "machine.json"
KG_PROCESS_JSON  = KNOWN_GOOD_DIR / "process.json"
KG_FILAMENT_JSON = KNOWN_GOOD_DIR / "filament.json"

# Headless-Defaults
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
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

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
# known-good JSONs sicherstellen (Typen so, wie Orca sie erwartet)
# ------------------------------------------------------------------------------
def ensure_known_good_jsons():
    KNOWN_GOOD_DIR.mkdir(parents=True, exist_ok=True)
    if not KG_MACHINE_JSON.exists():
        # WICHTIG: "extruders" und "max_print_height" als STRINGS!
        KG_MACHINE_JSON.write_text(json.dumps({
            "type": "machine",
            "version": "1",
            "from": "user",
            "name": "Generic 200x200 0.4 nozzle",
            "printer_model": "Generic 200",
            "printer_variant": "0.4",
            "printer_technology": "FFF",
            "bed_shape": [[0,0],[200,0],[200,200],[0,200]],
            "max_print_height": "200",
            "extruders": "1",
            "nozzle_diameter": ["0.4"],
            "gcode_flavor": "marlin"
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not KG_PROCESS_JSON.exists():
        KG_PROCESS_JSON.write_text(json.dumps({
            "type": "process",
            "version": "1",
            "from": "user",
            "name": "KnownGood 0.2mm",
            "layer_height": "0.2",
            "initial_layer_height": "0.2",
            "line_width": "0.45",
            "perimeters": "2",
            "solid_layers": "3",
            "z_seam_type": "aligned",
            "use_relative_e_distances": "0",
            "external_perimeter_speed": "60",
            "perimeter_speed": "60",
            "infill_speed": "90",
            "travel_speed": "120",
            "sparse_infill_density": "10%",
            "compatible_printers": ["*", "Generic 200x200 0.4 nozzle"],
            "compatible_printers_condition": ""
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not KG_FILAMENT_JSON.exists():
        KG_FILAMENT_JSON.write_text(json.dumps({
            "type": "filament",
            "from": "user",
            "name": "KnownGood PLA",
            "filament_diameter": ["1.75"],
            "filament_density": ["1.25"],
            "filament_flow_ratio": ["1.0"],
            "nozzle_temperature": ["200"],
            "nozzle_temperature_initial_layer": ["205"],
            "bed_temperature": ["0"],
            "first_layer_bed_temperature": ["0"],
            "compatible_printers": ["*"],
            "compatible_printers_condition": ""
        }, ensure_ascii=False, indent=2), encoding="utf-8")

# ------------------------------------------------------------------------------
# JSON-Härtung (robustes Schema für Orca)
# ------------------------------------------------------------------------------
def harden_printer_json(src: str, wd: Path) -> Tuple[str, dict, str]:
    j = load_json(Path(src))
    # Grundschema + Typen
    j["type"] = "machine"
    j["version"] = str(j.get("version", "1"))
    j["from"] = j.get("from", "user")
    j.setdefault("name", Path(src).stem)
    j.setdefault("printer_model", j["name"])
    j.setdefault("printer_variant", "0.4")
    j.setdefault("printer_technology", "FFF")

    # nozzle_diameter als Liste aus STRINGS
    nd = j.get("nozzle_diameter", ["0.4"])
    if isinstance(nd, list):
        j["nozzle_diameter"] = [str(x) for x in nd]
    else:
        j["nozzle_diameter"] = [str(nd)]

    # extruders MUSS String sein
    ext = j.get("extruders", "1")
    j["extruders"] = str(ext)

    # printable_area (Strings „0x0“) → bed_shape [[x,y],...]
    if not j.get("bed_shape"):
        pa = j.get("printable_area")
        pts: List[List[float]] = []
        if isinstance(pa, list):
            for s in pa:
                s = str(s).lower().replace(" ", "")
                if "x" in s:
                    xs, ys = s.split("x", 1)
                    try:
                        pts.append([float(xs), float(ys)])
                    except:
                        pass
        if len(pts) >= 3:
            j["bed_shape"] = pts[:4] if len(pts) >= 4 else pts + [pts[-1]]
        else:
            j["bed_shape"] = [[0,0],[400,0],[400,400],[0,400]]
    j.pop("printable_area", None)

    # max_print_height MUSS String sein
    mph = j.get("printable_height", j.get("max_print_height", "300"))
    try:
        mph = str(float(mph)).rstrip("0").rstrip(".") if isinstance(mph, (int, float)) else str(mph)
    except:
        mph = "300"
    j["max_print_height"] = mph
    j.pop("printable_height", None)

    out = wd / "printer_hardened.json"
    save_json(out, j)
    return str(out), j, j["name"]

def harden_process_json(src: str, wd: Path, *, infill_pct: int, printer_name: str, printer_json: dict) -> Tuple[str, dict]:
    p = load_json(Path(src))
    p["type"] = "process"
    p["version"] = str(p.get("version","1"))
    p["from"] = p.get("from","user")
    p["sparse_infill_density"] = f"{int(max(0,min(100,infill_pct)))}%"

    # Kompatibilität robust setzen
    base = printer_name.split(" (")[0].strip()
    compat = p.get("compatible_printers", [])
    if not isinstance(compat, list): compat = []
    p["compatible_printers"] = list({*compat, "*", printer_name, base})
    p["compatible_printers_condition"] = ""

    # sinnvolle Defaults, falls fehlen
    p.setdefault("layer_height", "0.2")
    p.setdefault("initial_layer_height", "0.2")
    p.setdefault("line_width", "0.45")
    p.setdefault("perimeters", "2")
    p.setdefault("solid_layers", "3")
    p.setdefault("use_relative_e_distances", "0")

    out = wd / "process_hardened.json"
    save_json(out, p)
    return str(out), p

def harden_filament_json(src: str, wd: Path) -> Tuple[str, dict]:
    f = load_json(Path(src))
    f["type"] = "filament"
    if "version" in f and not isinstance(f["version"], str): f["version"] = str(f["version"])
    # breit kompatibel
    f["compatible_printers"] = ["*"]; f["compatible_printers_condition"] = ""
    out = wd / "filament_hardened.json"
    save_json(out, f)
    return str(out), f

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
# Mini-UI
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
      <div style="margin:8px 0"><label>no-check</label><input name="no_check" type="checkbox" value="1"></div>
      <div style="margin:8px 0"><label>Matrix</label><input name="matrix" type="checkbox" value="1"><small> Varianten-Report</small></div>
      <input type="submit" value="Zeit ermitteln">
      <div><small>JSON-first, gehärtete Presets; Selftest nutzt known-good JSON.</small></div>
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
        info["help_snippet"] = (out or err or "")[:2000]
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
    ensure_known_good_jsons()
    out["known_good_present"] = {
        "printer": KG_MACHINE_JSON.exists(),
        "process": KG_PROCESS_JSON.exists(),
        "filament": KG_FILAMENT_JSON.exists(),
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
# Selftest (End-zu-Ende, JSON known-good)
# ------------------------------------------------------------------------------
@app.get("/selftest", response_class=JSONResponse)
def selftest():
    # sehr kleiner Würfel (10mm) als STL
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

    ensure_known_good_jsons()

    wd = Path(tempfile.mkdtemp(prefix="selftest_"))
    try:
        inp = wd / "cube.stl"; inp.write_bytes(CUBE_STL)
        out_meta = wd / "slicedata"; out_meta.mkdir(exist_ok=True)
        out_3mf  = wd / "out.3mf"
        datadir  = wd / "cfg"; datadir.mkdir(exist_ok=True)

        attempts = []

        # Try JSON (join)
        cmd1 = [XVFB, "-a", which, "--debug", "1", "--datadir", str(datadir),
                "--load-settings", f"{KG_MACHINE_JSON};{KG_PROCESS_JSON}",
                "--load-filaments", str(KG_FILAMENT_JSON),
                "--export-slicedata", str(out_meta),
                str(inp), "--slice", "1", "--export-3mf", str(out_3mf)]
        c1, o1, e1 = run(cmd1, timeout=300)
        attempts.append({"try":"json-1-join","code":c1,"stderr_tail":(e1 or o1)[-2000:], "cmd":" ".join(cmd1)})

        if c1 != 0:
            # Try JSON (split)
            cmd2 = [XVFB, "-a", which, "--debug", "1", "--datadir", str(datadir),
                    "--load-settings", str(KG_MACHINE_JSON),
                    "--load-settings", str(KG_PROCESS_JSON),
                    "--load-filaments", str(KG_FILAMENT_JSON),
                    str(inp), "--slice", "1", "--export-3mf", str(out_3mf),
                    "--export-slicedata", str(out_meta)]
            c2, o2, e2 = run(cmd2, timeout=300)
            attempts.append({"try":"json-2-split","code":c2,"stderr_tail":(e2 or o2)[-2000:], "cmd":" ".join(cmd2)})
        else:
            c2 = 0

        ok = (c1 == 0) or (c2 == 0)
        result = {"ok": ok, "attempts": attempts}

        if ok:
            meta = parse_slicedata_folder(out_meta)
            result["duration_s"] = meta.get("duration_s")
            result["filament_mm"] = meta.get("filament_mm")
            result["filament_g"] = meta.get("filament_g")
        return result
    finally:
        shutil.rmtree(wd, ignore_errors=True)

# ------------------------------------------------------------------------------
# Slicing / Zeitabschätzung (JSON-first)
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
    no_check: int = Form(0),
    matrix: int = Form(0),
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

        # Gehärtete JSONs
        hardened_printer, hp_json, printer_name = harden_printer_json(pick_printer, wd)
        hardened_process, _ = harden_process_json(pick_process0, wd, infill_pct=pct, printer_name=printer_name, printer_json=hp_json)
        hardened_filament, _ = harden_filament_json(pick_filament, wd)

        attempts = []
        result_matrix = []

        def cmd_json_join():
            cmd = [XVFB, "-a", which, "--debug", str(int(debug)),
                   "--datadir", str(datadir),
                   "--load-settings", f"{hardened_printer};{hardened_process}",
                   "--load-filaments", str(hardened_filament),
                   "--export-slicedata", str(out_meta),
                   str(inp), "--slice", "1", "--export-3mf", str(out_3mf)]
            if int(no_check or 0) == 1:
                cmd.append("--no-check")
            return cmd

        def cmd_json_split():
            cmd = [XVFB, "-a", which, "--debug", str(int(debug)),
                   "--datadir", str(datadir),
                   "--load-settings", str(hardened_printer),
                   "--load-settings", str(hardened_process),
                   "--load-filaments", str(hardened_filament),
                   str(inp), "--slice", "1", "--export-3mf", str(out_3mf),
                   "--export-slicedata", str(out_meta)]
            if int(no_check or 0) == 1:
                cmd.append("--no-check")
            return cmd

        plan = [("try-1-json-join", cmd_json_join), ("try-2-json-split", cmd_json_split)]

        last_code, last_out, last_err = None, "", ""
        for tag, factory in plan:
            cmd = factory()
            code, out, err = run(cmd, timeout=900)
            attempts.append({"tag": tag, "cmd": " ".join(cmd), "stderr_tail": (err or out)[-2000:], "code": code})
            result_matrix.append({"tag": tag, "ok": code == 0})
            last_code, last_out, last_err = code, out, err
            if code == 0 and not matrix:
                break

        if matrix:
            return {"ok": any(x["ok"] for x in result_matrix), "matrix": result_matrix, "attempts": attempts,
                    "printer_used": Path(pick_printer).name, "process_used": Path(pick_process0).name, "filament_used": Path(pick_filament).name}

        if last_code != 0:
            diag = {
                "message": "Slicing fehlgeschlagen (alle Strategien).",
                "attempts": attempts,
                "printer_hardened_json": Path(hardened_printer).read_text()[:2000],
                "process_hardened_json": Path(hardened_process).read_text()[:2000],
                "filament_hardened_json": Path(hardened_filament).read_text()[:2000],
            }
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
            "notes": "Sliced via JSON-first; /selftest nutzt known-good JSON."
        }
    finally:
        shutil.rmtree(wd, ignore_errors=True)
