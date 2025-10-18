# app.py
import os
import re
import json
import shutil
import tempfile
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

app = FastAPI(title="Orca CLI Wrapper", version="1.0.0")

# -------------------------------------------------------------------
# Konfiguration / Pfade
# -------------------------------------------------------------------
ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/bin/orca-slicer")
PROFILES_BASE = os.environ.get("PROFILES_BASE", "/app/profiles")
PRINTER_DIR = os.path.join(PROFILES_BASE, "printer")
PROCESS_DIR = os.path.join(PROFILES_BASE, "process")
FILAMENT_DIR = os.path.join(PROFILES_BASE, "filament")
BUNDLE_STRUCTURE_PATH = os.environ.get("BUNDLE_STRUCTURE", "/app/bundle_structure.json")

# feste Layer-Grenzen wie gefordert:
FORCED_MIN_LAYER = 0.15
FORCED_MAX_LAYER = 0.30

# Standard-Dateien, falls bundle_structure.json fehlt
DEFAULTS = {
    "printer": os.path.join(PRINTER_DIR, "X1C.json"),
    "process": os.path.join(PROCESS_DIR, "0.20mm_standard.json"),
    "filament": os.path.join(FILAMENT_DIR, "PLA.json"),
}

# Felder mit Zahlwerten im Machine-Profil
NUM_FIELDS_MACHINE_FLOAT = {
    "max_print_height", "min_layer_height", "max_layer_height",
    "extruder_clearance_radius", "extruder_clearance_height_to_rod",
    "extruder_clearance_height_to_lid",
}
NUM_FIELDS_MACHINE_INT = {"extruders"}
NUM_LIST_FIELDS_MACHINE_FLOAT = {"nozzle_diameter"}
BED_SHAPE_KEYS = ("bed_shape", "printable_area")  # akzeptieren beide Aliasse

# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------
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
        sx = x.strip().replace(",", ".").replace("%", "")
        try:
            return float(sx)
        except Exception:
            return None
    return None

def to_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, int):
        return x
    f = to_float(x)
    return int(f) if f is not None else None

def ensure_num(d: Dict[str, Any], key: str, kind: str) -> None:
    if kind == "int":
        v = to_int(d.get(key))
        if v is not None:
            d[key] = v
        else:
            d.pop(key, None)
    else:
        v = to_float(d.get(key))
        if v is not None:
            d[key] = v
        else:
            d.pop(key, None)

def ensure_num_list(d: Dict[str, Any], key: str) -> None:
    val = d.get(key)
    if val is None:
        return
    if not isinstance(val, list):
        val = [val]
    out: List[float] = []
    for item in val:
        f = to_float(item)
        if f is not None:
            out.append(f)
    if out:
        d[key] = out
    else:
        d.pop(key, None)

def parse_bed_shape_value(val: Any) -> Optional[List[List[float]]]:
    """
    Akzeptiert:
      - [["0","0"],["400","0"],["400","400"],["0","400"]]
      - [[0,0],[400,0],[400,400],[0,400]]
      - ["0x0","400x0","400x400","0x400"]
    """
    if isinstance(val, list):
        if val and isinstance(val[0], list):
            pts = []
            for p in val:
                if not isinstance(p, (list, tuple)) or len(p) != 2:
                    return None
                x = to_float(p[0])
                y = to_float(p[1])
                if x is None or y is None:
                    return None
                pts.append([x, y])
            return pts
        elif val and isinstance(val[0], str):
            pts = []
            for s in val:
                m = re.match(r"^\s*([+-]?\d+(?:[.,]\d+)?)x([+-]?\d+(?:[.,]\d+)?)\s*$", s)
                if not m:
                    return None
                x = to_float(m.group(1))
                y = to_float(m.group(2))
                if x is None or y is None:
                    return None
                pts.append([x, y])
            return pts
    return None

def normalize_bed_shape(machine: Dict[str, Any]) -> None:
    for k in BED_SHAPE_KEYS:
        if k in machine:
            pts = parse_bed_shape_value(machine.get(k))
            if pts:
                machine["bed_shape"] = pts
                break
    # falls keiner passte: Rechteck 200x200 als Fallback
    if "bed_shape" not in machine:
        machine["bed_shape"] = [[0.0, 0.0], [200.0, 0.0], [200.0, 200.0], [0.0, 200.0]]

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def clamp_layer_heights(process: Dict[str, Any]) -> None:
    # Prozess-Layerhart in das feste Band [0.15, 0.30]
    if "layer_height" in process:
        lh = to_float(process.get("layer_height"))
        if lh is not None:
            process["layer_height"] = round(clamp(lh, FORCED_MIN_LAYER, FORCED_MAX_LAYER), 3)
    # First/Initial Layer falls vorhanden
    for key in ("first_layer_height", "initial_layer_height"):
        if key in process:
            ilh = to_float(process.get(key))
            if ilh is not None:
                process[key] = round(clamp(ilh, FORCED_MIN_LAYER, FORCED_MAX_LAYER), 3)

def inject_compat(printer_name: str, process: Dict[str, Any], filament: Dict[str, Any]) -> None:
    cp = set(process.get("compatible_printers") or [])
    cp.add(printer_name)
    process["compatible_printers"] = sorted(cp)

    cf = set(filament.get("compatible_printers") or [])
    cf.add(printer_name)
    filament["compatible_printers"] = sorted(cf)

# -------------------------------------------------------------------
# Normalisierung der Profile
# -------------------------------------------------------------------
def normalize_machine(machine: Dict[str, Any]) -> None:
    machine["type"] = "machine"
    machine["version"] = machine.get("version", "1")
    machine["from"] = machine.get("from", "user")

    # Zahlenfelder
    for k in list(NUM_FIELDS_MACHINE_FLOAT):
        ensure_num(machine, k, "float")
    for k in list(NUM_FIELDS_MACHINE_INT):
        ensure_num(machine, k, "int")
    for k in list(NUM_LIST_FIELDS_MACHINE_FLOAT):
        ensure_num_list(machine, k)

    # Bed-Shape
    normalize_bed_shape(machine)

    # Pflichtfelder/Defaults
    if "extruders" not in machine:
        machine["extruders"] = 1
    if "nozzle_diameter" not in machine or not machine["nozzle_diameter"]:
        machine["nozzle_diameter"] = [0.4]
    if "gcode_flavor" not in machine:
        machine["gcode_flavor"] = "marlin"
    if not machine.get("printer_technology"):
        machine["printer_technology"] = "FFF"
    if "max_print_height" not in machine:
        machine["max_print_height"] = 300.0

    # Deine festen Bounds überschreiben ggf. vorhandene/fehlerhafte Werte
    machine["min_layer_height"] = FORCED_MIN_LAYER
    machine["max_layer_height"] = FORCED_MAX_LAYER

def normalize_process(process: Dict[str, Any]) -> None:
    process["type"] = "process"
    process["version"] = process.get("version", "1")
    process["from"] = process.get("from", "user")

    # Layerhöhen clampen
    clamp_layer_heights(process)

    # lineare Dichten: "35%" -> "35%"
    # Orca akzeptiert Prozentstrings, also lassen wir sie wie sie sind.
    # Falls numerisch, zu Prozentstring wandeln:
    for dens_key in ("sparse_infill_density", "fill_density"):
        if dens_key in process:
            val = process[dens_key]
            if isinstance(val, (int, float)):
                process[dens_key] = f"{int(round(val*100))}%"

def normalize_filament(filament: Dict[str, Any]) -> None:
    filament["type"] = filament.get("type", "filament")
    filament["from"] = filament.get("from", "user")
    # Diverse Filament-Felder dürfen Listen sein; wenn Strings drin sind, konvertieren wir sinnvoll
    for key in ("filament_flow_ratio", "nozzle_temperature", "nozzle_temperature_initial_layer",
                "bed_temperature", "bed_temperature_initial_layer", "filament_diameter", "filament_density"):
        if key in filament:
            val = filament[key]
            if not isinstance(val, list):
                val = [val]
            new_list: List[Any] = []
            for item in val:
                f = to_float(item)
                new_list.append(f if f is not None else item)
            filament[key] = new_list

# -------------------------------------------------------------------
# Bundle structure laden (optional)
# -------------------------------------------------------------------
def read_bundle_structure() -> Optional[Dict[str, Any]]:
    try:
        if os.path.isfile(BUNDLE_STRUCTURE_PATH):
            return load_json(BUNDLE_STRUCTURE_PATH)
    except Exception:
        return None
    return None

def resolve_profile_paths(bundle: Optional[Dict[str, Any]]) -> Tuple[str, str, str, str]:
    """
    Liefert (printer_path, process_path, filament_path, printer_preset_name)
    """
    printer_preset_name = "RatRig V-Core 4 400 0.4 nozzle"
    if bundle:
        # Pfade relativ zu /app
        def _first(path_list_key: str, fallback: str) -> str:
            lst = bundle.get(path_list_key) or []
            if lst:
                return os.path.join("/app", lst[0])
            return fallback

        printer_path = _first("printer_config", DEFAULTS["printer"])
        process_path = _first("process_config", DEFAULTS["process"])
        filament_path = _first("filament_config", DEFAULTS["filament"])
        printer_preset_name = bundle.get("printer_preset_name", printer_preset_name)
        return printer_path, process_path, filament_path, printer_preset_name

    # Fallback ohne bundle
    return DEFAULTS["printer"], DEFAULTS["process"], DEFAULTS["filament"], printer_preset_name

# -------------------------------------------------------------------
# Slicing
# -------------------------------------------------------------------
def build_temp_configs(
    stl: bytes,
    unit: str,
    material: str,
    infill: float,
    layer_height: float,
    nozzle: float
) -> Tuple[str, Dict[str, Any]]:
    """
    Baut ein Temp-Verzeichnis mit gehärteten JSONs (printer.json/process.json/filament.json)
    und speichert die STL. Gibt tempdir + Preview zurück.
    """
    tmpdir = tempfile.mkdtemp(prefix="fixedp_")
    preview: Dict[str, Any] = {}

    # Eingabedatei
    stl_path = os.path.join(tmpdir, "input.stl")
    with open(stl_path, "wb") as f:
        f.write(stl)

    # Bundle
    bundle = read_bundle_structure()
    printer_src, process_src, filament_src, printer_name = resolve_profile_paths(bundle)

    # Dateien laden
    try:
        machine = load_json(printer_src)
        process = load_json(process_src)
        filament = load_json(filament_src)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Profil-Parsing fehlgeschlagen: {e}")

    # Normalisieren
    normalize_machine(machine)
    normalize_process(process)
    normalize_filament(filament)

    # Overrides durch Request
    # Nozzle
    machine["nozzle_diameter"] = [float(nozzle)]
    # Layerhöhe (wird im normalize bereits geklemmt, hier erneut absichern)
    process["layer_height"] = float(layer_height)
    clamp_layer_heights(process)
    # Infill (als Prozentstring)
    process["sparse_infill_density"] = f"{int(round(float(infill)*100))}%"

    # Kompatibilität erzwingen
    # Druckername aus bundle_structure oder Fallback nutzen
    machine["name"] = printer_name
    inject_compat(printer_name, process, filament)

    # Preview zurückgeben
    preview["machine"] = {
        "name": machine.get("name"),
        "printer_technology": machine.get("printer_technology"),
        "gcode_flavor": machine.get("gcode_flavor"),
        "bed_shape": machine.get("bed_shape"),
        "max_print_height": machine.get("max_print_height"),
        "min_layer_height": machine.get("min_layer_height"),
        "max_layer_height": machine.get("max_layer_height"),
        "extruders": machine.get("extruders"),
        "nozzle_diameter": machine.get("nozzle_diameter"),
    }
    preview["process"] = {
        "compatible_printers": process.get("compatible_printers"),
        "sparse_infill_density": process.get("sparse_infill_density"),
        "layer_height": process.get("layer_height"),
    }
    preview["filament"] = {
        "name": filament.get("name", "Filament"),
        "compatible_printers": filament.get("compatible_printers"),
    }

    # Gehärtete Dateien schreiben
    printer_out = os.path.join(tmpdir, "printer.json")
    process_out = os.path.join(tmpdir, "process.json")
    filament_out = os.path.join(tmpdir, "filament.json")
    save_json(printer_out, machine)
    save_json(process_out, process)
    save_json(filament_out, filament)

    # bundle_structure (nur Infoausgabe im Env)
    if bundle:
        save_json(os.path.join(tmpdir, "bundle_structure.json"), bundle)

    return tmpdir, preview

def run_orca_slice(tmpdir: str, debug_level: int = 4) -> Dict[str, Any]:
    printer = os.path.join(tmpdir, "printer.json")
    process = os.path.join(tmpdir, "process.json")
    filament = os.path.join(tmpdir, "filament.json")
    stl = os.path.join(tmpdir, "input.stl")

    out_3mf = os.path.join(tmpdir, "out.3mf")
    slicedata = os.path.join(tmpdir, "slicedata")
    merged = os.path.join(tmpdir, "merged_settings.json")

    cmd = [
        "xvfb-run", "-a", ORCA_BIN,
        "--debug", str(debug_level),
        "--datadir", tmpdir,
        "--load-settings", printer,
        "--load-settings", process,
        "--load-filaments", filament,
        "--arrange", "1",
        "--orient", "1",
        stl,
        "--slice", "1",
        "--export-3mf", out_3mf,
        "--export-slicedata", slicedata,
        "--export-settings", merged
    ]

    r = subprocess.run(
        " ".join(cmd), shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace"
    )
    stdout_tail = r.stdout[-1500:]
    stderr_tail = r.stderr[-1500:]
    result = {
        "code": r.returncode,
        "cmd": " ".join(cmd),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "out_3mf_exists": os.path.isfile(out_3mf),
        "slicedata_exists": os.path.isdir(slicedata),
        "merged_settings_exists": os.path.isfile(merged),
    }
    return result

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    # kein f-string (um { } zu vermeiden), reine Stringliteral:
    return HTMLResponse("""
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Orca CLI Test</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;max-width:900px;margin:40px auto;padding:0 16px}
section{border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0}
button{padding:8px 14px;border-radius:8px;border:1px solid #888;background:#f5f5f5;cursor:pointer}
pre{white-space:pre-wrap;background:#fafafa;border:1px dashed #ddd;padding:10px;border-radius:8px}
label{display:block;margin:.5rem 0 .25rem}
input[type="number"]{width:120px}
</style>
</head>
<body>
<h1>OrcaSlicer CLI – Testoberfläche</h1>

<section>
  <h2>Health</h2>
  <button onclick="q('/health')">/health</button>
  <pre id="health"></pre>
</section>

<section>
  <h2>Slicer Env</h2>
  <button onclick="q('/slicer_env')">/slicer_env</button>
  <pre id="env"></pre>
</section>

<section>
  <h2>/slice_check</h2>
  <form id="f" onsubmit="return sendSlice(event)">
    <label>STL-Datei</label>
    <input type="file" name="file" accept=".stl" required />
    <label>Unit</label>
    <select name="unit"><option>mm</option><option>inch</option></select>
    <label>Material</label>
    <input name="material" value="PLA" />
    <label>Infill (0..1)</label>
    <input type="number" step="0.01" min="0" max="1" name="infill" value="0.20"/>
    <label>Layer height (mm) – wird auf [0.15..0.30] geklemmt</label>
    <input type="number" step="0.01" min="0.05" max="0.4" name="layer_height" value="0.20"/>
    <label>Nozzle (mm)</label>
    <input type="number" step="0.01" min="0.1" max="1.2" name="nozzle" value="0.40"/>
    <div style="margin-top:10px">
      <button type="submit">Slicing testen</button>
    </div>
  </form>
  <pre id="slice"></pre>
</section>

<section>
  <a href="/docs" target="_blank">Swagger (API-Doku) öffnen</a>
</section>

<script>
function q(path){
  fetch(path).then(r => r.text()).then(t => {
    const id = path.replace('/','');
    document.getElementById(id||'health').textContent = t;
  }).catch(e => alert(e));
}
function sendSlice(e){
  e.preventDefault();
  const fd = new FormData(document.getElementById('f'));
  fetch('/slice_check',{method:'POST', body:fd})
    .then(r=>r.text()).then(t=>{document.getElementById('slice').textContent=t})
    .catch(err=>{document.getElementById('slice').textContent=String(err)});
  return false;
}
</script>
</body>
</html>
""")

@app.get("/health")
def health():
    return JSONResponse({"ok": True, "version": app.version})

@app.get("/slicer_env")
def slicer_env():
    profiles = {
        "printer": [],
        "process": [],
        "filament": []
    }
    for d, key in ((PRINTER_DIR, "printer"), (PROCESS_DIR, "process"), (FILAMENT_DIR, "filament")):
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(".json"):
                    profiles[key].append(os.path.join(d, fn))
    bundle = read_bundle_structure()
    return JSONResponse({
        "ok": True,
        "slicer_bin": ORCA_BIN,
        "slicer_present": os.path.isfile(ORCA_BIN),
        "profiles": profiles,
        "bundle_structure": bundle
    })

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
        tmpdir, preview = build_temp_configs(
            stl=stl_bytes,
            unit=unit,
            material=material,
            infill=infill,
            layer_height=layer_height,
            nozzle=nozzle
        )
        res = run_orca_slice(tmpdir, debug_level=4)
        detail = {
            "ok": bool(res["code"] == 0),
            "code": res["code"],
            "cmd": res["cmd"],
            "stdout_tail": res["stdout_tail"],
            "stderr_tail": res["stderr_tail"],
            "out_3mf_exists": res["out_3mf_exists"],
            "slicedata_exists": res["slicedata_exists"],
            "merged_settings_exists": res["merged_settings_exists"],
            "normalized_preview": preview,
        }
        # Profile-Quellen (zur Transparenz)
        bundle = read_bundle_structure()
        p_src, prc_src, fil_src, printer_name = resolve_profile_paths(bundle)
        detail["profiles_used"] = {
            "printer_name": printer_name,
            "printer_path": p_src,
            "process_path": prc_src,
            "filament_path": fil_src
        }
        detail["inputs"] = {
            "unit": unit, "material": material,
            "infill": infill, "layer_height": layer_height,
            "nozzle": nozzle, "stl_bytes": len(stl_bytes)
        }

        if res["code"] == 0:
            return JSONResponse(detail)
        else:
            return JSONResponse({"detail": detail}, status_code=500)
    except RuntimeError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=500)

# Optionaler direkter Slice-Endpunkt (identisch, nur Name anders)
@app.post("/slice")
async def slice_endpoint(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.20),
    layer_height: float = Form(0.20),
    nozzle: float = Form(0.40),
):
    return await slice_check(file, unit, material, infill, layer_height, nozzle)

# -------------------------------------------------------------------
# Uvicorn Entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
