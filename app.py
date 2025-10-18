import os
import io
import json
import hashlib
import tempfile
import subprocess
from typing import Tuple, List, Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

# -------------------------
# Helpers
# -------------------------

def env(name: str, default: str) -> str:
    return os.environ.get(name, default)

ORCA_BIN = env("SLICER_BIN", "/opt/orca/bin/orca-slicer")

def run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 600) -> Tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""

def tail_text(s: str, k: int = 2048) -> str:
    if not s:
        return ""
    return s[-k:]

def write_text(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

def write_json(path: str, data: Dict[str, Any]):
    write_text(path, json.dumps(data, ensure_ascii=False))

def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256(); h.update(data); return h.hexdigest()

# -------------------------
# Profile builders (robust)
# -------------------------

def build_machine_json(name: str = "Generic 400x400 0.4 nozzle") -> Dict[str, Any]:
    # Minimal, typ-sauber, nur Felder die Orca sicher kennt/pars’t
    return {
        "type": "machine",
        "version": "1",
        "from": "user",
        "name": name,
        "printer_technology": "FFF",
        "gcode_flavor": "marlin",
        "printer_model": "Generic 400x400",
        "printer_variant": "0.4",
        "bed_shape": [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]],
        "max_print_height": 300.0,
        "min_layer_height": 0.06,
        "max_layer_height": 0.32,
        "extruders": 1,
        "nozzle_diameter": [0.4],
    }

def build_process_json(printer_name: str,
                       layer_height: float = 0.2,
                       first_layer_height: float = 0.3,
                       infill_percent: int = 35) -> Dict[str, Any]:
    return {
        "type": "process",
        "version": "1",
        "from": "user",
        "name": "API process",
        "layer_height": str(layer_height),
        "first_layer_height": str(first_layer_height),
        "sparse_infill_density": f"{infill_percent}%",
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
        # harte Bindung + Wildcard, damit Kompat-Check nicht blockiert
        "compatible_printers": [printer_name, "*"],
        "compatible_printers_condition": "",
        # Spiegel-Felder, die Orca manchmal erwartet
        "printer_technology": "FFF",
        "printer_model": "Generic 400x400",
        "printer_variant": "0.4",
        "nozzle_diameter": [0.4],
    }

def build_filament_json(printer_name: str,
                        material: str = "PLA") -> Dict[str, Any]:
    mats = {
        "PLA":  dict(noz=200, noz0=205, bed=0,   bed0=0,   dens=1.24, flow=0.95),
        "PETG": dict(noz=240, noz0=245, bed=60,  bed0=60,  dens=1.27, flow=0.94),
        "ASA":  dict(noz=250, noz0=255, bed=100, bed0=100, dens=1.10, flow=0.93),
        "PC":   dict(noz=260, noz0=265, bed=110, bed0=110, dens=1.04, flow=0.93),
    }
    m = mats.get(material.upper(), mats["PLA"])
    return {
        "type": "filament",
        "version": "1",
        "from": "user",
        "name": f"Generic {material.upper()}",
        "filament_diameter": ["1.75"],
        "filament_density": [f"{m['dens']}"],
        "filament_flow_ratio": [f"{m['flow']}"],
        "nozzle_temperature": [f"{m['noz']}"],
        "nozzle_temperature_initial_layer": [f"{m['noz0']}"],
        "bed_temperature": [f"{m['bed']}"],
        "bed_temperature_initial_layer": [f"{m['bed0']}"],
        "compatible_printers": [printer_name],
        "compatible_printers_condition": "",
        # Spiegel-Felder
        "printer_technology": "FFF",
        "printer_model": "Generic 400x400",
        "printer_variant": "0.4",
        "nozzle_diameter": [0.4],
    }

def build_ini_profiles(work: str, material: str = "PLA") -> Tuple[str, str, str]:
    printer_ini = (
        "printer_technology = FFF\n"
        "gcode_flavor = marlin\n"
        "printer_notes = Minimal 400x400 0.4\n"
        "bed_shape = 0x0,400x0,400x400,0x400\n"
        "max_print_height = 300\n"
        "nozzle_diameter = 0.4\n"
        "extruders = 1\n"
        "use_firmware_retraction = 0\n"
    )
    process_ini = (
        "layer_height = 0.2\n"
        "first_layer_height = 0.3\n"
        "fill_density = 35\n"
        "perimeter_extrusion_width = 0.45\n"
        "external_perimeter_extrusion_width = 0.45\n"
        "infill_extrusion_width = 0.45\n"
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
    mats = {
        "PLA":  dict(temp=200, temp0=205, bed=0,   bed0=0,   dens=1.24, flow=0.95),
        "PETG": dict(temp=240, temp0=245, bed=60,  bed0=60,  dens=1.27, flow=0.94),
        "ASA":  dict(temp=250, temp0=255, bed=100, bed0=100, dens=1.10, flow=0.93),
        "PC":   dict(temp=260, temp0=265, bed=110, bed0=110, dens=1.04, flow=0.93),
    }
    m = mats.get(material.upper(), mats["PLA"])
    filament_ini = (
        f"temperature = {m['temp']}\n"
        f"first_layer_temperature = {m['temp0']}\n"
        f"bed_temperature = {m['bed']}\n"
        f"first_layer_bed_temperature = {m['bed0']}\n"
        "filament_diameter = 1.75\n"
        f"filament_density = {m['dens']}\n"
        f"filament_flow_ratio = {m['flow']}\n"
    )
    p1 = os.path.join(work, "printer.ini")
    p2 = os.path.join(work, "process.ini")
    p3 = os.path.join(work, "filament.ini")
    write_text(p1, printer_ini)
    write_text(p2, process_ini)
    write_text(p3, filament_ini)
    return p1, p2, p3

# -------------------------
# Slicing attempts
# -------------------------

def cmd_slice_json(orca: str, datadir: str, printer: str, process: str, filament: str,
                   model: str, arrange_orient: bool, no_check: bool) -> List[str]:
    cmd = ["xvfb-run","-a",orca,"--debug","0","--datadir",datadir,
           "--load-settings", f"{printer};{process}",
           "--load-filaments", filament]
    if arrange_orient:
        cmd += ["--arrange","1","--orient","1"]
    if no_check:
        cmd += ["--no-check"]
    out3mf = os.path.join(os.path.dirname(datadir), "out.3mf")
    sdir = os.path.join(os.path.dirname(datadir), "slicedata")
    cmd += [model,"--slice","1","--export-3mf",out3mf,"--export-slicedata",sdir]
    return cmd

def cmd_slice_ini(orca: str, datadir: str, printer_ini: str, process_ini: str, filament_ini: str,
                  model: str, arrange_orient: bool, no_check: bool) -> List[str]:
    cmd = ["xvfb-run","-a",orca,"--debug","0","--datadir",datadir,
           "--load-settings", printer_ini,
           "--load-settings", process_ini,
           "--load-filaments", filament_ini]
    if arrange_orient:
        cmd += ["--arrange","1","--orient","1"]
    if no_check:
        cmd += ["--no-check"]
    out3mf = os.path.join(os.path.dirname(datadir), "out.3mf")
    sdir = os.path.join(os.path.dirname(datadir), "slicedata")
    cmd += [model,"--slice","1","--export-3mf",out3mf,"--export-slicedata",sdir]
    return cmd

# -------------------------
# FastAPI
# -------------------------

app = FastAPI(title="OrcaSlicer API (robust)")

@app.get("/health")
def health():
    present = os.path.exists(ORCA_BIN)
    return {"ok": present, "slicer_bin": ORCA_BIN}

@app.post("/analyze_upload")
async def analyze_upload(file: UploadFile = File(...)):
    data = await file.read()
    return {
        "ok": True,
        "filename": file.filename,
        "bytes": len(data),
        "sha256": sha256_bytes(data)
    }

@app.post("/slice")
async def slice_endpoint(
    file: UploadFile = File(...),
    material: str = Form("PLA"),
    infill: int = Form(35),
    use_repo_profiles: bool = Form(False),
    layer_height: float = Form(0.2),
    first_layer_height: float = Form(0.3),
):
    # read model
    model_bytes = await file.read()
    if not model_bytes:
        return JSONResponse(status_code=400, content={"ok": False, "detail": "empty file"})

    work = tempfile.mkdtemp(prefix="fixedp_")
    input_stl = os.path.join(work, "input.stl")
    with open(input_stl, "wb") as f:
        f.write(model_bytes)

    # datadir must be a directory; Orca writes cache there
    cfg = os.path.join(work, "cfg")
    os.makedirs(cfg, exist_ok=True)

    attempts = []
    final_code = 0

    # ---------- Build JSON profiles (either repo or generated) ----------
    printer_name = "Generic 400x400 0.4 nozzle"

    if use_repo_profiles:
        # Expect repo paths via env or defaults
        repo_prn = env("REPO_PRINTER", "/app/profiles/printers/X1C.json")
        repo_pro = env("REPO_PROCESS", "/app/profiles/process/0.20mm_standard.json")
        repo_fil = env("REPO_FILAMENT", f"/app/profiles/filaments/{material.upper()}.json")
        printer_json_path = repo_prn
        process_json_path = repo_pro
        filament_json_path = repo_fil
    else:
        # internally generated, type-safe
        printer_json = build_machine_json(printer_name)
        process_json = build_process_json(printer_name, layer_height, first_layer_height, infill)
        filament_json = build_filament_json(printer_name, material)

        printer_json_path = os.path.join(work, "printer.json")
        process_json_path = os.path.join(work, "process.json")
        filament_json_path = os.path.join(work, "filament.json")
        write_json(printer_json_path, printer_json)
        write_json(process_json_path, process_json)
        write_json(filament_json_path, filament_json)

    # ---------- Pass 1: JSON normal ----------
    cmd = cmd_slice_json(ORCA_BIN, cfg, printer_json_path, process_json_path, filament_json_path,
                         input_stl, arrange_orient=True, no_check=False)
    code, out, err = run(cmd, cwd=work, timeout=420)
    attempts.append({
        "try": "json-normal",
        "code": code,
        "cmd": " ".join(cmd),
        "stdout_tail": tail_text(out),
        "stderr_tail": tail_text(err)
    })
    final_code = code

    # ---------- Pass 2: JSON no-check ----------
    if final_code != 0:
        cmd = cmd_slice_json(ORCA_BIN, cfg, printer_json_path, process_json_path, filament_json_path,
                             input_stl, arrange_orient=False, no_check=True)
        code, out, err = run(cmd, cwd=work, timeout=420)
        attempts.append({
            "try": "json-no-check",
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": tail_text(out),
            "stderr_tail": tail_text(err)
        })
        final_code = code

    # ---------- Build INI and try ----------
    ini_files = None
    if final_code != 0:
        ini_files = build_ini_profiles(work, material)
        prn_ini, pro_ini, fil_ini = ini_files

        # Pass 3: INI normal
        cmd = cmd_slice_ini(ORCA_BIN, cfg, prn_ini, pro_ini, fil_ini,
                            input_stl, arrange_orient=True, no_check=False)
        code, out, err = run(cmd, cwd=work, timeout=420)
        attempts.append({
            "try": "ini-normal",
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": tail_text(out),
            "stderr_tail": tail_text(err)
        })
        final_code = code

    # ---------- Pass 4: INI no-check ----------
    if final_code != 0 and ini_files:
        prn_ini, pro_ini, fil_ini = ini_files
        cmd = cmd_slice_ini(ORCA_BIN, cfg, prn_ini, pro_ini, fil_ini,
                            input_stl, arrange_orient=False, no_check=True)
        code, out, err = run(cmd, cwd=work, timeout=420)
        attempts.append({
            "try": "ini-no-check",
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": tail_text(out),
            "stderr_tail": tail_text(err)
        })
        final_code = code

    # ---------- Response ----------
    if final_code == 0:
        return {"ok": True, "message": "Slicing erfolgreich.", "workdir": work}

    # Include previews to help debug (only when we generated them)
    previews = {}
    try:
        if not use_repo_profiles:
            with open(printer_json_path, "r", encoding="utf-8") as f:
                previews["printer_preview"] = json.loads(f.read())
            with open(process_json_path, "r", encoding="utf-8") as f:
                previews["process_preview"] = json.loads(f.read())
            with open(filament_json_path, "r", encoding="utf-8") as f:
                previews["filament_preview"] = json.loads(f.read())
    except Exception:
        pass

    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "message": "Slicing fehlgeschlagen.",
            "attempts": attempts,
            **({"previews": previews} if previews else {})
        }
    )
