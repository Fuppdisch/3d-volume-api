# app.py
import os
import io
import json
import shutil
import tempfile
import hashlib
import subprocess
from typing import Optional, Literal, Dict, Any, List

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

API_TITLE = "Online 3D-Druck Kalkulator & Slicer API"
DESCRIPTION = "FastAPI-Backend für Volumen, Gewicht & OrcaSlicer-Checks"
VERSION = "2025-10-18"

ORCA_CANDIDATES = [
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "orca-slicer",
]
XVFB = os.environ.get("XVFB_BIN", "/usr/bin/xvfb-run")

PROFILES_ROOT = os.environ.get("PROFILES_ROOT", "/app/profiles")
PRINTERS_DIR = os.path.join(PROFILES_ROOT, "printers")
PROCESS_DIR = os.path.join(PROFILES_ROOT, "process")
FILAMENTS_DIR = os.path.join(PROFILES_ROOT, "filaments")

MATERIAL_DENSITY = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC": 1.20,
}

ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")

app = FastAPI(title=API_TITLE, description=DESCRIPTION, version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- Models --------------------

class AnalyzeOut(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float

class WeightDirectIn(BaseModel):
    volume_mm3: float
    material: Literal["PLA", "PETG", "ASA", "PC"]
    infill: float  # 0..1

class WeightDirectOut(BaseModel):
    weight_g: float

# -------------------- Helpers (general) --------------------

def _safe_float3(x: float) -> float:
    return float(f"{x:.3f}")

def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _which_orca() -> Optional[str]:
    for cand in ORCA_CANDIDATES:
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    return None

def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _first_file(dirpath: str, exts: List[str]) -> Optional[str]:
    if not os.path.isdir(dirpath):
        return None
    for name in sorted(os.listdir(dirpath)):
        p = os.path.join(dirpath, name)
        if os.path.isfile(p) and any(name.lower().endswith(e) for e in exts):
            return p
    return None

def _load_profile_or_none(dirpath: str) -> Optional[dict]:
    p = _first_file(dirpath, [".json"])
    return _read_json(p) if p else None

def _load_filament_for_material(material: str) -> Optional[str]:
    if not os.path.isdir(FILAMENTS_DIR):
        return None
    for n in sorted(os.listdir(FILAMENTS_DIR)):
        p = os.path.join(FILAMENTS_DIR, n)
        if os.path.isfile(p) and n.lower().endswith(".json") and material.lower() in n.lower():
            return p
    return None

# -------------------- Mesh / Volume --------------------

def _analyze_mesh_bytes(data: bytes, unit: Literal["mm","cm","m"]) -> Dict[str, Any]:
    import trimesh
    import numpy as np

    try:
        mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    except Exception:
        mesh = trimesh.load(io.BytesIO(data), force="mesh")

    if not isinstance(mesh, trimesh.Trimesh):
        if hasattr(mesh, "dump"):
            mesh = mesh.dump().sum()

    # scale to mm
    if unit == "cm":
        mesh.apply_scale(10.0)
    elif unit == "m":
        mesh.apply_scale(1000.0)

    # light repairs
    try: trimesh.repair.fix_normals(mesh)
    except Exception: pass
    try: trimesh.repair.fill_holes(mesh)
    except Exception: pass

    vol = None
    if getattr(mesh, "is_watertight", False):
        try: vol = float(mesh.volume)
        except Exception: vol = None

    if vol is None or vol <= 0:
        try:
            pitch = 0.5
            vox = mesh.voxelized(pitch=pitch)
            vol = float(vox.points.shape[0]) * (pitch ** 3)
        except Exception:
            try:
                vol = float(mesh.convex_hull.volume)
            except Exception:
                raise HTTPException(status_code=422, detail="Volumen konnte nicht bestimmt werden")

    return {
        "volume_mm3": vol,
        "volume_cm3": vol / 1000.0,
        "triangles": int(mesh.faces.shape[0]) if hasattr(mesh, "faces") else None,
        "watertight": bool(getattr(mesh, "is_watertight", False)),
        "bbox_mm": mesh.bounds.tolist() if hasattr(mesh, "bounds") else None,
    }

# -------------------- Profile builders --------------------

def _parse_bed_shape(value) -> List[List[float]]:
    if isinstance(value, list) and value and isinstance(value[0], list):
        return [[float(a), float(b)] for a, b in value]
    pts: List[List[float]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and "x" in item:
                a, b = item.split("x", 1)
                try: pts.append([float(a), float(b)])
                except Exception: pass
    if len(pts) >= 3:
        return pts
    return [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]]

def _normalize_machine(machine_in: Optional[dict]) -> dict:
    bed_shape = _parse_bed_shape(
        (machine_in or {}).get("bed_shape")
        or (machine_in or {}).get("printable_area")
        or []
    )
    max_h = (machine_in or {}).get("max_print_height") \
            or (machine_in or {}).get("printable_height") \
            or 300.0
    try:
        max_h_val = float(max_h)
    except Exception:
        max_h_val = 300.0

    nozzle_in = (machine_in or {}).get("nozzle_diameter", [0.4])
    if isinstance(nozzle_in, (int, float, str)):
        nozzle_list = [nozzle_in]
    else:
        nozzle_list = nozzle_in
    nozzle_list = [float(x) for x in nozzle_list]

    name = (machine_in or {}).get("name") or "Generic 0.4 nozzle"
    model = (machine_in or {}).get("printer_model") or "Generic"
    variant = (machine_in or {}).get("printer_variant") or "0.4"

    return {
        "type": "machine",
        "version": "1",
        "from": "user",
        "name": name,
        "printer_technology": "FFF",
        "gcode_flavor": "marlin",
        "printer_model": model,
        "printer_variant": variant,
        "bed_shape": bed_shape,
        "max_print_height": max_h_val,    # numeric
        "min_layer_height": 0.06,         # numeric
        "max_layer_height": 0.30,         # numeric
        "extruders": 1,                   # numeric
        "nozzle_diameter": nozzle_list,   # list of floats
    }

def _normalize_process(proc_in: Optional[dict], infill: float, machine_ref: dict, mode: Literal["relaxed","named","bound"]="relaxed") -> dict:
    layer_h = str((proc_in or {}).get("layer_height", "0.2"))
    first_layer = str((proc_in or {}).get("first_layer_height")
                      or (proc_in or {}).get("initial_layer_height")
                      or "0.3")

    try:
        infill_pct = max(0.0, min(1.0, float(infill)))
    except Exception:
        infill_pct = 0.35
    infill_str = f"{int(round(infill_pct * 100))}%"

    line_w = str((proc_in or {}).get("line_width", "0.45"))
    perim = str((proc_in or {}).get("perimeters", "2"))
    top_l = str((proc_in or {}).get("top_solid_layers", (proc_in or {}).get("solid_layers", "3")))
    bot_l = str((proc_in or {}).get("bottom_solid_layers", (proc_in or {}).get("solid_layers", "3")))

    base = {
        "type": "process",
        "version": "1",
        "from": "user",
        "name": (proc_in or {}).get("name", "0.20mm Standard"),
        "layer_height": layer_h,
        "first_layer_height": first_layer,

        # Infill
        "sparse_infill_density": infill_str,

        # Extrusion widths – verbreitete Keys
        "line_width": line_w,
        "perimeter_extrusion_width": line_w,
        "external_perimeter_extrusion_width": line_w,
        "infill_extrusion_width": line_w,

        # Schichten
        "perimeters": perim,
        "top_solid_layers": top_l,
        "bottom_solid_layers": bot_l,

        # harmlose Geschwindigkeiten
        "outer_wall_speed": str((proc_in or {}).get("outer_wall_speed", "250")),
        "inner_wall_speed": str((proc_in or {}).get("inner_wall_speed", "350")),
        "travel_speed": str((proc_in or {}).get("travel_speed", "500")),

        "before_layer_gcode": (proc_in or {}).get("before_layer_gcode", ""),
        "layer_gcode": (proc_in or {}).get("layer_gcode", ""),
        "toolchange_gcode": (proc_in or {}).get("toolchange_gcode", ""),
        "printing_by_object_gcode": (proc_in or {}).get("printing_by_object_gcode", ""),
    }

    # Kompatibilitätsmodus
    if mode == "relaxed":
        base.update({
            "compatible_printers": ["*"],
            "compatible_printers_condition": "",
        })
    elif mode == "named":
        base.update({
            "compatible_printers": ["*", machine_ref.get("name", "Generic 0.4 nozzle")],
            "compatible_printers_condition": "",
        })
    elif mode == "bound":
        # Process an Machine binden
        base.update({
            "printer_technology": machine_ref.get("printer_technology", "FFF"),
            "printer_model": machine_ref.get("printer_model", "Generic"),
            "printer_variant": machine_ref.get("printer_variant", "0.4"),
            "nozzle_diameter": [str(x) for x in (machine_ref.get("nozzle_diameter") or [0.4])],
            "compatible_printers": [machine_ref.get("name", "Generic 0.4 nozzle")],
            "compatible_printers_condition": "",
        })

    # Aufräumen potentiell problematischer Felder aus eingehendem Prozess
    for k in ("fill_density",):  # wir nutzen sparse_infill_density
        if k in base:
            base.pop(k, None)

    return base

# -------------------- Orca Runner --------------------

def _run_orca(orcapath: str, work: str, input_stl: str, machine_json: str, process_json: str,
              filament_json: Optional[str], arrange: int, orient: int, debug: int) -> Dict[str, Any]:
    out3mf = os.path.join(work, "out.3mf")
    slicedata = os.path.join(work, "slicedata")
    os.makedirs(slicedata, exist_ok=True)

    cmd = [
        XVFB, "-a",
        orcapath,
        "--debug", str(int(debug)),
        "--datadir", os.path.join(work, "cfg"),
        "--load-settings", f"{machine_json};{process_json}",
    ]
    if filament_json:
        cmd += ["--load-filaments", filament_json]
    cmd += ["--arrange", str(int(arrange))]
    cmd += ["--orient", str(int(orient))]
    cmd += [
        input_stl,
        "--slice", "1",
        "--export-3mf", out3mf,
        "--export-slicedata", slicedata,
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "cmd": " ".join(cmd),
        "stdout_tail": (proc.stdout or "")[-600:],
        "stderr_tail": (proc.stderr or "")[-600:],
        "out_3mf": out3mf if proc.returncode == 0 and os.path.exists(out3mf) else None,
        "slicedata_dir": slicedata if os.path.isdir(slicedata) else None,
    }

# -------------------- Routes: UI & Env --------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return f"""<!doctype html><html lang="de"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{API_TITLE}</title>
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem}}
button,a.button{{padding:.6rem 1rem;border-radius:.6rem;border:1px solid #ccc;background:#fafafa;text-decoration:none;color:#111}}
.row{{display:flex;gap:.5rem;flex-wrap:wrap;margin:.6rem 0}} .card{{border:1px solid #eee;border-radius:12px;padding:1rem;margin:1rem 0}}
label{{display:block;margin:.3rem 0 .1rem}} input,select{{padding:.4rem;border:1px solid #ccc;border-radius:.4rem}}</style></head>
<body><h1>{API_TITLE}</h1><p>Version: {VERSION}</p>
<div class="row">
  <a class="button" href="/health" target="_blank">Health</a>
  <a class="button" href="/slicer_env" target="_blank">Slicer-Env</a>
  <a class="button" href="/docs" target="_blank">Swagger (API)</a>
</div>
<div class="card"><h3>Test: /slice_check</h3>
<form action="/slice_check" method="post" enctype="multipart/form-data" target="_blank">
  <label>STL-Datei</label><input type="file" name="file" required />
  <div class="row">
    <div><label>Unit</label><select name="unit"><option>mm</option><option>cm</option><option>m</option></select></div>
    <div><label>Material</label><select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select></div>
    <div><label>Infill (0..1)</label><input name="infill" type="number" step="0.01" min="0" max="1" value="0.35"/></div>
    <div><label>Arrange</label><select name="arrange"><option>1</option><option>0</option></select></div>
    <div><label>Orient</label><select name="orient"><option>1</option><option>0</option></select></div>
    <div><label>Debug</label><select name="debug"><option>1</option><option>0</option></select></div>
  </div>
  <div style="margin-top:.6rem"><button type="submit">Slicen</button></div>
</form></div>
</body></html>"""

@app.get("/health")
def health():
    return {"ok": True, "service": "fastapi", "version": VERSION}

@app.get("/slicer_env")
def slicer_env():
    orca = _which_orca()
    ret = None
    help_snippet = None
    if orca:
        p = subprocess.run([orca, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ret = p.returncode
        help_snippet = (p.stdout or "")[:900]
    profiles = {
        "printer": [os.path.join(PRINTERS_DIR, n) for n in sorted(os.listdir(PRINTERS_DIR))] if os.path.isdir(PRINTERS_DIR) else [],
        "process": [os.path.join(PROCESS_DIR, n) for n in sorted(os.listdir(PROCESS_DIR))] if os.path.isdir(PROCESS_DIR) else [],
        "filament": [os.path.join(FILAMENTS_DIR, n) for n in sorted(os.listdir(FILAMENTS_DIR))] if os.path.isdir(FILAMENTS_DIR) else [],
    }
    return {
        "ok": bool(orca),
        "slicer_bin": orca,
        "slicer_present": bool(orca),
        "return_code": ret,
        "help_snippet": help_snippet,
        "profiles": profiles,
    }

# -------------------- Business endpoints --------------------

@app.post("/analyze", response_model=AnalyzeOut)
async def analyze(file: UploadFile = File(...), unit: Literal["mm","cm","m"] = Form("mm")):
    data = await file.read()
    res = _analyze_mesh_bytes(data, unit=unit)
    model_id = _hash_bytes(data)
    return AnalyzeOut(model_id=model_id, volume_mm3=_safe_float3(res["volume_mm3"]), volume_cm3=_safe_float3(res["volume_cm3"]))

@app.post("/weight_direct", response_model=WeightDirectOut)
def weight_direct(payload: WeightDirectIn):
    vol_cm3 = payload.volume_mm3 / 1000.0
    density = MATERIAL_DENSITY[payload.material]
    weight = vol_cm3 * density * float(max(0.0, min(1.0, payload.infill)))
    return WeightDirectOut(weight_g=_safe_float3(weight))

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: Literal["mm","cm","m"] = Form("mm"),
    material: Literal["PLA","PETG","ASA","PC"] = Form("PLA"),
    infill: float = Form(0.35),
    arrange: int = Form(1),
    orient: int = Form(1),
    debug: int = Form(0),
):
    orca = _which_orca()
    if not orca:
        raise HTTPException(status_code=500, detail="OrcaSlicer binary nicht gefunden")

    data = await file.read()

    # leichte Voranalyse (Skalierung/Validierung). Misslingt sie, versuchen wir trotzdem zu slicen.
    try: _ = _analyze_mesh_bytes(data, unit=unit)
    except Exception: pass

    # Basisprofile (falls vorhanden)
    base_machine_json = _load_profile_or_none(PRINTERS_DIR)
    base_process_json = _load_profile_or_none(PROCESS_DIR)

    # Maschine IMMER hart normalisieren (richtige Typen!)
    machine_norm = _normalize_machine(base_machine_json)

    # Filament (optional)
    filament_path = _load_filament_for_material(material)

    attempts: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="fixedp_") as work:
        # Eingabedatei
        input_path = os.path.join(work, "input_model.stl")
        with open(input_path, "wb") as f:
            f.write(data)

        # Maschine schreiben
        mach_path = os.path.join(work, "printer_hardened.json")
        with open(mach_path, "w", encoding="utf-8") as f:
            json.dump(machine_norm, f, ensure_ascii=False)

        # --- Stufe 1: RELAXED (compatible_printers=["*"])
        proc_relaxed = _normalize_process(base_process_json, infill=infill, machine_ref=machine_norm, mode="relaxed")
        proc1_path = os.path.join(work, "process_relaxed.json")
        with open(proc1_path, "w", encoding="utf-8") as f:
            json.dump(proc_relaxed, f, ensure_ascii=False)

        r1 = _run_orca(orca, work, input_path, mach_path, proc1_path, filament_path, arrange, orient, debug)
        if r1["ok"]:
            return {"ok": True, "cmd": r1["cmd"], "out_3mf": r1["out_3mf"], "slicedata_dir": r1["slicedata_dir"]}

        attempts.append({"tag": "relaxed", **{k: r1[k] for k in ("code","stdout_tail","stderr_tail","cmd")}})

        # --- Stufe 2: NAMED (zusätzlich Druckername in kompatiblen Listen)
        proc_named = _normalize_process(base_process_json, infill=infill, machine_ref=machine_norm, mode="named")
        proc2_path = os.path.join(work, "process_named.json")
        with open(proc2_path, "w", encoding="utf-8") as f:
            json.dump(proc_named, f, ensure_ascii=False)

        r2 = _run_orca(orca, work, input_path, mach_path, proc2_path, filament_path, arrange, orient, debug)
        if r2["ok"]:
            return {"ok": True, "cmd": r2["cmd"], "out_3mf": r2["out_3mf"], "slicedata_dir": r2["slicedata_dir"]}
        attempts.append({"tag": "named", **{k: r2[k] for k in ("code","stdout_tail","stderr_tail","cmd")}})

        # --- Stufe 3: BOUND (Process trägt Model/Variant/Nozzle etc.)
        proc_bound = _normalize_process(base_process_json, infill=infill, machine_ref=machine_norm, mode="bound")
        proc3_path = os.path.join(work, "process_bound.json")
        with open(proc3_path, "w", encoding="utf-8") as f:
            json.dump(proc_bound, f, ensure_ascii=False)

        r3 = _run_orca(orca, work, input_path, mach_path, proc3_path, filament_path, arrange, orient, debug)
        if r3["ok"]:
            return {"ok": True, "cmd": r3["cmd"], "out_3mf": r3["out_3mf"], "slicedata_dir": r3["slicedata_dir"]}
        attempts.append({"tag": "bound", **{k: r3[k] for k in ("code","stdout_tail","stderr_tail","cmd")}})

        # Alles fehlgeschlagen → Fehler inkl. Profile zurückgeben
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Slicing fehlgeschlagen.",
                "machine_json": json.dumps(machine_norm),
                "process_relaxed_json": json.dumps(proc_relaxed),
                "process_named_json": json.dumps(proc_named),
                "process_bound_json": json.dumps(proc_bound),
                "filament_used": filament_path,
                "attempts": attempts,
            },
        )

# Hinweis: Render startet Uvicorn typischerweise über Procfile/CMD:
# uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
