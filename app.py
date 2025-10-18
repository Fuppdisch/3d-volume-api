import os
import io
import json
import tempfile
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

APP_VERSION = "1.0.0"

# --- Helpers ---------------------------------------------------------------

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace("%", "")
        try:
            return float(s)
        except Exception:
            return None
    return None

def to_int(x: Any) -> Optional[int]:
    f = to_float(x)
    if f is None:
        return None
    try:
        return int(round(f))
    except Exception:
        return None

def ensure_list(x: Any) -> List[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]

def last_tail(s: str, n: int = 1200) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[-n:]

def orca_bin() -> str:
    return os.environ.get("ORCA_SLICER_BIN", "/opt/orca/bin/orca-slicer")

def profiles_root() -> str:
    # fixed layout in Render image
    return "/app/profiles"

def find_bundle() -> Optional[Dict[str, Any]]:
    p = os.path.join(profiles_root(), "bundle_structure.json")
    if os.path.isfile(p):
        try:
            b = load_json(p)
            # minimal sanity
            if "bundle_type" in b and "printer_config" in b:
                return b
        except Exception:
            return None
    return None

def resolve_profile_paths() -> Tuple[str, str, str, str]:
    """
    Returns (printer_path, process_path, filament_path, printer_preset_name)
    Preference order:
      1) bundle_structure.json
      2) default repo files
    """
    bundle = find_bundle()
    if bundle:
        # Resolve relative paths inside /app/profiles
        pr_list = ensure_list(bundle.get("printer_config"))
        pc_list = ensure_list(bundle.get("process_config"))
        fi_list = ensure_list(bundle.get("filament_config"))
        def resolve_one(rel_list: List[str]) -> Optional[str]:
            for rel in rel_list:
                cand = os.path.join(profiles_root(), rel)
                if os.path.isfile(cand):
                    return cand
            return None
        pr = resolve_one(pr_list)
        pc = resolve_one(pc_list)
        fi = resolve_one(fi_list)
        name = bundle.get("printer_preset_name", "")
        if pr and pc and fi:
            return pr, pc, fi, name

    # Fallback to repo defaults
    pr = os.path.join(profiles_root(), "printer", "X1C.json")
    pc = os.path.join(profiles_root(), "process", "0.20mm_standard.json")
    fi = os.path.join(profiles_root(), "filament", "PLA.json")
    name = "RatRig V-Core 4 400 0.4 nozzle"  # our working default
    return pr, pc, fi, name

# --- Normalization of machine/process/filament -----------------------------

CONFLICT_KEYS = (
    "extruders", "nozzle_diameter", "printer_model", "printer_variant",
    "printer_technology", "gcode_flavor"
)

def normalize_machine(machine: Dict[str, Any]) -> None:
    machine["type"] = machine.get("type", "machine")
    machine["from"] = machine.get("from", "user")
    # Bed shape -> list of float pairs
    if "bed_shape" in machine:
        bs = machine["bed_shape"]
        if isinstance(bs, list) and all(isinstance(t, list) and len(t) == 2 for t in bs):
            machine["bed_shape"] = [[to_float(t[0]) or 0.0, to_float(t[1]) or 0.0] for t in bs]
        elif isinstance(bs, list) and all(isinstance(t, str) and "x" in t for t in bs):
            pts = []
            for s in bs:
                a, b = s.split("x", 1)
                pts.append([to_float(a) or 0.0, to_float(b) or 0.0])
            machine["bed_shape"] = pts
    # printable_area alias -> convert
    if "printable_area" in machine and "bed_shape" not in machine:
        pa = machine["printable_area"]
        pts = []
        for s in ensure_list(pa):
            if isinstance(s, str) and "x" in s:
                a, b = s.split("x", 1)
                pts.append([to_float(a) or 0.0, to_float(b) or 0.0])
        if pts:
            machine["bed_shape"] = pts

    # numeric conversions
    if "max_print_height" in machine:
        mph = to_float(machine["max_print_height"])
        if mph is not None:
            machine["max_print_height"] = mph
    # set requested min/max layer window
    machine["min_layer_height"] = 0.15
    machine["max_layer_height"] = 0.30

    # extruders
    if "extruders" in machine:
        ei = to_int(machine["extruders"])
        machine["extruders"] = ei if ei is not None else 1
    else:
        machine["extruders"] = 1

    # nozzle_diameter list of floats
    nd = machine.get("nozzle_diameter", [0.4])
    nd_list = []
    for x in ensure_list(nd):
        f = to_float(x)
        if f is not None:
            nd_list.append(f)
    machine["nozzle_diameter"] = nd_list if nd_list else [0.4]

    # defaults
    machine["printer_technology"] = machine.get("printer_technology", "FFF")
    machine["gcode_flavor"] = machine.get("gcode_flavor", "marlin")

def clamp_layer_heights(process: Dict[str, Any], min_h: float = 0.15, max_h: float = 0.30) -> None:
    lh = to_float(process.get("layer_height", 0.2)) or 0.2
    if lh < min_h:
        lh = min_h
    if lh > max_h:
        lh = max_h
    process["layer_height"] = lh

def normalize_process(process: Dict[str, Any]) -> None:
    process["type"] = process.get("type", "process")
    process["from"] = process.get("from", "user")

    # Convert numbers
    if "layer_height" in process:
        f = to_float(process["layer_height"])
        if f is not None:
            process["layer_height"] = f
    if "first_layer_height" in process:
        f = to_float(process["first_layer_height"])
        if f is not None:
            process["first_layer_height"] = f

    # speeds as numbers if provided (not strictly required)
    for k in ("outer_wall_speed", "inner_wall_speed", "travel_speed"):
        if k in process:
            f = to_float(process[k])
            if f is not None:
                process[k] = f

    # densities: sparse_infill_density must be a percentage string like "20%"
    if "sparse_infill_density" in process:
        val = process["sparse_infill_density"]
        f = to_float(val)
        if f is not None:
            process["sparse_infill_density"] = f"{int(round(f))}%"

    # clean conflicts (printer decides)
    for k in CONFLICT_KEYS:
        process.pop(k, None)

    # compat lists always exist
    if "compatible_printers" not in process or process["compatible_printers"] is None:
        process["compatible_printers"] = []
    process["compatible_printers_condition"] = ""

def normalize_filament(filament: Dict[str, Any]) -> None:
    filament["type"] = filament.get("type", "filament")
    filament["from"] = filament.get("from", "user")

    # Orca accepts numeric arrays as strings more reliably for filament params
    def to_str_list(val):
        if val is None:
            return None
        vals = ensure_list(val)
        out = []
        for item in vals:
            f = to_float(item)
            if f is None:
                out.append(str(item))
            else:
                if float(int(f)) == f:
                    out.append(str(int(f)))
                else:
                    out.append(str(f))
        return out

    for key in (
        "filament_flow_ratio",
        "nozzle_temperature",
        "nozzle_temperature_initial_layer",
        "bed_temperature",
        "bed_temperature_initial_layer",
        "filament_diameter",
        "filament_density",
    ):
        if key in filament:
            filament[key] = to_str_list(filament[key])

    # clean conflicts
    for k in CONFLICT_KEYS:
        filament.pop(k, None)

    if "compatible_printers" not in filament or filament["compatible_printers"] is None:
        filament["compatible_printers"] = []
    filament["compatible_printers_condition"] = ""

def inject_compat(printer_name: str, process: Dict[str, Any], filament: Dict[str, Any]) -> None:
    # ensure exact printer name is listed
    def add_unique(lst: List[str], val: str):
        if val not in lst:
            lst.append(val)

    process["compatible_printers"] = ensure_list(process.get("compatible_printers"))
    filament["compatible_printers"] = ensure_list(filament.get("compatible_printers"))

    add_unique(process["compatible_printers"], printer_name)
    add_unique(filament["compatible_printers"], printer_name)

    process["compatible_printers_condition"] = ""
    filament["compatible_printers_condition"] = ""

# --- Build temp configs for a run ------------------------------------------

def build_temp_configs(
    stl_bytes: bytes,
    unit: str,
    material: str,
    infill: float,
    layer_height: float,
    nozzle: float,
) -> Tuple[str, str, str, str, str]:
    """
    Returns tmpdir, printer.json path, process.json path, filament.json path, input_stl path
    """
    tmpdir = tempfile.mkdtemp(prefix="fixedp_")
    input_stl = os.path.join(tmpdir, "input.stl")
    with open(input_stl, "wb") as f:
        f.write(stl_bytes)

    # load base profiles from repo (or bundle_structure mapping)
    repo_printer, repo_process, repo_filament, printer_name_from_bundle = resolve_profile_paths()
    machine = load_json(repo_printer)
    process = load_json(repo_process)
    filament = load_json(repo_filament)

    # normalize profiles
    normalize_machine(machine)
    normalize_process(process)
    normalize_filament(filament)

    # Requested overrides
    machine["name"] = printer_name_from_bundle or machine.get("name", "Generic 400x400 0.4 nozzle")
    machine["nozzle_diameter"] = [float(nozzle)]  # printer owns this

    process["layer_height"] = float(layer_height)
    clamp_layer_heights(process, machine.get("min_layer_height", 0.15), machine.get("max_layer_height", 0.30))
    process["sparse_infill_density"] = f"{int(round(float(infill) * 100))}%"

    # Ensure compatibility lists include exact printer name
    inject_compat(machine["name"], process, filament)

    # Write to temp dir
    p_pr = os.path.join(tmpdir, "printer.json")
    p_pc = os.path.join(tmpdir, "process.json")
    p_fi = os.path.join(tmpdir, "filament.json")

    save_json(p_pr, machine)
    save_json(p_pc, process)
    save_json(p_fi, filament)

    return tmpdir, p_pr, p_pc, p_fi, input_stl

# --- Call Orca -------------------------------------------------------------

def run_orca_slice(tmpdir: str, printer_json: str, process_json: str, filament_json: str, input_stl: str) -> Dict[str, Any]:
    out_3mf = os.path.join(tmpdir, "out.3mf")
    slicedata = os.path.join(tmpdir, "slicedata")
    merged = os.path.join(tmpdir, "merged_settings.json")
    result_json_path = os.path.join(tmpdir, "result.json")

    cmd = [
        "xvfb-run", "-a", orca_bin(),
        "--debug", "4",
        "--datadir", tmpdir,
        "--load-settings", printer_json,
        "--load-settings", process_json,
        "--load-filaments", filament_json,
        "--arrange", "1", "--orient", "1", input_stl,
        "--slice", "1",
        "--export-3mf", out_3mf,
        "--export-slicedata", slicedata,
        "--export-settings", merged,
    ]

    try:
        r = subprocess.run(
            cmd, check=False, capture_output=True, text=True
        )
        stdout_tail = last_tail(r.stdout or "")
        stderr_tail = last_tail(r.stderr or "")
        result: Dict[str, Any] = {
            "code": r.returncode,
            "cmd": " ".join(cmd),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "out_3mf_exists": os.path.isfile(out_3mf),
            "slicedata_exists": os.path.isdir(slicedata),
            "merged_settings_exists": os.path.isfile(merged),
            "profiles_used": {},
            "result_json": None,
        }
        if os.path.isfile(result_json_path):
            try:
                result["result_json"] = load_json(result_json_path)
            except Exception:
                pass
        return result
    except Exception as e:
        return {
            "code": -1,
            "cmd": " ".join(cmd),
            "error": str(e),
        }

# --- FastAPI ---------------------------------------------------------------

app = FastAPI(title="Orca Slice API", version=APP_VERSION)

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}

@app.get("/slicer_env")
def slicer_env():
    pr, pc, fi, name = resolve_profile_paths()
    env = {
        "ok": True,
        "slicer_bin": orca_bin(),
        "slicer_present": os.path.isfile(orca_bin()),
        "profiles": {
            "printer": [pr],
            "process": [pc],
            "filament": [fi],
        },
        "bundle_structure": load_json(os.path.join(profiles_root(), "bundle_structure.json")) if os.path.isfile(os.path.join(profiles_root(), "bundle_structure.json")) else None
    }
    return JSONResponse(env)

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.20),
    layer_height: float = Form(0.20),
    nozzle: float = Form(0.40),
):
    try:
        stl_bytes = await file.read()
        tmpdir, p_pr, p_pc, p_fi, input_stl = build_temp_configs(
            stl_bytes=stl_bytes,
            unit=unit,
            material=material,
            infill=infill,
            layer_height=layer_height,
            nozzle=nozzle,
        )
        pr, pc, fi, preset_name = resolve_profile_paths()
        result = run_orca_slice(tmpdir, p_pr, p_pc, p_fi, input_stl)

        # Add preview of what we actually wrote
        try:
            machine = load_json(p_pr)
            process = load_json(p_pc)
            filament = load_json(p_fi)
            normalized_preview = {
                "machine": {
                    "name": machine.get("name"),
                    "printer_technology": machine.get("printer_technology"),
                    "gcode_flavor": machine.get("gcode_flavor"),
                    "bed_shape": machine.get("bed_shape"),
                    "max_print_height": machine.get("max_print_height"),
                    "min_layer_height": machine.get("min_layer_height"),
                    "max_layer_height": machine.get("max_layer_height"),
                    "extruders": machine.get("extruders"),
                    "nozzle_diameter": machine.get("nozzle_diameter"),
                },
                "process": {
                    "compatible_printers": process.get("compatible_printers"),
                    "sparse_infill_density": process.get("sparse_infill_density"),
                    "layer_height": process.get("layer_height"),
                },
                "filament": {
                    "name": filament.get("name", filament.get("type", "filament")),
                    "compatible_printers": filament.get("compatible_printers"),
                },
            }
        except Exception:
            normalized_preview = None

        detail = {
            "ok": (result.get("code") == 0 and result.get("out_3mf_exists")),
            "code": result.get("code"),
            "cmd": result.get("cmd"),
            "stdout_tail": result.get("stdout_tail"),
            "stderr_tail": result.get("stderr_tail"),
            "out_3mf_exists": result.get("out_3mf_exists"),
            "slicedata_exists": result.get("slicedata_exists"),
            "merged_settings_exists": result.get("merged_settings_exists"),
            "profiles_used": {
                "printer_name": preset_name,
                "printer_path": pr,
                "process_path": pc,
                "filament_path": fi,
            },
            "inputs": {
                "unit": unit,
                "material": material,
                "infill": infill,
                "layer_height": layer_height,
                "nozzle": nozzle,
                "stl_bytes": len(stl_bytes),
            },
            "normalized_preview": normalized_preview,
            "result_json": result.get("result_json"),
        }

        status = 200 if detail["ok"] else 500
        return JSONResponse({"detail": detail}, status_code=status)

    except Exception as e:
        return JSONResponse({"detail": f"Slice-Fehler: {e}"}, status_code=500)

@app.get("/", response_class=HTMLResponse)
def index():
    # Simple tester UI (IDs match the JS map!)
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Orca Slice API</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; max-width: 920px; }
    h1 { margin: 0 0 8px 0; }
    .row { display:flex; gap:12px; margin: 12px 0; flex-wrap: wrap; }
    button { padding:8px 12px; border:1px solid #ddd; background:#f8f8f8; cursor:pointer; border-radius:8px; }
    pre { background:#111; color:#9fe097; padding:12px; border-radius:8px; white-space:pre-wrap; word-break:break-word; max-height: 360px; overflow:auto; }
    form { border:1px solid #eee; padding:12px; border-radius:8px; }
    label { display:block; font-size:12px; margin-top:8px; color:#333 }
    input { padding:6px 8px; border:1px solid #ccc; border-radius:6px; }
  </style>
</head>
<body>
  <h1>Orca Slice API</h1>
  <div class="row">
    <button onclick="q('/health')">Health</button>
    <button onclick="q('/slicer_env')">Slicer Env</button>
    <a href="/docs" target="_blank"><button>Swagger (API)</button></a>
  </div>

  <h3>Antwort: /health</h3>
  <pre id="health"></pre>

  <h3>Antwort: /slicer_env</h3>
  <pre id="env"></pre>

  <h3>/slice_check</h3>
  <form id="f" onsubmit="return sendSlice(event)">
    <label>STL-Datei <input type="file" name="file" required accept=".stl"/></label>
    <div class="row">
      <label>Material <input name="material" value="PLA"/></label>
      <label>Infill (0..1) <input name="infill" value="0.2" /></label>
      <label>Layer (mm) <input name="layer_height" value="0.2" /></label>
      <label>Nozzle (mm) <input name="nozzle" value="0.4" /></label>
      <label>Unit <input name="unit" value="mm" /></label>
    </div>
    <button type="submit">Slice testen</button>
  </form>
  <pre id="slice"></pre>

  <script>
    const targetMap = { "/health": "health", "/slicer_env": "env" };
    function q(path){
      const id = targetMap[path] || "health";
      fetch(path).then(r => r.text()).then(t => {
        const el = document.getElementById(id);
        if (el) el.textContent = t;
      }).catch(e => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(e);
      });
    }
    function sendSlice(e){
      e.preventDefault();
      const fd = new FormData(document.getElementById('f'));
      fetch('/slice_check', { method:'POST', body:fd })
        .then(r=>r.text())
        .then(t=>{
          const el = document.getElementById('slice');
          if (el) el.textContent = t;
        })
        .catch(err=>{
          const el = document.getElementById('slice');
          if (el) el.textContent = String(err);
        });
      return false;
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(html)

# --- Uvicorn entrypoint (Render uses this module path) ---------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
