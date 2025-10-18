# app.py
import os
import io
import json
import hashlib
import tempfile
import shutil
import subprocess
from typing import Literal, Dict, Any, List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

API_TITLE = "Slicer API (OrcaSlicer JSON-only)"
VERSION = "2025-10-18"

ORCA_CANDIDATES = [
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "orca-slicer",
]
XVFB = os.environ.get("XVFB_BIN", "/usr/bin/xvfb-run")

PROFILES_ROOT = os.environ.get("PROFILES_ROOT", "/app/profiles")
FILAMENTS_DIR = os.path.join(PROFILES_ROOT, "filaments")

ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")

app = FastAPI(title=API_TITLE, version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- helpers -----------------

def _which_orca() -> Optional[str]:
    for cand in ORCA_CANDIDATES:
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    return None

def _sha256(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()

def _load_filament_json(material: str) -> Optional[str]:
    """Nimmt bevorzugt ein vorhandenes Filament-Profil aus /app/profiles/filaments."""
    if not os.path.isdir(FILAMENTS_DIR):
        return None
    want = material.lower()
    picks = sorted(os.listdir(FILAMENTS_DIR))
    # bevorzugt exakter Name (PLA.json), dann Teiltreffer
    for n in picks:
        if n.lower() == f"{want}.json":
            return os.path.join(FILAMENTS_DIR, n)
    for n in picks:
        if n.lower().endswith(".json") and want in n.lower():
            return os.path.join(FILAMENTS_DIR, n)
    # letzte Chance: irgendein Filament
    for n in picks:
        if n.lower().endswith(".json"):
            return os.path.join(FILAMENTS_DIR, n)
    return None

# ----------------- JSON profile builders -----------------

def make_machine_json() -> dict:
    """
    Minimales, robustes Machine-Profil NUR mit Feldern,
    die diese Orca-Version sicher akzeptiert.
    Alle heiklen Werte **als Strings**, nozzle_diameter als String-Array.
    bed_shape als 'x'-Paare (Strings) – entspricht der CLI-Hilfe.
    """
    return {
        "type": "machine",
        "version": "1",
        "from": "user",
        "name": "RatRig V-Core 4 400 0.4 nozzle",
        "printer_technology": "FFF",
        "gcode_flavor": "marlin",
        "bed_shape": ["0x0", "400x0", "400x400", "0x400"],
        "max_print_height": "300.0",
        "min_layer_height": "0.06",
        "max_layer_height": "0.3",
        "extruders": "1",
        "nozzle_diameter": ["0.4"],
        # folgende drei helfen manchen Builds bei der Kompat.-Prüfung:
        "printer_model": "RatRig V-Core 4 400",
        "printer_variant": "0.4"
    }

def make_process_json(infill: float) -> dict:
    """
    Sehr neutrales Process-Profil.
    - fill als 'NN%' String, wie in deinen Logs verwendet.
    - compatible_printers = ["*"] auf minimalen Konflikt getrimmt.
    """
    fill_pct = max(0, min(100, int(round(float(infill) * 100))))
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
        "outer_wall_speed": "250",
        "inner_wall_speed": "350",
        "travel_speed": "500",
        "before_layer_gcode": "",
        "layer_gcode": "",
        "toolchange_gcode": "",
        "printing_by_object_gcode": "",
        "sparse_infill_density": f"{fill_pct}%",
        "compatible_printers": ["*"],
        "compatible_printers_condition": ""
    }

# ----------------- orca runner -----------------

def run_orca(orca: str, workdir: str, stl_path: str,
             machine_json: str, process_json: str,
             filament_json: Optional[str],
             arrange: int, orient: int, debug: int) -> Dict[str, Any]:
    out3mf = os.path.join(workdir, "out.3mf")
    slicedata = os.path.join(workdir, "slicedata")
    os.makedirs(os.path.join(workdir, "cfg"), exist_ok=True)
    os.makedirs(slicedata, exist_ok=True)

    cmd = [
        XVFB, "-a", orca,
        "--debug", str(int(debug)),
        "--datadir", os.path.join(workdir, "cfg"),
        # sehr wichtig: JEDE Datei mit eigenem --load-settings
        "--load-settings", machine_json,
        "--load-settings", process_json,
    ]
    if filament_json:
        cmd += ["--load-filaments", filament_json]

    cmd += ["--arrange", str(int(arrange)), "--orient", str(int(orient))]
    cmd += [stl_path, "--slice", "1", "--export-3mf", out3mf, "--export-slicedata", slicedata]

    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {
        "ok": p.returncode == 0,
        "code": p.returncode,
        "cmd": " ".join(cmd),
        "stdout_tail": (p.stdout or "")[-1200:],
        "stderr_tail": (p.stderr or "")[-1200:],
        "out_3mf": out3mf if (p.returncode == 0 and os.path.exists(out3mf)) else None,
        "slicedata_dir": slicedata if os.path.isdir(slicedata) else None,
    }

# ----------------- routes -----------------

@app.get("/", response_class=HTMLResponse)
def root():
    return f"""<!doctype html><meta charset=utf-8>
<title>{API_TITLE}</title>
<body style="font-family:system-ui;max-width:900px;margin:2rem auto">
<h1>{API_TITLE}</h1><p>Version {VERSION}</p>
<p><a href="/docs">Swagger</a> · <a href="/slicer_env">Slicer-Env</a> · <a href="/health">Health</a></p>
<form action="/slice" method="post" enctype="multipart/form-data" target="_blank" style="border:1px solid #ddd;padding:1rem;border-radius:10px">
  <div><label>STL</label><input type="file" name="file" required></div>
  <div style="display:flex;gap:10px;margin-top:.5rem;flex-wrap:wrap">
    <label>Material
      <select name="material">
        <option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option>
      </select>
    </label>
    <label>Infill (0..1)
      <input type="number" name="infill" step="0.01" min="0" max="1" value="0.35">
    </label>
    <label>Arrange <select name="arrange"><option>1</option><option>0</option></select></label>
    <label>Orient <select name="orient"><option>1</option><option>0</option></select></label>
    <label>Debug <select name="debug"><option>0</option><option>1</option></select></label>
  </div>
  <div style="margin-top:.6rem"><button>Slicen</button></div>
</form>
</body>"""

@app.get("/health")
def health():
    return {"ok": True, "version": VERSION}

@app.get("/slicer_env")
def slicer_env():
    orca = _which_orca()
    out = {"ok": bool(orca), "slicer_bin": orca}
    if orca:
        p = subprocess.run([orca, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out.update({
            "return_code": p.returncode,
            "help_snippet": (p.stdout or "")[:1200]
        })
    return out

@app.post("/slice")
async def slice_endpoint(
    file: UploadFile = File(...),
    material: Literal["PLA","PETG","ASA","PC"] = Form("PLA"),
    infill: float = Form(0.35),
    arrange: int = Form(1),
    orient: int = Form(1),
    debug: int = Form(0),
):
    orca = _which_orca()
    if not orca:
        raise HTTPException(status_code=500, detail="orca-slicer nicht gefunden")

    stl_bytes = await file.read()
    if not stl_bytes:
        raise HTTPException(status_code=400, detail="leere Datei")

    filament_json = _load_filament_json(material)

    with tempfile.TemporaryDirectory(prefix="fixedp_") as work:
        stl_path = os.path.join(work, "input_model.stl")
        with open(stl_path, "wb") as f:
            f.write(stl_bytes)

        # Profile schreiben (JSON-only)
        machine_path = os.path.join(work, "printer.json")
        process_path = os.path.join(work, "process.json")
        with open(machine_path, "w", encoding="utf-8") as f:
            json.dump(make_machine_json(), f, ensure_ascii=False)
        with open(process_path, "w", encoding="utf-8") as f:
            json.dump(make_process_json(infill), f, ensure_ascii=False)

        res = run_orca(
            orca=orca, workdir=work, stl_path=stl_path,
            machine_json=machine_path, process_json=process_path,
            filament_json=filament_json,
            arrange=arrange, orient=orient, debug=debug
        )
        if res["ok"]:
            return {"ok": True, "cmd": res["cmd"], "out_3mf": res["out_3mf"], "slicedata_dir": res["slicedata_dir"]}

        # Fehler -> alles transparent zurückgeben
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Slicing fehlgeschlagen.",
                "cmd": res["cmd"],
                "code": res["code"],
                "stdout_tail": res["stdout_tail"],
                "stderr_tail": res["stderr_tail"],
                "machine_json": make_machine_json(),
                "process_json": make_process_json(infill),
                "filament_used": filament_json,
            }
        )

# Start: uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
