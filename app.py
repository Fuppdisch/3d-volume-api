import os
import json
import tempfile
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

APP_VERSION = "1.0.1"

# -------------------- utilities --------------------

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def to_float(x: Any) -> Optional[float]:
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    if isinstance(x, str):
        s = x.strip().replace("%", "")
        try: return float(s)
        except Exception: return None
    return None

def to_int(x: Any) -> Optional[int]:
    f = to_float(x)
    if f is None: return None
    try: return int(round(f))
    except Exception: return None

def ensure_list(x: Any) -> List[Any]:
    if x is None: return []
    return x if isinstance(x, list) else [x]

def last_tail(s: str, n: int = 1400) -> str:
    if not s: return ""
    return s if len(s) <= n else s[-n:]

def orca_bin() -> str:
    return os.environ.get("ORCA_SLICER_BIN", "/opt/orca/bin/orca-slicer")

def profiles_root() -> str:
    return "/app/profiles"

def find_bundle() -> Optional[Dict[str, Any]]:
    p = os.path.join(profiles_root(), "bundle_structure.json")
    if os.path.isfile(p):
        try:
            b = load_json(p)
            if "bundle_type" in b and "printer_config" in b:
                return b
        except Exception:
            pass
    return None

def resolve_profile_paths() -> Tuple[str, str, str, str]:
    """
    (printer.json, process.json, filament.json, printer_preset_name)
    """
    bundle = find_bundle()
    if bundle:
        def resolve_one(rel_list: List[str]) -> Optional[str]:
            for rel in ensure_list(rel_list):
                cand = os.path.join(profiles_root(), rel)
                if os.path.isfile(cand):
                    return cand
            return None
        pr = resolve_one(bundle.get("printer_config", []))
        pc = resolve_one(bundle.get("process_config", []))
        fi = resolve_one(bundle.get("filament_config", []))
        name = bundle.get("printer_preset_name", "")
        if pr and pc and fi:
            return pr, pc, fi, name

    # Fallback
    pr = os.path.join(profiles_root(), "printer", "X1C.json")
    pc = os.path.join(profiles_root(), "process", "0.20mm_standard.json")
    fi = os.path.join(profiles_root(), "filament", "PLA.json")
    name = "RatRig V-Core 4 400 0.4 nozzle"
    return pr, pc, fi, name

# -------------------- normalization --------------------

def normalize_machine(m: Dict[str, Any]) -> None:
    m["type"] = m.get("type", "machine")
    m["from"] = m.get("from", "user")
    # bed shape from strings "AxB" or pairs
    if "bed_shape" in m and isinstance(m["bed_shape"], list):
        bs = m["bed_shape"]
        if bs and isinstance(bs[0], str):
            pts = []
            for s in bs:
                if "x" in s:
                    a, b = s.split("x", 1)
                    pts.append([to_float(a) or 0.0, to_float(b) or 0.0])
            if pts:
                m["bed_shape"] = pts
        else:
            m["bed_shape"] = [[to_float(p[0]) or 0.0, to_float(p[1]) or 0.0] for p in bs]
    if "printable_area" in m and "bed_shape" not in m:
        pts = []
        for s in ensure_list(m["printable_area"]):
            if isinstance(s, str) and "x" in s:
                a, b = s.split("x", 1)
                pts.append([to_float(a) or 0.0, to_float(b) or 0.0])
        if pts: m["bed_shape"] = pts

    # numeric fields
    mph = to_float(m.get("max_print_height"))
    m["max_print_height"] = mph if mph is not None else 300.0
    m["min_layer_height"] = 0.15
    m["max_layer_height"] = 0.30

    ex = to_int(m.get("extruders"))
    m["extruders"] = ex if ex is not None else 1

    nd = []
    for v in ensure_list(m.get("nozzle_diameter", [0.4])):
        f = to_float(v)
        if f is not None:
            nd.append(f)
    m["nozzle_diameter"] = nd if nd else [0.4]

    m["printer_technology"] = m.get("printer_technology", "FFF")
    m["gcode_flavor"] = m.get("gcode_flavor", "marlin")

    # keep model/variant if present (some profiles rely on it)
    if not m.get("printer_model"):
        m["printer_model"] = "RatRig V-Core 4 400"
    if not m.get("printer_variant"):
        m["printer_variant"] = "0.4"

def clamp_layer(process: Dict[str, Any], lo: float, hi: float) -> None:
    lh = to_float(process.get("layer_height", 0.2)) or 0.2
    if lh < lo: lh = lo
    if lh > hi: lh = hi
    process["layer_height"] = lh

def normalize_process(p: Dict[str, Any]) -> None:
    p["type"] = p.get("type", "process")
    p["from"] = p.get("from", "user")
    if "layer_height" in p:
        f = to_float(p["layer_height"])
        if f is not None: p["layer_height"] = f
    if "first_layer_height" in p:
        f = to_float(p["first_layer_height"])
        if f is not None: p["first_layer_height"] = f
    # density to "NN%"
    if "sparse_infill_density" in p:
        f = to_float(p["sparse_infill_density"])
        if f is not None: p["sparse_infill_density"] = f"{int(round(f))}%"
    # ensure lists exist
    if "compatible_printers" not in p or p["compatible_printers"] is None:
        p["compatible_printers"] = []
    p["compatible_printers_condition"] = ""

def normalize_filament(f: Dict[str, Any]) -> None:
    f["type"] = f.get("type", "filament")
    f["from"] = f.get("from", "user")

    def to_str_list(val):
        if val is None: return None
        vals = ensure_list(val)
        out = []
        for item in vals:
            ff = to_float(item)
            if ff is None:
                out.append(str(item))
            else:
                out.append(str(int(ff)) if ff.is_integer() else str(ff))
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
        if key in f:
            f[key] = to_str_list(f[key])

    if "compatible_printers" not in f or f["compatible_printers"] is None:
        f["compatible_printers"] = []
    f["compatible_printers_condition"] = ""

def copy_machine_signature(machine: Dict[str, Any], target: Dict[str, Any]) -> None:
    """Copy signature fields into process/filament to satisfy Orca compat matcher."""
    target["printer_technology"] = machine.get("printer_technology", "FFF")
    target["printer_model"] = machine.get("printer_model", "RatRig V-Core 4 400")
    target["printer_variant"] = machine.get("printer_variant", "0.4")
    target["gcode_flavor"] = machine.get("gcode_flavor", "marlin")
    target["extruders"] = machine.get("extruders", 1)
    target["nozzle_diameter"] = machine.get("nozzle_diameter", [0.4])

def inject_compat(printer_name: str, process: Dict[str, Any], filament: Dict[str, Any]) -> None:
    def add(lst: List[str], val: str):
        if val not in lst: lst.append(val)
    process["compatible_printers"] = ensure_list(process.get("compatible_printers"))
    filament["compatible_printers"] = ensure_list(filament.get("compatible_printers"))
    add(process["compatible_printers"], printer_name)
    add(filament["compatible_printers"], printer_name)
    process["compatible_printers_condition"] = ""
    filament["compatible_printers_condition"] = ""

# -------------------- build run configs --------------------

def build_temp_configs(
    stl_bytes: bytes,
    unit: str,
    material: str,
    infill: float,
    layer_height: float,
    nozzle: float,
) -> Tuple[str, str, str, str, str, Dict[str, Any], Dict[str, Any], Dict[str, Any], str, str, str]:
    tmpdir = tempfile.mkdtemp(prefix="fixedp_")
    input_stl = os.path.join(tmpdir, "input.stl")
    with open(input_stl, "wb") as f:
        f.write(stl_bytes)

    repo_pr, repo_pc, repo_fi, preset_name = resolve_profile_paths()
    machine = load_json(repo_pr)
    process = load_json(repo_pc)
    filament = load_json(repo_fi)

    # normalize machine first
    normalize_machine(machine)
    # apply requested nozzle to machine
    machine["nozzle_diameter"] = [float(nozzle)]
    # normalize process & filament
    normalize_process(process)
    normalize_filament(filament)

    # clamp requested layer
    process["layer_height"] = float(layer_height)
    clamp_layer(process, machine["min_layer_height"], machine["max_layer_height"])

    # infill 0..1 -> "NN%"
    process["sparse_infill_density"] = f"{int(round(float(infill) * 100))}%"

    # Copy full machine signature into process + filament (to satisfy compat)
    copy_machine_signature(machine, process)
    copy_machine_signature(machine, filament)

    # Ensure compat lists include exact printer name
    printer_name = preset_name or machine.get("name") or "RatRig V-Core 4 400 0.4 nozzle"
    machine["name"] = printer_name
    inject_compat(printer_name, process, filament)

    p_pr = os.path.join(tmpdir, "printer.json")
    p_pc = os.path.join(tmpdir, "process.json")
    p_fi = os.path.join(tmpdir, "filament.json")
    save_json(p_pr, machine)
    save_json(p_pc, process)
    save_json(p_fi, filament)
    return tmpdir, p_pr, p_pc, p_fi, input_stl, machine, process, filament, repo_pr, repo_pc, repo_fi

# -------------------- orca runner --------------------

def run_orca(tmpdir: str, p_pr: str, p_pc: str, p_fi: str, input_stl: str) -> Dict[str, Any]:
    out_3mf = os.path.join(tmpdir, "out.3mf")
    slicedata = os.path.join(tmpdir, "slicedata")
    merged = os.path.join(tmpdir, "merged_settings.json")
    result_json_path = os.path.join(tmpdir, "result.json")

    cmd = [
        "xvfb-run","-a", orca_bin(),
        "--debug","4",
        "--datadir", tmpdir,
        "--load-settings", p_pr,
        "--load-settings", p_pc,
        "--load-filaments", p_fi,
        "--arrange","1","--orient","1", input_stl,
        "--slice","1",
        "--export-3mf", out_3mf,
        "--export-slicedata", slicedata,
        "--export-settings", merged,
    ]
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        out = {
            "code": r.returncode,
            "cmd": " ".join(cmd),
            "stdout_tail": last_tail(r.stdout or ""),
            "stderr_tail": last_tail(r.stderr or ""),
            "out_3mf_exists": os.path.isfile(out_3mf),
            "slicedata_exists": os.path.isdir(slicedata),
            "merged_settings_exists": os.path.isfile(merged),
            "result_json": None,
        }
        if os.path.isfile(result_json_path):
            try: out["result_json"] = load_json(result_json_path)
            except Exception: pass
        return out
    except Exception as e:
        return {"code": -1, "cmd": " ".join(cmd), "error": str(e)}

# -------------------- fastapi app --------------------

app = FastAPI(title="Orca Slice API", version=APP_VERSION)

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}

@app.get("/slicer_env")
def slicer_env():
    pr, pc, fi, name = resolve_profile_paths()
    bundle = find_bundle()
    return {
        "ok": True,
        "slicer_bin": orca_bin(),
        "slicer_present": os.path.isfile(orca_bin()),
        "profiles": {"printer":[pr], "process":[pc], "filament":[fi]},
        "bundle_structure": bundle
    }

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
        (tmpdir, p_pr, p_pc, p_fi, input_stl,
         machine, process, filament, repo_pr, repo_pc, repo_fi) = build_temp_configs(
            stl_bytes, unit, material, infill, layer_height, nozzle
        )
        res = run_orca(tmpdir, p_pr, p_pc, p_fi, input_stl)
        detail = {
            "ok": (res.get("code") == 0 and res.get("out_3mf_exists")),
            "code": res.get("code"),
            "cmd": res.get("cmd"),
            "stdout_tail": res.get("stdout_tail"),
            "stderr_tail": res.get("stderr_tail"),
            "out_3mf_exists": res.get("out_3mf_exists"),
            "slicedata_exists": res.get("slicedata_exists"),
            "merged_settings_exists": res.get("merged_settings_exists"),
            "profiles_used": {
                "printer_name": machine.get("name"),
                "printer_path": repo_pr,
                "process_path": repo_pc,
                "filament_path": repo_fi
            },
            "inputs": {
                "unit": unit, "material": material, "infill": infill,
                "layer_height": layer_height, "nozzle": nozzle,
                "stl_bytes": len(stl_bytes)
            },
            "normalized_preview": {
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
                    "printer_model": machine.get("printer_model"),
                    "printer_variant": machine.get("printer_variant"),
                },
                "process": {
                    "compatible_printers": process.get("compatible_printers"),
                    "sparse_infill_density": process.get("sparse_infill_density"),
                    "layer_height": process.get("layer_height"),
                    "printer_model": process.get("printer_model"),
                    "printer_variant": process.get("printer_variant"),
                    "extruders": process.get("extruders"),
                    "nozzle_diameter": process.get("nozzle_diameter"),
                },
                "filament": {
                    "name": filament.get("name", filament.get("type","filament")),
                    "compatible_printers": filament.get("compatible_printers"),
                    "printer_model": filament.get("printer_model"),
                    "printer_variant": filament.get("printer_variant"),
                    "extruders": filament.get("extruders"),
                    "nozzle_diameter": filament.get("nozzle_diameter"),
                },
            },
            "result_json": res.get("result_json"),
        }
        return JSONResponse({"detail": detail}, status_code=200 if detail["ok"] else 500)
    except Exception as e:
        return JSONResponse({"detail": f"Slice-Fehler: {e}"}, status_code=500)

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse("""<!doctype html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Orca Slice API</title>
<style>
body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;max-width:960px}
h1{margin:0 0 8px 0}.row{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
button{padding:8px 12px;border:1px solid #ddd;background:#f8f8f8;border-radius:8px;cursor:pointer}
pre{background:#111;color:#9fe097;padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}
form{border:1px solid #eee;padding:12px;border-radius:8px}label{display:block;font-size:12px;margin-top:8px;color:#333}
input{padding:6px 8px;border:1px solid #ccc;border-radius:6px}
</style></head>
<body>
<h1>Orca Slice API</h1>
<div class="row">
  <button onclick="q('/health','health')">Health</button>
  <button onclick="q('/slicer_env','env')">Slicer Env</button>
  <a href="/docs" target="_blank"><button>Swagger (API)</button></a>
</div>
<h3>Antwort: /health</h3><pre id="health"></pre>
<h3>Antwort: /slicer_env</h3><pre id="env"></pre>
<h3>/slice_check</h3>
<form id="f" onsubmit="return sendSlice(event)">
  <label>STL-Datei <input type="file" name="file" required accept=".stl"/></label>
  <div class="row">
    <label>Material <input name="material" value="PLA"/></label>
    <label>Infill (0..1) <input name="infill" value="0.2"/></label>
    <label>Layer (mm) <input name="layer_height" value="0.2"/></label>
    <label>Nozzle (mm) <input name="nozzle" value="0.4"/></label>
    <label>Unit <input name="unit" value="mm"/></label>
  </div>
  <button type="submit">Slice testen</button>
</form>
<pre id="slice"></pre>
<script>
function q(path,id){ fetch(path).then(r=>r.text()).then(t=>{ const el=document.getElementById(id); if(el) el.textContent=t; }).catch(e=>{ const el=document.getElementById(id); if(el) el.textContent=String(e); });}
function sendSlice(e){ e.preventDefault(); const fd=new FormData(document.getElementById('f'));
  fetch('/slice_check',{method:'POST',body:fd}).then(r=>r.text()).then(t=>{ const el=document.getElementById('slice'); if(el) el.textContent=t; })
  .catch(err=>{ const el=document.getElementById('slice'); if(el) el.textContent=String(err); }); return false;}
</script>
</body></html>""")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
