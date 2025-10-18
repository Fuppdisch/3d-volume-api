# app.py
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import tempfile
import json
import os
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
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ---------- Helpers ----------

def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(data: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _as_str(x: Union[int, float, str]) -> str:
    if isinstance(x, (int, float)):
        s = f"{x}"
        if "." not in s:
            s = f"{x:.1f}"
        return s
    return str(x)

def _scalarize(v: Any) -> Any:
    """Wenn v eine Liste ist, nimm das erste Element. (Für Felder wie min/max_layer_height.)"""
    if isinstance(v, list) and v:
        return v[0]
    return v

def _ensure_float_pairs_bed_shape(machine: Dict[str, Any]) -> None:
    # Korrektes bed_shape erzeugen
    if "bed_shape" in machine and isinstance(machine["bed_shape"], list):
        out = []
        ok = True
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
    # Fallback: printable_area ["0x0","400x0",...]
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
    machine.setdefault("type", "machine")
    name = machine.get("name") or "Generic 400x400 0.4 nozzle"
    machine["name"] = name

    machine.setdefault("printer_technology", "FFF")
    machine.setdefault("gcode_flavor", "marlin")

    _ensure_float_pairs_bed_shape(machine)

    # scalarize + strings für Höhen
    for key in ("max_print_height", "min_layer_height", "max_layer_height"):
        if key in machine:
            val = _scalarize(machine[key])
            machine[key] = _as_str(val)
    machine.setdefault("max_print_height", "300.0")

    # extruders als String
    if "extruders" in machine:
        machine["extruders"] = _as_str(_scalarize(machine["extruders"]))
    else:
        machine["extruders"] = "1"

    # nozzle_diameter: Liste aus Strings
    nd = machine.get("nozzle_diameter")
    if isinstance(nd, list):
        nd = [_as_str(_scalarize(v)) for v in nd]
    else:
        nd = ["0.4"]
    machine["nozzle_diameter"] = nd

    return machine, name

def _strip_printer_specific_from(data: Dict[str, Any]) -> None:
    for k in ("extruders", "nozzle_diameter"):
        data.pop(k, None)

def _force_compatible_printers(preset: Dict[str, Any], exact_printer_name: str) -> None:
    preset["compatible_printers"] = [exact_printer_name]
    preset["compatible_printers_condition"] = preset.get("compatible_printers_condition", "")

def _merge_user_overrides(process: Dict[str, Any], filament: Dict[str, Any],
                          material: str, infill: float, layer_height: float) -> None:
    process["layer_height"] = _as_str(layer_height)
    process["sparse_infill_density"] = f"{int(round(infill * 100))}%"
    if "name" in filament and material:
        base = (filament["name"] or "").split(" (")[0]
        filament["name"] = f"{base} ({material})" if base else material

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

def _exec(cmd: List[str], timeout: int = 180) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, check=False, text=True)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 999, f"TIMEOUT: {e}", ""

# ---------- UI ----------

INDEX_HTML = """
<!doctype html><html lang="de"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>OrcaSlicer API Tester</title>
<style>
 body{font-family:system-ui,sans-serif;margin:24px} .row{display:flex;gap:12px;flex-wrap:wrap}
 .card{border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin-bottom:16px}
 .btn{padding:8px 12px;border:1px solid #111827;background:#111827;color:#fff;border-radius:8px;cursor:pointer}
 .btn.outline{background:#fff;color:#111827} pre{background:#f6f8fa;padding:8px;border-radius:8px}
</style></head><body>
<h1>OrcaSlicer API Tester</h1>
<div class="card"><div class="row">
<button class="btn" id="b1">/health</button>
<button class="btn" id="b2">/slicer_env</button>
<a class="btn outline" href="/docs" target="_blank">Swagger (API)</a>
</div></div>
<div class="card"><h3>/slice_check</h3>
<form id="f">
<input type="file" name="file" accept=".stl" required><br>
<label>unit</label><select name="unit"><option value="mm" selected>mm</option><option>inch</option></select>
<label>material</label><select name="material"><option selected>PLA</option><option>PETG</option><option>ASA</option></select>
<label>infill</label><input name="infill" type="number" step="0.01" min="0" max="1" value="0.2">
<label>layer_height</label><input name="layer_height" type="number" step="0.01" min="0.06" max="0.4" value="0.2">
<label>nozzle</label><input name="nozzle" type="number" step="0.01" min="0.2" max="1.2" value="0.4">
<button class="btn" type="submit">Senden</button>
</form></div>
<div class="card"><h3>Output</h3><pre id="out">Bereit.</pre></div>
<script>
const out = document.getElementById('out');
const show = o => out.textContent = JSON.stringify(o,null,2);
document.getElementById('b1').onclick=async()=>show(await (await fetch('/health')).json());
document.getElementById('b2').onclick=async()=>show(await (await fetch('/slicer_env')).json());
document.getElementById('f').onsubmit=async(e)=>{e.preventDefault(); const fd=new FormData(e.target);
 const r=await fetch('/slice_check',{method:'POST',body:fd}); const ct=r.headers.get('content-type')||'';
 show(ct.includes('application/json')? await r.json(): await r.text());};
</script></body></html>
"""

# ---------- Models ----------

class SliceParams(BaseModel):
    unit: str = "mm"
    material: str = "PLA"
    infill: float = 0.2
    layer_height: float = 0.2
    nozzle: float = 0.4

# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

@app.get("/health")
def health():
    return {"ok": True, "version": "1.0.0"}

@app.get("/slicer_env")
def slicer_env():
    resp = {
        "ok": True,
        "slicer_bin": ORCA_BIN,
        "slicer_present": os.path.exists(ORCA_BIN),
        "profiles": {"printer": [], "process": [], "filament": []},
    }
    for key, path in (("printer", PRINTER_DIR), ("process", PROCESS_DIR), ("filament", FILAMENT_DIR)):
        if os.path.isdir(path):
            resp["profiles"][key] = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".json")]
    if os.path.exists(BUNDLE_FILE):
        try:
            resp["bundle_structure"] = _read_json(BUNDLE_FILE)
        except Exception as e:
            resp["bundle_structure_error"] = str(e)
    return resp

def _pick_repo_profiles(material: str = "PLA") -> Tuple[str, str, str]:
    p = os.path.join(PRINTER_DIR, "X1C.json")
    if not os.path.exists(p):
        alt = _find_first_json(PRINTER_DIR)
        if not alt: raise FileNotFoundError(f"Printer-Profil fehlt: {os.path.join(PRINTER_DIR,'X1C.json')}")
        p = alt
    pr = os.path.join(PROCESS_DIR, "0.20mm_standard.json")
    if not os.path.exists(pr):
        alt = _find_first_json(PROCESS_DIR)
        if not alt: raise FileNotFoundError(f"Process-Profil fehlt: {os.path.join(PROCESS_DIR,'0.20mm_standard.json')}")
        pr = alt
    f = os.path.join(FILAMENT_DIR, f"{material.upper()}.json")
    if not os.path.exists(f):
        alt = _find_first_json(FILAMENT_DIR)
        if not alt: raise FileNotFoundError(f"Filament-Profil fehlt: {f}")
        f = alt
    return p, pr, f

def _prepare_profiles_in_tmp(tmp: str, material: str, infill: float, layer_height: float):
    printer_path, process_path, filament_path = _pick_repo_profiles(material)

    machine = _read_json(printer_path)
    process = _read_json(process_path)
    filament = _read_json(filament_path)

    machine, printer_name = _normalize_machine(machine)

    _strip_printer_specific_from(process)
    _strip_printer_specific_from(filament)
    _force_compatible_printers(process, printer_name)
    _force_compatible_printers(filament, printer_name)

    _merge_user_overrides(process, filament, material, infill, layer_height)

    p_path = os.path.join(tmp, "printer.json")
    pr_path = os.path.join(tmp, "process.json")
    f_path = os.path.join(tmp, "filament.json")
    _write_json(machine, p_path)
    _write_json(process, pr_path)
    _write_json(filament, f_path)

    return p_path, pr_path, f_path, machine, process, filament, printer_name, (printer_path, process_path, filament_path)

def _run_orca(tmpdir: str, p_path: str, pr_path: str, f_path: str, stl_path: str):
    out_3mf = os.path.join(tmpdir, "out.3mf")
    slicedata = os.path.join(tmpdir, "slicedata")
    merged = os.path.join(tmpdir, "merged_settings.json")
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
        "--export-settings", merged,
    ]
    code, out, err = _exec(cmd, timeout=180)
    details = {
        "cmd": " ".join(cmd),
        "stdout_tail": _safe_tail(out, 4000),
        "stderr_tail": _safe_tail(err, 2000),
        "settings_tail": "",
        "out_3mf_exists": os.path.exists(out_3mf),
        "slicedata_exists": os.path.exists(slicedata),
        "merged_settings_exists": os.path.exists(merged),
    }
    if os.path.exists(merged):
        try:
            with open(merged, "r", encoding="utf-8") as f:
                details["settings_tail"] = _safe_tail(f.read(), 4000)
        except Exception:
            pass
    return code, details

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    if not os.path.exists(ORCA_BIN):
        return JSONResponse(status_code=500, content={"detail": {"ok": False, "message": f"Slicer nicht gefunden: {ORCA_BIN}"}})

    with tempfile.TemporaryDirectory(prefix="fixedp_") as tmp:
        stl_path = os.path.join(tmp, "input.stl")
        content = await file.read()
        with open(stl_path, "wb") as f:
            f.write(content)

        try:
            p_path, pr_path, f_path, machine, process, filament, printer_name, repo_paths = _prepare_profiles_in_tmp(
                tmp, material, infill, layer_height
            )
        except FileNotFoundError as e:
            return JSONResponse(status_code=404, content={"detail": str(e)})
        except json.JSONDecodeError as e:
            return JSONResponse(status_code=400, content={"detail": f"JSON-Fehler in Profilen: {e}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"Profil-Vorbereitung fehlgeschlagen: {e}"})

        code, details = _run_orca(tmp, p_path, pr_path, f_path, stl_path)

        # kurze Sicht auf die Normalisierung (Typkontrolle)
        norm_preview = {
            "machine": {k: machine.get(k) for k in (
                "name","printer_technology","gcode_flavor","bed_shape",
                "max_print_height","min_layer_height","max_layer_height",
                "extruders","nozzle_diameter"
            )},
            "process": {
                "compatible_printers": process.get("compatible_printers"),
                "sparse_infill_density": process.get("sparse_infill_density"),
                "layer_height": process.get("layer_height"),
            },
            "filament": {
                "name": filament.get("name"),
                "compatible_printers": filament.get("compatible_printers"),
            }
        }

        resp = {
            "detail": {
                "ok": code == 0,
                "code": code,
                **details,
                "profiles_used": {
                    "printer_name": printer_name,
                    "printer_path": repo_paths[0],
                    "process_path": repo_paths[1],
                    "filament_path": repo_paths[2],
                },
                "inputs": {
                    "unit": unit, "material": material, "infill": infill,
                    "layer_height": layer_height, "nozzle": nozzle, "stl_bytes": len(content),
                },
                "normalized_preview": norm_preview,
            }
        }
        status = 200 if code in (0, 239) else 500
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

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT","8000")), reload=False)
