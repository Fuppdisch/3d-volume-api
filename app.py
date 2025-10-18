# app.py
import os
import io
import json
import hashlib
import shutil
import tempfile
import subprocess
from typing import Optional, Literal, Dict, Any, List

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

API_TITLE = "Online 3D-Druck – Slicer API"
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

ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")

MATERIAL_DENSITY = {"PLA": 1.25, "PETG": 1.26, "ASA": 1.08, "PC": 1.20}

app = FastAPI(title=API_TITLE, version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Schemas ----------

class AnalyzeOut(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float
    watertight: bool

class WeightDirectIn(BaseModel):
    volume_mm3: float
    material: Literal["PLA","PETG","ASA","PC"]
    infill: float  # 0..1

class WeightDirectOut(BaseModel):
    weight_g: float

# ---------- Helpers ----------

def _which_orca() -> Optional[str]:
    for cand in ORCA_CANDIDATES:
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    return None

def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _safe3(x: float) -> float:
    return float(f"{x:.3f}")

def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _first_file(d: str, exts: List[str]) -> Optional[str]:
    if not os.path.isdir(d): return None
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if os.path.isfile(p) and any(n.lower().endswith(e) for e in exts):
            return p
    return None

def _load_filament_path(material: str) -> Optional[str]:
    # Versuch: passendes vorhandenes JSON aus /app/profiles/filaments
    if os.path.isdir(FILAMENTS_DIR):
        for n in sorted(os.listdir(FILAMENTS_DIR)):
            p = os.path.join(FILAMENTS_DIR, n)
            if os.path.isfile(p) and n.lower().endswith(".json") and material.lower() in n.lower():
                return p
    return None

# ---------- Volume (non-fatal) ----------

def _analyze_mesh(data: bytes, unit: Literal["mm","cm","m"]) -> Dict[str, Any]:
    import trimesh
    mesh = None
    try:
        mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    except Exception:
        mesh = trimesh.load(io.BytesIO(data), force="mesh")

    if not isinstance(mesh, trimesh.Trimesh):
        if hasattr(mesh, "dump"):
            mesh = mesh.dump().sum()

    # scale to mm
    if unit == "cm": mesh.apply_scale(10.0)
    elif unit == "m": mesh.apply_scale(1000.0)

    try:
        trimesh.repair.fix_normals(mesh)
    except Exception:
        pass

    vol = None
    if getattr(mesh, "is_watertight", False):
        try: vol = float(mesh.volume)
        except Exception: vol = None

    if not vol or vol <= 0:
        try:
            vol = float(mesh.convex_hull.volume)
        except Exception:
            # Last resort: voxel approximation
            try:
                vox = mesh.voxelized(pitch=0.6)
                vol = float(vox.points.shape[0]) * (0.6 ** 3)
            except Exception:
                raise HTTPException(status_code=422, detail="Volumen konnte nicht bestimmt werden")

    return {
        "volume_mm3": vol,
        "volume_cm3": vol / 1000.0,
        "watertight": bool(getattr(mesh, "is_watertight", False)),
    }

# ---------- INI builders (robust for Orca/Prusa schema) ----------

def _make_printer_ini(name: str = "RatRig V-Core 4 400 0.4 nozzle") -> str:
    # Bewährte Minimalmenge an Keys; Zahlen als Zahlen, bed_shape als "x"-Paare
    return (
        "printer_technology = FFF\n"
        "gcode_flavor = marlin\n"
        f"printer_notes = Auto-generated for {name}\n"
        "bed_shape = 0x0,400x0,400x400,0x400\n"
        "max_print_height = 300.0\n"
        "nozzle_diameter = 0.4\n"
        "extruders = 1\n"
        "use_firmware_retraction = 0\n"
    )

def _make_process_ini(layer_h="0.2", first_layer="0.3", line_w="0.45", infill_pct: float = 0.35) -> str:
    # fill_density erwartet Zahl (0-100), NICHT "35%"
    try:
        fill_density = max(0, min(100, int(round(infill_pct * 100))))
    except Exception:
        fill_density = 35
    return (
        f"layer_height = {layer_h}\n"
        f"first_layer_height = {first_layer}\n"
        f"fill_density = {fill_density}\n"
        f"perimeter_extrusion_width = {line_w}\n"
        f"external_perimeter_extrusion_width = {line_w}\n"
        f"infill_extrusion_width = {line_w}\n"
        "perimeters = 2\n"
        "top_solid_layers = 3\n"
        "bottom_solid_layers = 3\n"
        "perimeter_speed = 250\n"
        "external_perimeter_speed = 250\n"
        "infill_speed = 350\n"
        "travel_speed = 500\n"
        "avoid_crossing_perimeters = 1\n"
        "z_seam_type = aligned\n"
    )

def _make_filament_ini(material: str = "PLA") -> str:
    # Minimal sicherer Satz
    if material.upper() == "PLA":
        nozzle, bed = 200, 0
        first_nozzle, first_bed = 205, 0
        density = 1.25
        flow = 0.92
    elif material.upper() == "PETG":
        nozzle, bed = 240, 60
        first_nozzle, first_bed = 240, 60
        density = 1.27
        flow = 0.94
    elif material.upper() == "ASA":
        nozzle, bed = 245, 100
        first_nozzle, first_bed = 250, 100
        density = 1.08
        flow = 0.93
    else:  # PC
        nozzle, bed = 260, 110
        first_nozzle, first_bed = 265, 110
        density = 1.20
        flow = 0.93
    return (
        f"temperature = {nozzle}\n"
        f"first_layer_temperature = {first_nozzle}\n"
        f"bed_temperature = {bed}\n"
        f"first_layer_bed_temperature = {first_bed}\n"
        "filament_diameter = 1.75\n"
        f"filament_density = {density}\n"
        f"filament_flow_ratio = {flow}\n"
    )

def _make_machine_json_strict() -> dict:
    # Falls JSON nötig ist: strikt minimal, alle Nummern als Strings (um Typmeckern auszuweichen)
    return {
        "type": "machine",
        "version": "1",
        "from": "user",
        "name": "RatRig V-Core 4 400 0.4 nozzle",
        "printer_technology": "FFF",
        "gcode_flavor": "marlin",
        "bed_shape": ["0x0", "400x0", "400x400", "0x400"],
        "max_print_height": "300.0",
        "extruders": "1",
        "nozzle_diameter": ["0.4"],
    }

def _make_process_json_strict(infill_pct: float) -> dict:
    fill = max(0, min(100, int(round(float(infill_pct) * 100))))
    return {
        "type": "process",
        "version": "1",
        "from": "user",
        "name": "0.20mm Standard",
        "layer_height": "0.2",
        "first_layer_height": "0.3",
        "line_width": "0.45",
        "perimeter_extrusion_width": "0.45",
        "external_perimeter_extrusion_width": "0.45",
        "infill_extrusion_width": "0.45",
        "perimeters": "2",
        "top_solid_layers": "3",
        "bottom_solid_layers": "3",
        "sparse_infill_density": f"{fill}%",
        "compatible_printers": ["*"],
        "compatible_printers_condition": "",
    }

# ---------- Orca runner ----------

def _run_orca(orcapath: str, input_stl: str, settings_files: List[str], filament_file: Optional[str],
              work: str, arrange: int, orient: int, debug: int) -> Dict[str, Any]:
    out3mf = os.path.join(work, "out.3mf")
    slicedata = os.path.join(work, "slicedata")
    os.makedirs(slicedata, exist_ok=True)

    cmd = [XVFB, "-a", orcapath, "--debug", str(int(debug)), "--datadir", os.path.join(work, "cfg")]

    # Wichtig: JEDES Settings-File separat per --load-settings anhängen (robuster als Join per ;)
    for s in settings_files:
        cmd += ["--load-settings", s]

    if filament_file:
        cmd += ["--load-filaments", filament_file]

    cmd += ["--arrange", str(int(arrange)), "--orient", str(int(orient))]
    cmd += [input_stl, "--slice", "1", "--export-3mf", out3mf, "--export-slicedata", slicedata]

    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {
        "ok": p.returncode == 0,
        "code": p.returncode,
        "cmd": " ".join(cmd),
        "stdout_tail": (p.stdout or "")[-800:],
        "stderr_tail": (p.stderr or "")[-800:],
        "out_3mf": out3mf if (p.returncode == 0 and os.path.exists(out3mf)) else None,
        "slicedata_dir": slicedata if os.path.isdir(slicedata) else None,
    }

# ---------- UI ----------

@app.get("/", response_class=HTMLResponse)
def home():
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{API_TITLE}</title>
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
.card{{border:1px solid #eee;border-radius:12px;padding:1rem;margin:1rem 0}}
label{{display:block;margin:.4rem 0 .2rem}} input,select{{padding:.4rem;border:1px solid #ccc;border-radius:.4rem}}</style></head>
<body>
<h1>{API_TITLE}</h1><p>Version {VERSION}</p>
<p><a href="/docs">Swagger</a> · <a href="/slicer_env">Slicer-Env</a> · <a href="/health">Health</a></p>
<div class="card"><h3>Quick Test: /slice_check</h3>
<form action="/slice_check" method="post" enctype="multipart/form-data" target="_blank">
  <label>STL-Datei</label><input type="file" name="file" required>
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:.5rem">
    <div><label>Unit</label><select name="unit"><option>mm</option><option>cm</option><option>m</option></select></div>
    <div><label>Material</label><select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select></div>
    <div><label>Infill (0..1)</label><input type="number" min="0" max="1" step="0.01" name="infill" value="0.35"></div>
    <div><label>Arrange</label><select name="arrange"><option>1</option><option>0</option></select></div>
    <div><label>Orient</label><select name="orient"><option>1</option><option>0</option></select></div>
    <div><label>Debug</label><select name="debug"><option>0</option><option>1</option></select></div>
  </div>
  <div style="margin-top:.6rem"><button>Slicen</button></div>
</form></div>
</body></html>"""

@app.get("/health")
def health():
    return {"ok": True, "service": "fastapi", "version": VERSION}

@app.get("/slicer_env")
def slicer_env():
    orca = _which_orca()
    out = {"ok": bool(orca), "slicer_bin": orca, "slicer_present": bool(orca)}
    if orca:
        p = subprocess.run([orca, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out.update({"return_code": p.returncode, "help_snippet": (p.stdout or "")[:900]})
    out["profiles"] = {
        "printer": [os.path.join(PRINTERS_DIR, n) for n in sorted(os.listdir(PRINTERS_DIR))] if os.path.isdir(PRINTERS_DIR) else [],
        "process": [os.path.join(PROCESS_DIR, n) for n in sorted(os.listdir(PROCESS_DIR))] if os.path.isdir(PROCESS_DIR) else [],
        "filament": [os.path.join(FILAMENTS_DIR, n) for n in sorted(os.listdir(FILAMENTS_DIR))] if os.path.isdir(FILAMENTS_DIR) else [],
    }
    return out

# ---------- API ----------

@app.post("/analyze", response_model=AnalyzeOut)
async def analyze(file: UploadFile = File(...), unit: Literal["mm","cm","m"] = Form("mm")):
    data = await file.read()
    res = _analyze_mesh(data, unit)
    return AnalyzeOut(
        model_id=_hash_bytes(data),
        volume_mm3=_safe3(res["volume_mm3"]),
        volume_cm3=_safe3(res["volume_cm3"]),
        watertight=res["watertight"],
    )

@app.post("/weight_direct", response_model=WeightDirectOut)
def weight_direct(payload: WeightDirectIn):
    vol_cm3 = payload.volume_mm3 / 1000.0
    density = MATERIAL_DENSITY[payload.material]
    weight = vol_cm3 * density * max(0.0, min(1.0, float(payload.infill)))
    return WeightDirectOut(weight_g=_safe3(weight))

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
        raise HTTPException(status_code=500, detail="OrcaSlicer nicht gefunden")

    data = await file.read()

    # Voranalyse (best effort)
    try: _ = _analyze_mesh(data, unit)
    except Exception: pass

    filament_json_path = _load_filament_path(material)

    attempts: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="fixedp_") as work:
        input_stl = os.path.join(work, "input_model.stl")
        with open(input_stl, "wb") as f:
            f.write(data)

        # --- STUFE 1: INI RELAXED ---
        printer_ini = os.path.join(work, "printer.ini")
        process_ini = os.path.join(work, "process.ini")
        filament_ini = os.path.join(work, "filament.ini")

        with open(printer_ini, "w", encoding="utf-8") as f:
            f.write(_make_printer_ini())
        with open(process_ini, "w", encoding="utf-8") as f:
            f.write(_make_process_ini(infill_pct=infill))
        with open(filament_ini, "w", encoding="utf-8") as f:
            f.write(_make_filament_ini(material))

        r1 = _run_orca(
            orcapath=orca,
            input_stl=input_stl,
            settings_files=[printer_ini, process_ini],
            filament_file=filament_ini,
            work=work,
            arrange=arrange,
            orient=orient,
            debug=debug,
        )
        if r1["ok"]:
            return {"ok": True, "cmd": r1["cmd"], "out_3mf": r1["out_3mf"], "slicedata_dir": r1["slicedata_dir"]}
        attempts.append({"tag": "ini-relaxed", **{k: r1[k] for k in ("code","stdout_tail","stderr_tail","cmd")}})

        # --- STUFE 2: INI BOUND (bind: keine *, aber gleiche Minimalwerte) ---
        # Hier reichen die gleichen INIs – Prusa/Orca nutzt bei INI keine harte „compatible_printers“-Liste.
        # Wir justieren minimal Geschwindigkeiten konservativ.
        with open(process_ini, "w", encoding="utf-8") as f:
            f.write(_make_process_ini(layer_h="0.2", first_layer="0.3", line_w="0.45", infill_pct=infill))
        r2 = _run_orca(
            orcapath=orca,
            input_stl=input_stl,
            settings_files=[printer_ini, process_ini],
            filament_file=filament_ini,
            work=work,
            arrange=arrange,
            orient=orient,
            debug=debug,
        )
        if r2["ok"]:
            return {"ok": True, "cmd": r2["cmd"], "out_3mf": r2["out_3mf"], "slicedata_dir": r2["slicedata_dir"]}
        attempts.append({"tag": "ini-bound", **{k: r2[k] for k in ("code","stdout_tail","stderr_tail","cmd")}})

        # --- STUFE 3: JSON STRICT (nur wenn INI partout nicht geht) ---
        mach_json = os.path.join(work, "machine.json")
        proc_json = os.path.join(work, "process.json")
        with open(mach_json, "w", encoding="utf-8") as f:
            json.dump(_make_machine_json_strict(), f, ensure_ascii=False)
        with open(proc_json, "w", encoding="utf-8") as f:
            json.dump(_make_process_json_strict(infill), f, ensure_ascii=False)

        r3 = _run_orca(
            orcapath=orca,
            input_stl=input_stl,
            settings_files=[mach_json, proc_json],
            filament_file=filament_json_path,  # JSON- oder INI-Filament ist optional; JSON hier ok
            work=work,
            arrange=arrange,
            orient=orient,
            debug=debug,
        )
        if r3["ok"]:
            return {"ok": True, "cmd": r3["cmd"], "out_3mf": r3["out_3mf"], "slicedata_dir": r3["slicedata_dir"]}
        attempts.append({"tag": "json-strict", **{k: r3[k] for k in ("code","stdout_tail","stderr_tail","cmd")}})

        raise HTTPException(
            status_code=500,
            detail={
                "message": "Slicing fehlgeschlagen.",
                "attempts": attempts,
                "ini_printer_preview": (open(printer_ini, "r", encoding="utf-8").read()[:400] if os.path.exists(printer_ini) else None),
                "ini_process_preview": (open(process_ini, "r", encoding="utf-8").read()[:400] if os.path.exists(process_ini) else None),
                "ini_filament_preview": (open(filament_ini, "r", encoding="utf-8").read()[:400] if os.path.exists(filament_ini) else None),
                "json_machine_preview": (_make_machine_json_strict()),
                "json_process_preview": (_make_process_json_strict(infill)),
                "filament_json_used": filament_json_path,
            },
        )

# uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
