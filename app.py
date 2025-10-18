# app.py
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import tempfile
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Union

APP_PROFILES_ROOT = "/app/profiles"
PRINTER_DIR = os.path.join(APP_PROFILES_ROOT, "printer")
PROCESS_DIR = os.path.join(APP_PROFILES_ROOT, "process")
FILAMENT_DIR = os.path.join(APP_PROFILES_ROOT, "filament")
BUNDLE_FILE = os.path.join(APP_PROFILES_ROOT, "bundle_structure.json")

ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/bin/orca-slicer")

app = FastAPI(title="OrcaSlicer API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Helpers
# ---------------------------

def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(data: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _as_str(x: Union[int, float, str]) -> str:
    # normalize numeric → string
    if isinstance(x, (int, float)):
        # keep one decimal for floats like 300.0
        s = f"{x}"
        if "." not in s:
            s = f"{x:.1f}"
        return s
    return str(x)

def _ensure_float_pairs_bed_shape(machine: Dict[str, Any]) -> None:
    """
    Accepts:
      bed_shape as [[0.0,0.0], [400.0,0.0], [400.0,400.0], [0.0,400.0]]
      OR printable_area ["0x0","400x0","400x400","0x400"]
    Produces machine["bed_shape"] as list[list[float,float]].
    """
    if "bed_shape" in machine and isinstance(machine["bed_shape"], list):
        ok = True
        out = []
        for p in machine["bed_shape"]:
            if isinstance(p, list) and len(p) == 2:
                try:
                    out.append([float(p[0]), float(p[1])])
                except Exception:
                    ok = False
                    break
            else:
                ok = False
                break
        if ok:
            machine["bed_shape"] = out
            return

    # try printable_area strings like "0x0"
    pa = machine.get("printable_area")
    if isinstance(pa, list) and all(isinstance(s, str) for s in pa):
        pts: List[List[float]] = []
        for s in pa:
            if "x" in s:
                a, b = s.split("x", 1)
                pts.append([float(a), float(b)])
        if len(pts) >= 3:
            machine["bed_shape"] = pts
            machine.pop("printable_area", None)

def _normalize_machine(machine: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    # Ensure required keys and types
    name = machine.get("name") or "Generic 400x400 0.4 nozzle"
    machine["name"] = name

    # technology & flavor defaults
    machine.setdefault("printer_technology", "FFF")
    machine.setdefault("gcode_flavor", "marlin")

    # bed shape
    _ensure_float_pairs_bed_shape(machine)

    # heights as strings
    if "max_print_height" in machine:
        machine["max_print_height"] = _as_str(machine["max_print_height"])
    else:
        machine["max_print_height"] = "300.0"

    if "min_layer_height" in machine:
        machine["min_layer_height"] = _as_str(machine["min_layer_height"])
    if "max_layer_height" in machine:
        machine["max_layer_height"] = _as_str(machine["max_layer_height"])

    # extruders must be string
    if "extruders" in machine:
        machine["extruders"] = _as_str(machine["extruders"])
    else:
        machine["extruders"] = "1"

    # nozzle_diameter must be array of strings
    nd = machine.get("nozzle_diameter")
    if isinstance(nd, list):
        machine["nozzle_diameter"] = [ _as_str(v) for v in nd ]
    else:
        machine["nozzle_diameter"] = ["0.4"]

    return machine, name

def _strip_printer_specific_from(data: Dict[str, Any]) -> None:
    """
    Remove fields that cause type/compat checks in non-machine presets.
    """
    for k in ("extruders", "nozzle_diameter"):
        if k in data:
            data.pop(k, None)

def _force_compatible_printers(preset: Dict[str, Any], exact_printer_name: str) -> None:
    preset["compatible_printers"] = [exact_printer_name]
    # keep condition as empty string
    preset["compatible_printers_condition"] = preset.get("compatible_printers_condition", "")

def _merge_user_overrides(process: Dict[str, Any],
                          filament: Dict[str, Any],
                          material: str,
                          infill: float,
                          layer_height: float) -> None:
    # layer height/infill from user inputs (as strings Orca-safe)
    process["layer_height"] = _as_str(layer_height)
    # many Orca keys: "sparse_infill_density" accepts "0.2" or "20%" – wir nehmen Prozentstring
    pct = int(round(infill * 100))
    process["sparse_infill_density"] = f"{pct}%"

    # set material name in filament title (optional cosmetic)
    if "name" in filament and material:
        filament["name"] = f"{filament['name'].split(' (')[0]} ({material})" if filament["name"] else material

def _find_first_json(path_dir: str) -> Optional[str]:
    if not os.path.isdir(path_dir):
        return None
    for fn in os.listdir(path_dir):
        if fn.lower().endswith(".json"):
            return os.path.join(path_dir, fn)
    return None

def _safe_tail(text: str, n: int = 1200) -> str:
    if not text:
        return ""
    return text[-n:]

def _exec(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=True
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 999, f"TIMEOUT: {e}", ""

# ---------------------------
# HTML (no f-strings!)
# ---------------------------

INDEX_HTML = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <title>OrcaSlicer API Tester</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; }
    h1 { margin-top: 0; }
    code, pre { background:#f6f8fa; padding:4px 8px; border-radius:6px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
    .card { border:1px solid #e5e7eb; border-radius:10px; padding:16px; margin-bottom:16px; }
    .btn { padding:8px 12px; border:1px solid #111827; background:#111827; color:#fff; border-radius:8px; cursor:pointer; }
    .btn.outline { background:#fff; color:#111827; }
    #out { white-space:pre-wrap; font-family:ui-monospace, SFMono-Regular, Menlo, monospace; }
    input[type="file"] { display:block; margin:8px 0; }
    label { display:block; font-weight:600; margin-top:8px; }
    input, select { padding:6px 8px; border:1px solid #e5e7eb; border-radius:6px; }
  </style>
</head>
<body>
  <h1>OrcaSlicer API Tester</h1>

  <div class="card">
    <div class="row">
      <button class="btn" id="btnHealth">/health</button>
      <button class="btn" id="btnEnv">/slicer_env</button>
      <a class="btn outline" href="/docs" target="_blank">Swagger (API)</a>
    </div>
  </div>

  <div class="card">
    <h3>/slice_check</h3>
    <form id="sliceForm">
      <label>STL</label>
      <input type="file" name="file" accept=".stl" required/>
      <div class="row">
        <div>
          <label>unit</label>
          <select name="unit">
            <option value="mm" selected>mm</option>
            <option value="inch">inch</option>
          </select>
        </div>
        <div>
          <label>material</label>
          <select name="material">
            <option value="PLA" selected>PLA</option>
            <option value="PETG">PETG</option>
            <option value="ASA">ASA</option>
          </select>
        </div>
        <div>
          <label>infill (0..1)</label>
          <input type="number" step="0.01" min="0" max="1" name="infill" value="0.2"/>
        </div>
        <div>
          <label>layer_height (mm)</label>
          <input type="number" step="0.01" min="0.06" max="0.4" name="layer_height" value="0.2"/>
        </div>
        <div>
          <label>nozzle (mm)</label>
          <input type="number" step="0.01" min="0.2" max="1.2" name="nozzle" value="0.4"/>
        </div>
      </div>
      <div style="margin-top:12px;">
        <button class="btn" type="submit">Senden</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>Output</h3>
    <pre id="out">Bereit.</pre>
  </div>

  <script>
    const out = document.getElementById('out');

    function show(obj) {
      out.textContent = JSON.stringify(obj, null, 2);
    }

    document.getElementById('btnHealth').addEventListener('click', async (e) => {
      const r = await fetch('/health');
      show(await r.json());
    });

    document.getElementById('btnEnv').addEventListener('click', async (e) => {
      const r = await fetch('/slicer_env');
      show(await r.json());
    });

    document.getElementById('sliceForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const r = await fetch('/slice_check', { method: 'POST', body: fd });
      const t = r.headers.get('content-type') || '';
      if (t.includes('application/json')) {
        show(await r.json());
      } else {
        out.textContent = await r.text();
      }
    });
  </script>
</body>
</html>
"""

# ---------------------------
# Models
# ---------------------------

class SliceParams(BaseModel):
    unit: str = "mm"
    material: str = "PLA"
    infill: float = 0.2
    layer_height: float = 0.2
    nozzle: float = 0.4

# ---------------------------
# Routes
# ---------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

@app.get("/health")
def health():
    return {"ok": True, "version": app.version if hasattr(app, "version") else "1.0.0"}

@app.get("/slicer_env")
def slicer_env():
    resp: Dict[str, Any] = {
        "ok": True,
        "slicer_bin": ORCA_BIN,
        "slicer_present": os.path.exists(ORCA_BIN),
        "profiles": {
            "printer": [],
            "process": [],
            "filament": [],
        },
    }
    for dkey, dpath in (("printer", PRINTER_DIR), ("process", PROCESS_DIR), ("filament", FILAMENT_DIR)):
        if os.path.isdir(dpath):
            resp["profiles"][dkey] = [os.path.join(dpath, f) for f in os.listdir(dpath) if f.lower().endswith(".json")]

    if os.path.exists(BUNDLE_FILE):
        try:
            resp["bundle_structure"] = _read_json(BUNDLE_FILE)
        except Exception as e:
            resp["bundle_structure_error"] = str(e)
    return resp

def _pick_repo_profiles(material: str = "PLA") -> Tuple[str, str, str]:
    # Prefer specific known filenames; fallback to first JSON in each dir
    printer = os.path.join(PRINTER_DIR, "X1C.json")
    if not os.path.exists(printer):
        # fallback any
        alt = _find_first_json(PRINTER_DIR)
        if not alt:
            raise FileNotFoundError(f"Printer-Profil fehlt: {os.path.join(PRINTER_DIR, 'X1C.json')}")
        printer = alt

    process = os.path.join(PROCESS_DIR, "0.20mm_standard.json")
    if not os.path.exists(process):
        alt = _find_first_json(PROCESS_DIR)
        if not alt:
            raise FileNotFoundError(f"Process-Profil fehlt: {os.path.join(PROCESS_DIR, '0.20mm_standard.json')}")
        process = alt

    # Filament by material, fallback any
    filament = os.path.join(FILAMENT_DIR, f"{material.upper()}.json")
    if not os.path.exists(filament):
        alt = _find_first_json(FILAMENT_DIR)
        if not alt:
            raise FileNotFoundError(f"Filament-Profil fehlt: {filament}")
        filament = alt

    return printer, process, filament

def _prepare_profiles_in_tmp(tmp: str,
                             material: str,
                             infill: float,
                             layer_height: float) -> Tuple[str, str, str, Dict[str, Any], Dict[str, Any], Dict[str, Any], str]:
    """
    Returns paths to temp printer/process/filament + their dicts + printer_name
    """
    printer_path, process_path, filament_path = _pick_repo_profiles(material)

    machine = _read_json(printer_path)
    process = _read_json(process_path)
    filament = _read_json(filament_path)

    # Normalize machine and get its exact name
    machine, printer_name = _normalize_machine(machine)

    # Process/Filament clean-up and binding
    _strip_printer_specific_from(process)
    _strip_printer_specific_from(filament)
    _force_compatible_printers(process, printer_name)
    _force_compatible_printers(filament, printer_name)

    # Merge user overrides
    _merge_user_overrides(process, filament, material, infill, layer_height)

    # Write normalized copies
    p_path = os.path.join(tmp, "printer.json")
    pr_path = os.path.join(tmp, "process.json")
    f_path = os.path.join(tmp, "filament.json")
    _write_json(machine, p_path)
    _write_json(process, pr_path)
    _write_json(filament, f_path)

    return p_path, pr_path, f_path, machine, process, filament, printer_name

def _run_orca(tmpdir: str,
              p_path: str,
              pr_path: str,
              f_path: str,
              stl_path: str) -> Tuple[int, str, str, Dict[str, Any]]:
    out_3mf = os.path.join(tmpdir, "out.3mf")
    slicedata = os.path.join(tmpdir, "slicedata")
    merged_settings = os.path.join(tmpdir, "merged_settings.json")

    cmd = [
        "xvfb-run", "-a", ORCA_BIN,
        "--debug", "4",
        "--datadir", os.path.join(tmpdir, "cfg"),
        "--load-settings", f"{p_path};{pr_path}",
        "--load-filaments", f_path,
        "--arrange", "1",
        "--orient", "1",
        stl_path,
        "--slice", "1",
        "--export-3mf", out_3mf,
        "--export-slicedata", slicedata,
        "--export-settings", merged_settings,
    ]

    code, out, err = _exec(cmd, timeout=180)
    details = {
        "cmd": " ".join(cmd),
        "stdout_tail": _safe_tail(out, 4000),
        "stderr_tail": _safe_tail(err, 2000),
        "settings_tail": "",
        "out_3mf_exists": os.path.exists(out_3mf),
        "slicedata_exists": os.path.exists(slicedata),
        "merged_settings_exists": os.path.exists(merged_settings),
    }
    if os.path.exists(merged_settings):
        try:
            with open(merged_settings, "r", encoding="utf-8") as f:
                ms = f.read()
            details["settings_tail"] = _safe_tail(ms, 4000)
        except Exception:
            pass

    return code, out, err, details

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(..., description="STL file"),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),  # derzeit nur informativ; Machine-Profil liefert nozzle_diameter
):
    if not os.path.exists(ORCA_BIN):
        return JSONResponse(status_code=500, content={"detail": {"ok": False, "message": f"Slicer nicht gefunden: {ORCA_BIN}"}})

    with tempfile.TemporaryDirectory(prefix="fixedp_") as tmp:
        # Save STL
        stl_path = os.path.join(tmp, "input.stl")
        content = await file.read()
        with open(stl_path, "wb") as f:
            f.write(content)

        # Prepare normalized profiles
        try:
            p_path, pr_path, f_path, machine, process, filament, printer_name = _prepare_profiles_in_tmp(
                tmp, material, infill, layer_height
            )
        except FileNotFoundError as e:
            return JSONResponse(status_code=404, content={"detail": str(e)})
        except json.JSONDecodeError as e:
            return JSONResponse(status_code=400, content={"detail": f"JSON-Fehler in Profilen: {e}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"Profil-Vorbereitung fehlgeschlagen: {e}"})

        # Run slicer
        code, out, err, details = _run_orca(tmp, p_path, pr_path, f_path, stl_path)

        resp = {
            "detail": {
                "ok": code == 0,
                "code": code,
                **details,
                "profiles_used": {
                    "printer_name": printer_name,
                    "printer_path": os.path.join(PRINTER_DIR, os.path.basename(p_path)),  # informative
                    "process_path": os.path.join(PROCESS_DIR, os.path.basename(pr_path)),
                    "filament_path": os.path.join(FILAMENT_DIR, os.path.basename(f_path)),
                },
                "inputs": {
                    "unit": unit,
                    "material": material,
                    "infill": infill,
                    "layer_height": layer_height,
                    "nozzle": nozzle,
                    "stl_bytes": len(content),
                },
                "hint": {
                    "summary": "process not compatible with printer" if code != 0 else "ok",
                    "we_fixed_types": {
                        "machine.extruders": machine.get("extruders"),
                        "machine.nozzle_diameter": machine.get("nozzle_diameter"),
                        "machine.max_print_height": machine.get("max_print_height"),
                        "machine.min_layer_height": machine.get("min_layer_height"),
                        "machine.max_layer_height": machine.get("max_layer_height"),
                    },
                    "we_removed_from_process_and_filament": ["extruders", "nozzle_diameter"],
                    "compatible_printers": process.get("compatible_printers"),
                }
            }
        }
        status = 200 if code == 0 else 500 if code not in (239, ) else 200
        # 239 behalten wir als 200 mit ok=false, damit die UI gut debuggen kann
        return JSONResponse(status_code=status, content=resp)

@app.post("/slice")
async def slice_alias(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    return await slice_check(file=file, unit=unit, material=material, infill=infill, layer_height=layer_height, nozzle=nozzle)

# -------------- Run (for local dev) --------------
if __name__ == "__main__":
    # Render startet via gunicorn/uvicorn; lokal kann man so testen:
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
