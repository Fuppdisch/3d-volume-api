# app.py
import json
import os
import shutil
import tempfile
import uuid
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, File, UploadFile, Form
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

# ------------------------------------------------------------
# Konstanten & Pfade
# ------------------------------------------------------------
APP_VERSION = "1.0.0"
SLICER_BIN = Path("/opt/orca/bin/orca-slicer")

PROFILES_DIR = Path("/app/profiles")
DEFAULT_PRINTER_FILE = PROFILES_DIR / "printer" / "X1C.json"
DEFAULT_PROCESS_FILE  = PROFILES_DIR / "process" / "0.20mm_standard.json"
DEFAULT_FILAMENT_FILE = PROFILES_DIR / "filament" / "PLA.json"
BUNDLE_FILE = PROFILES_DIR / "bundle_structure.json"

# Felder, die häufig fälschlich als String statt Zahl kommen
NUM_FIELDS_MACHINE = {
    "max_print_height", "min_layer_height", "max_layer_height", "extruders",
    "extruder_clearance_radius", "extruder_clearance_height_to_rod", "extruder_clearance_height_to_lid"
}
NUM_LIST_FIELDS_MACHINE = {
    "nozzle_diameter"
}
# Prozess-Zahlfelder
NUM_FIELDS_PROCESS = {
    "layer_height", "first_layer_height", "initial_layer_height", "line_width",
    "perimeter_extrusion_width", "external_perimeter_extrusion_width", "infill_extrusion_width",
    "perimeters", "top_solid_layers", "bottom_solid_layers",
    "outer_wall_speed", "inner_wall_speed", "travel_speed",
    "outer_wall_acceleration", "inner_wall_acceleration", "travel_acceleration"
}
# Filament-Zahlfelder (werden in Orca meist als Liste erwartet)
NUM_LIST_FIELDS_FILAMENT = {
    "filament_flow_ratio", "filament_max_volumetric_speed", "filament_z_hop",
    "pressure_advance", "slow_down_layer_time",
    "nozzle_temperature_initial_layer", "nozzle_temperature",
    "bed_temperature", "bed_temperature_initial_layer",
    "filament_diameter", "filament_density"
}

# ------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------
app = FastAPI(title="OrcaSlicer API", version=APP_VERSION)


# ------------------------------------------------------------
# Hilfen: IO
# ------------------------------------------------------------
def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def coerce_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", ".")
        # Prozente im Process bleiben als String ("20%"), aber layer_height etc. nicht
        if s.endswith("%"):
            try:
                return float(s[:-1])
            except:
                return None
        try:
            return float(s)
        except:
            return None
    return None


def coerce_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x)
    if isinstance(x, str):
        try:
            return int(float(x.strip().replace(",", ".")))
        except:
            return None
    return None


def ensure_num(obj: Dict[str, Any], key: str, kind: str = "float") -> None:
    if key not in obj:
        return
    if kind == "float":
        v = coerce_float(obj[key])
        if v is not None:
            obj[key] = v
        else:
            del obj[key]
    elif kind == "int":
        v = coerce_int(obj[key])
        if v is not None:
            obj[key] = v
        else:
            del obj[key]


def ensure_num_list(obj: Dict[str, Any], key: str) -> None:
    if key not in obj:
        return
    v = obj[key]
    if isinstance(v, list):
        newv = []
        for item in v:
            fv = coerce_float(item)
            if fv is not None:
                newv.append(fv)
        if newv:
            obj[key] = newv
        else:
            del obj[key]
    else:
        fv = coerce_float(v)
        if fv is not None:
            obj[key] = [fv]
        else:
            del obj[key]


def normalize_bed_shape(machine: Dict[str, Any]) -> None:
    # akzeptiere "bed_shape" als Liste von Paaren oder {printable_area:["0x0","400x0",...]}
    if "bed_shape" in machine and isinstance(machine["bed_shape"], list):
        pts = []
        for p in machine["bed_shape"]:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                pts.append([coerce_float(p[0]) or 0.0, coerce_float(p[1]) or 0.0])
        if pts:
            machine["bed_shape"] = pts
            return
    # printable_area als "0x0" Strings
    if "printable_area" in machine and isinstance(machine["printable_area"], list):
        pts = []
        for s in machine["printable_area"]:
            if isinstance(s, str) and "x" in s:
                xy = s.split("x", 1)
                if len(xy) == 2:
                    fx = coerce_float(xy[0])
                    fy = coerce_float(xy[1])
                    if fx is not None and fy is not None:
                        pts.append([fx, fy])
        if pts:
            machine["bed_shape"] = pts
            machine.pop("printable_area", None)


def normalize_machine(machine: Dict[str, Any]) -> None:
    # Pflichtfelder
    machine["type"] = "machine"
    machine["version"] = machine.get("version", "1")
    machine["from"] = machine.get("from", "user")

    # Zahlenfelder
    for k in list(machine.keys()):
        if k in NUM_FIELDS_MACHINE:
            kind = "int" if k in {"extruders"} else "float"
            ensure_num(machine, k, kind=kind)
    for k in NUM_LIST_FIELDS_MACHINE:
        ensure_num_list(machine, k)

    # bed shape
    normalize_bed_shape(machine)

    # max_print_height Alias: printable_height -> max_print_height
    if "max_print_height" not in machine and "printable_height" in machine:
        ensure_num(machine, "printable_height", "float")
        if isinstance(machine.get("printable_height"), (int, float)):
            machine["max_print_height"] = float(machine["printable_height"])
        machine.pop("printable_height", None)

    # extruders & nozzle_diameter Default
    if "extruders" not in machine:
        machine["extruders"] = 1
    if "nozzle_diameter" not in machine:
        machine["nozzle_diameter"] = [0.4]
    # gcode flavor
    if "gcode_flavor" not in machine:
        machine["gcode_flavor"] = "marlin"


def normalize_process(proc: Dict[str, Any]) -> None:
    proc["type"] = "process"
    proc["version"] = proc.get("version", "1")
    proc["from"] = proc.get("from", "user")

    # Zahlen erzwingen
    for k in list(proc.keys()):
        if k in NUM_FIELDS_PROCESS:
            # Layer-Höhen sind floats, counts ints
            if k in {"perimeters", "top_solid_layers", "bottom_solid_layers"}:
                ensure_num(proc, k, "int")
            else:
                ensure_num(proc, k, "float")

    # Infill kann "20%" (String) sein – lassen wir zu. Wenn Zahl, in Prozentstring wandeln
    if "sparse_infill_density" in proc:
        v = proc["sparse_infill_density"]
        if isinstance(v, (int, float)):
            proc["sparse_infill_density"] = f"{int(v)}%"
        elif isinstance(v, str) and v.strip().endswith("%"):
            proc["sparse_infill_density"] = v.strip()
        else:
            # unbekannt -> entfernen, Slicer defaultet
            proc.pop("sparse_infill_density", None)

    # aus Kompatibilitätsgründen KEINE extruders/nozzle_diameter im Process
    proc.pop("extruders", None)
    proc.pop("nozzle_diameter", None)


def normalize_filament(fila: Dict[str, Any]) -> None:
    fila["type"] = "filament"
    fila["from"] = fila.get("from", "user")

    for k in NUM_LIST_FIELDS_FILAMENT:
        if k in fila:
            ensure_num_list(fila, k)

    # KEINE extruders/nozzle_diameter im Filament
    fila.pop("extruders", None)
    fila.pop("nozzle_diameter", None)


def ensure_compatible_printer(proc: Dict[str, Any], fila: Dict[str, Any], printer_name: str) -> None:
    # Process
    cp = proc.get("compatible_printers")
    if not isinstance(cp, list):
        cp = []
    if printer_name not in cp and "*" not in cp:
        cp.append(printer_name)
    proc["compatible_printers"] = cp
    proc.setdefault("compatible_printers_condition", "")

    # Filament
    cf = fila.get("compatible_printers")
    if not isinstance(cf, list):
        cf = []
    if printer_name not in cf and "*" not in cf:
        cf.append(printer_name)
    fila["compatible_printers"] = cf
    fila.setdefault("compatible_printers_condition", "")


def read_bundle_structure() -> Optional[Dict[str, Any]]:
    if not BUNDLE_FILE.exists():
        return None
    try:
        return read_json(BUNDLE_FILE)
    except Exception:
        return None


def resolve_profiles_from_bundle() -> Optional[Dict[str, Path]]:
    """Liest bundle_structure.json und mappt relative Einträge auf /app/profiles/*."""
    bundle = read_bundle_structure()
    if not bundle:
        return None
    p_conf = bundle.get("printer_config") or []
    r_conf = bundle.get("process_config") or []
    f_conf = bundle.get("filament_config") or []
    if not (p_conf and r_conf and f_conf):
        return None

    def to_abs(rel: str) -> Path:
        return (PROFILES_DIR / rel).resolve()

    p_path = to_abs(p_conf[0])
    r_path = to_abs(r_conf[0])
    f_path = to_abs(f_conf[0])
    if not (p_path.exists() and r_path.exists() and f_path.exists()):
        return None
    return {"printer": p_path, "process": r_path, "filament": f_path}


def orca_present() -> bool:
    return SLICER_BIN.exists() and os.access(SLICER_BIN, os.X_OK)


# ------------------------------------------------------------
# Endpunkte
# ------------------------------------------------------------
HOME_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>OrcaSlicer API</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;max-width:900px;margin:40px auto;padding:0 16px}
section{border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:12px 0}
h1{margin:0 0 8px}
code,pre{background:#f6f7f9;border-radius:8px;padding:2px 6px}
button{padding:8px 12px;border-radius:10px;border:1px solid #d1d5db;background:#fff;cursor:pointer}
input,select{padding:6px 8px;border:1px solid #d1d5db;border-radius:8px}
#log{white-space:pre-wrap}
</style>
</head>
<body>
<h1>OrcaSlicer API</h1>

<section>
  <h3>Health</h3>
  <button onclick="fetch('/health').then(r=>r.text()).then(t=>log(t))">/health</button>
</section>

<section>
  <h3>Umgebung</h3>
  <button onclick="fetch('/slicer_env').then(r=>r.json()).then(t=>log(JSON.stringify(t,null,2)))">/slicer_env</button>
  <button onclick="fetch('/bundle_env').then(r=>r.json()).then(t=>log(JSON.stringify(t,null,2)))">/bundle_env</button>
</section>

<section>
  <h3>Slice Check</h3>
  <form id="f" onsubmit="event.preventDefault(); doSlice();">
    <div><label>Datei (STL): <input type="file" name="file" required/></label></div>
    <div><label>Unit:
      <select name="unit">
        <option value="mm" selected>mm</option>
        <option value="inch">inch</option>
      </select></label>
    </div>
    <div><label>Material: <input name="material" value="PLA"/></label></div>
    <div><label>Infill: <input name="infill" value="0.2"/></label></div>
    <div><label>Layer Height: <input name="layer_height" value="0.2"/></label></div>
    <div><label>Nozzle: <input name="nozzle" value="0.4"/></label></div>
    <div style="margin-top:8px"><button type="submit">/slice_check ausführen</button></div>
  </form>
</section>

<section>
  <h3>Log</h3>
  <pre id="log"></pre>
</section>

<script>
function log(t){document.getElementById('log').textContent = t;}
async function doSlice(){
  const fd = new FormData(document.getElementById('f'));
  const r = await fetch('/slice_check', { method:'POST', body: fd });
  const j = await r.json();
  log(JSON.stringify(j,null,2));
}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HOME_HTML


@app.get("/health")
def health():
    return PlainTextResponse("ok")


@app.get("/slicer_env")
def slicer_env():
    profiles = {
        "printer": [str(p) for p in (PROFILES_DIR / "printer").glob("*.json")],
        "process": [str(p) for p in (PROFILES_DIR / "process").glob("*.json")],
        "filament": [str(p) for p in (PROFILES_DIR / "filament").glob("*.json")],
    }
    bundle = read_bundle_structure()
    return {
        "ok": True,
        "slicer_bin": str(SLICER_BIN),
        "slicer_present": orca_present(),
        "profiles": profiles,
        "bundle_structure": bundle,
    }


@app.get("/bundle_env")
def bundle_env():
    paths = resolve_profiles_from_bundle()
    bundle = read_bundle_structure()
    return {
        "ok": True,
        "bundle": bundle,
        "resolved": {k: str(v) for k, v in (paths or {}).items()},
        "fallback_if_none": {
            "printer": str(DEFAULT_PRINTER_FILE),
            "process": str(DEFAULT_PROCESS_FILE),
            "filament": str(DEFAULT_FILAMENT_FILE),
        },
    }


@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    if not orca_present():
        return JSONResponse(status_code=500, content={"detail": f"Slicer nicht vorhanden: {SLICER_BIN}"})

    # Profile-Pfade: zuerst Bundle, sonst Fallback
    paths = resolve_profiles_from_bundle()
    if paths:
        p_printer = paths["printer"]
        p_process = paths["process"]
        p_filament = paths["filament"]
    else:
        p_printer = DEFAULT_PRINTER_FILE
        p_process = DEFAULT_PROCESS_FILE
        p_filament = DEFAULT_FILAMENT_FILE

    # Profile laden
    try:
        machine_raw = read_json(p_printer)
        process_raw = read_json(p_process)
        filament_raw = read_json(p_filament)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Profil-Parsing fehlgeschlagen: {e}"})

    # Druckername
    printer_name = machine_raw.get("name") or "Generic 0.4 nozzle"

    # Normalisieren
    try:
        machine = json.loads(json.dumps(machine_raw))  # deep copy
        process = json.loads(json.dumps(process_raw))
        filament = json.loads(json.dumps(filament_raw))

        normalize_machine(machine)
        normalize_process(process)
        normalize_filament(filament)

        # Kompatibilität sicherstellen
        ensure_compatible_printer(process, filament, printer_name)

        # Benutzer-Overrides
        # layer_height
        if layer_height:
            process["layer_height"] = float(layer_height)
        # nozzle_diameter im Printer (Array)
        if nozzle:
            machine["nozzle_diameter"] = [float(nozzle)]
        # Infill in Prozent
        if infill is not None:
            iv = float(infill)
            if 0 <= iv <= 1:
                iv *= 100.0
            process["sparse_infill_density"] = f"{int(round(iv))}%"
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Normalisierung fehlgeschlagen: {e}"})

    # Temporärer Job-Ordner
    job_dir = Path(tempfile.mkdtemp(prefix="fixedp_"))
    try:
        stl_path = job_dir / "input.stl"
        with stl_path.open("wb") as dst:
            dst.write(await file.read())

        # Bereinigte Profile schreiben
        printer_out = job_dir / "printer.json"
        process_out = job_dir / "process.json"
        filament_out = job_dir / "filament.json"
        write_json(printer_out, machine)
        write_json(process_out, process)
        write_json(filament_out, filament)

        out_3mf = job_dir / "out.3mf"
        slicedata_dir = job_dir / "slicedata"
        merged_settings = job_dir / "merged_settings.json"

        # CLI-Kommando
        cmd = [
            "xvfb-run", "-a", str(SLICER_BIN),
            "--debug", "4",
            "--datadir", str(job_dir / "cfg"),
            "--load-settings", str(printer_out),
            "--load-settings", str(process_out),
            "--load-filaments", str(filament_out),
            "--arrange", "1",
            "--orient", "1",
            str(stl_path),
            "--slice", "1",
            "--export-3mf", str(out_3mf),
            "--export-slicedata", str(slicedata_dir),
            "--export-settings", str(merged_settings),
        ]

        # Ausführen
        proc_run = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )

        ok = proc_run.returncode == 0
        payload: Dict[str, Any] = {
            "ok": ok,
            "code": proc_run.returncode,
            "cmd": " ".join(cmd),
            "stdout_tail": proc_run.stdout[-1200:],
            "stderr_tail": proc_run.stderr[-1200:],
            "out_3mf_exists": out_3mf.exists(),
            "slicedata_exists": slicedata_dir.exists(),
            "merged_settings_exists": merged_settings.exists(),
            "profiles_used": {
                "printer_name": printer_name,
                "printer_path": str(p_printer),
                "process_path": str(p_process),
                "filament_path": str(p_filament),
            },
            "inputs": {
                "unit": unit, "material": material,
                "infill": infill, "layer_height": layer_height, "nozzle": nozzle,
                "stl_bytes": stl_path.stat().st_size if stl_path.exists() else None,
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
                },
                "process": {
                    "compatible_printers": process.get("compatible_printers"),
                    "sparse_infill_density": process.get("sparse_infill_density"),
                    "layer_height": process.get("layer_height"),
                },
                "filament": {
                    "name": filament.get("name"),
                    "compatible_printers": filament.get("compatible_printers"),
                },
            }
        }

        if not ok:
            # hilfreicher Hinweis bauen
            hints: Dict[str, Any] = {"summary": "Slicer-Fehler (siehe stdout_tail)"}
            # Heuristiken
            stdo = proc_run.stdout
            if "process not compatible with printer" in stdo:
                hints["summary"] = "process not compatible with printer"
                hints["note"] = "Wir haben compatible_printers gesetzt. Prüfe dennoch Druckername & Datentypen."
            if "invalid json" in stdo or "parse" in stdo:
                hints["summary"] = "Profil-Parsingproblem"
                hints["note"] = "Ein Feld hat evtl. falschen Typ. Unsere Normalisierung wandelt vieles, prüfe Preview."
            payload["hint"] = hints

            return JSONResponse(status_code=500, content={"detail": payload})

        # Erfolg
        return {"detail": payload}

    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=500, content={"detail": "Slicing Timeout."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Unerwarteter Fehler: {e}"})
    finally:
        # Artefakte zur Analyse behalten? -> hier NICHT löschen.
        # Zum Aufräumen auskommentieren:
        # shutil.rmtree(job_dir, ignore_errors=True)
        pass


# ------------------------------------------------------------
# Uvicorn Runner (lokal)
# ------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
