# app.py
import os
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Orca Slice Service", version="1.0.0")

# ====== Pfade (anpassen falls nötig) ==========================================================
ROOT = Path("/app")
PROFILES_DIR = ROOT / "profiles"
PRINTER_PATH = PROFILES_DIR / "printer" / "X1C.json"
PROCESS_PATH = PROFILES_DIR / "process" / "0.20mm_standard.json"
FILAMENT_PATH = PROFILES_DIR / "filament" / "PLA.json"
BUNDLE_STRUCTURE_PATH = PROFILES_DIR / "bundle_structure.json"  # optional
ORCA_BIN = Path(os.environ.get("ORCA_BIN", "/opt/orca/bin/orca-slicer"))

# ====== kleine HTML-Startseite ================================================================
INDEX_HTML = """
<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orca Slice Service</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:2rem;max-width:900px}
section{border:1px solid #ddd;border-radius:12px;padding:16px;margin-bottom:16px}
h1{margin-top:0} code,pre{background:#f6f8fa;border-radius:6px;padding:2px 6px}
button{padding:.6rem 1rem;border-radius:8px;border:1px solid #ccc;cursor:pointer}
input[type=file]{margin:.5rem 0}
small{color:#555}
#out{white-space:pre-wrap;background:#0b1020;color:#e4e7ef;padding:12px;border-radius:8px}
</style>
</head>
<body>
<h1>Orca Slice Service</h1>
<section>
  <button onclick="fetchTxt('/health')">/health</button>
  <button onclick="fetchTxt('/slicer_env')">/slicer_env</button>
  <a href="/docs" target="_blank"><button>Swagger (API-Doku)</button></a>
</section>
<section>
  <h3>/slice_check</h3>
  <form id="f" onsubmit="doSlice(event)">
    <input type="file" name="file" required accept=".stl,.3mf"/>
    <div>
      <label>unit</label>
      <select name="unit">
        <option value="mm" selected>mm</option>
        <option value="inch">inch</option>
      </select>
      <label>material</label>
      <input name="material" value="PLA">
      <label>infill</label>
      <input name="infill" value="0.2" type="number" step="0.01" min="0" max="1">
      <label>layer_height</label>
      <input name="layer_height" value="0.2" type="number" step="0.01">
      <label>nozzle</label>
      <input name="nozzle" value="0.4" type="number" step="0.01">
    </div>
    <p><button type="submit">Test-Run</button> <small>(lädt kein GCODE runter—nur Check & Logs)</small></p>
  </form>
</section>
<section>
  <h3>Ausgabe</h3>
  <div id="out">–</div>
</section>
<script>
async function fetchTxt(url){
  const res = await fetch(url);
  document.getElementById('out').textContent = JSON.stringify(await res.json(), null, 2);
}
async function doSlice(e){
  e.preventDefault();
  const fd = new FormData(document.getElementById('f'));
  const res = await fetch('/slice_check', { method:'POST', body: fd });
  const js = await res.json();
  document.getElementById('out').textContent = JSON.stringify(js, null, 2);
}
</script>
</body>
</html>
"""

# ====== Utility: robustes JSON-Laden ==========================================================
def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def to_float(x) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        return float(x.strip().replace(",", "."))
    raise ValueError(f"cannot convert to float: {x!r}")

def to_int(x) -> int:
    if isinstance(x, (int, float)):
        return int(x)
    if isinstance(x, str):
        return int(float(x.strip().replace(",", ".")))
    raise ValueError(f"cannot convert to int: {x!r}")

def normalize_bed_shape(v) -> List[List[float]]:
    # akzeptiert bereits [[0,0],...] oder ["0x0", "400x0", ...]
    if isinstance(v, list) and v and isinstance(v[0], list):
        return [[to_float(a), to_float(b)] for a, b in v]
    if isinstance(v, list) and v and isinstance(v[0], str):
        out = []
        for s in v:
            parts = s.lower().split("x")
            if len(parts) != 2:
                raise ValueError(f"bad bed_shape token: {s}")
            out.append([to_float(parts[0]), to_float(parts[1])])
        return out
    raise ValueError("bed_shape must be list of pairs or list of 'NxM' strings")

def as_float_list(v) -> List[float]:
    if isinstance(v, list):
        return [to_float(x) for x in v]
    return [to_float(v)]

def add_compat(name: str, lst: Optional[List[str]]) -> List[str]:
    s = set(lst or [])
    s.add(name)
    return sorted(s)

# ====== Normalizer: baut drei gültige Dateien für den CLI-Call ================================
def normalize_profiles(printer_path: Path, process_path: Path, filament_path: Path) -> Dict[str, Any]:
    machine = load_json(printer_path)
    process = load_json(process_path)
    filament = load_json(filament_path)

    # Druckername bestimmen (fällt zurück auf "name" oder "printer_model + variant")
    printer_name = machine.get("name") or "{} {}".format(
        machine.get("printer_model", "Printer"),
        machine.get("printer_variant", "")
    ).strip()

    # Machine: harte Typ-Normalisierung
    if "bed_shape" in machine:
        machine["bed_shape"] = normalize_bed_shape(machine["bed_shape"])
    # akzeptiere sowohl bed_shape als auch printable_area; wenn nur printable_area da → wandeln
    if "bed_shape" not in machine and "printable_area" in machine:
        machine["bed_shape"] = normalize_bed_shape(machine["printable_area"])
        machine.pop("printable_area", None)

    if "max_print_height" in machine:
        machine["max_print_height"] = to_float(machine["max_print_height"])
    if "min_layer_height" in machine:
        machine["min_layer_height"] = to_float(machine["min_layer_height"])
    if "max_layer_height" in machine:
        machine["max_layer_height"] = to_float(machine["max_layer_height"])

    # Extruder / Nozzle
    if "extruders" in machine:
        machine["extruders"] = to_int(machine["extruders"])
    else:
        machine["extruders"] = 1

    if "nozzle_diameter" in machine:
        machine["nozzle_diameter"] = as_float_list(machine["nozzle_diameter"])
    else:
        machine["nozzle_diameter"] = [0.4]

    # Pflichtfelder
    machine.setdefault("type", "machine")
    machine.setdefault("version", "1")
    machine.setdefault("from", "user")
    machine.setdefault("printer_technology", "FFF")
    machine.setdefault("gcode_flavor", "marlin")

    # Zusatz: model id hilft Orca intern beim Matching
    if "printer_model" in machine:
        machine["printer_model_id"] = machine.get("printer_model")

    # Process: Strings → Zahlen
    if "layer_height" in process:
        process["layer_height"] = to_float(process["layer_height"])
    if "first_layer_height" in process or "initial_layer_height" in process:
        v = process.get("first_layer_height", process.get("initial_layer_height"))
        process["first_layer_height"] = to_float(v)
        process.pop("initial_layer_height", None)

    # Dichte-Feld: 0..1 oder Prozent-String erlauben
    if "sparse_infill_density" in process:
        v = process["sparse_infill_density"]
        if isinstance(v, str) and v.strip().endswith("%"):
            process["sparse_infill_density"] = f"{v.strip()}"
        elif isinstance(v, (int, float)):
            # CLI versteht beide Varianten; behalten float bei, aber clampen 0..1
            process["sparse_infill_density"] = max(0.0, min(1.0, float(v)))
        else:
            process["sparse_infill_density"] = "20%"

    # Keine extruder/nozzle-Doppler in process/filament (führt oft zu „invalid json type“)
    for k in ("extruders", "nozzle_diameter"):
        process.pop(k, None)
        filament.pop(k, None)

    # Kompatibilität sicherstellen (exakter Druckername)
    process["compatible_printers"] = add_compat(printer_name, process.get("compatible_printers"))
    filament["compatible_printers"] = add_compat(printer_name, filament.get("compatible_printers"))

    # Kontextfelder (rein informativ, schaden aber nicht)
    for target in (process, filament):
        for key in ("printer_technology", "printer_model", "printer_variant"):
            if machine.get(key) is not None:
                target[key] = machine[key]
        if "printer_model_id" in machine:
            target["printer_model_id"] = machine["printer_model_id"]

    return {"machine": machine, "process": process, "filament": filament, "printer_name": printer_name}

# ====== Slicer-Aufruf ========================================================================
def run_orca(tempdir: Path, norm: Dict[str, Any], stl_path: Path) -> Dict[str, Any]:
    # schreibe normalisierte Profile
    p_machine = tempdir / "printer.json"
    p_process = tempdir / "process.json"
    p_filament = tempdir / "filament.json"
    p_merged = tempdir / "merged_settings.json"
    p_slicedata = tempdir / "slicedata"
    p_out3mf = tempdir / "out.3mf"
    p_result = tempdir / "result.json"  # wird i.d.R. von Orca geschrieben (siehe Logs)

    ensure_dir(p_slicedata)

    with p_machine.open("w", encoding="utf-8") as f:
        json.dump(norm["machine"], f, indent=2)
    with p_process.open("w", encoding="utf-8") as f:
        json.dump(norm["process"], f, indent=2)
    with p_filament.open("w", encoding="utf-8") as f:
        json.dump(norm["filament"], f, indent=2)

    cmd = [
        "xvfb-run", "-a", str(ORCA_BIN),
        "--debug", "4",
        "--datadir", str(tempdir / "cfg"),
        "--load-settings", str(p_machine) + ";" + str(p_process),
        "--load-filaments", str(p_filament),
        "--arrange", "1",
        "--orient", "1",
        str(stl_path),
        "--slice", "1",
        "--export-3mf", str(p_out3mf),
        "--export-slicedata", str(p_slicedata),
        "--export-settings", str(p_merged)
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    # best effort: result.json einlesen (falls vorhanden)
    result_json = None
    if p_result.exists():
        try:
            result_json = json.loads(p_result.read_text(encoding="utf-8"))
        except Exception:
            result_json = {"error": "failed to parse result.json", "raw": p_result.read_text(errors="ignore")[:2000]}

    return {
        "code": proc.returncode,
        "cmd": " ".join(cmd),
        "stdout_tail": proc.stdout[-1800:],
        "stderr_tail": proc.stderr[-1800:],
        "out_3mf_exists": p_out3mf.exists(),
        "slicedata_exists": p_slicedata.exists() and any(p_slicedata.iterdir()),
        "merged_settings_exists": p_merged.exists(),
        "result_json": result_json,
        "normalized_preview": {
            "machine": norm["machine"],
            "process": {
                "compatible_printers": norm["process"].get("compatible_printers", []),
                "sparse_infill_density": norm["process"].get("sparse_infill_density"),
                "layer_height": norm["process"].get("layer_height"),
            },
            "filament": {
                "name": norm["filament"].get("name"),
                "compatible_printers": norm["filament"].get("compatible_printers", []),
            }
        }
    }

# ====== Routes ===============================================================================

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

@app.get("/health")
def health():
    return {"ok": True, "version": app.version}

@app.get("/slicer_env")
def slicer_env():
    info = {
        "ok": True,
        "slicer_bin": str(ORCA_BIN),
        "slicer_present": ORCA_BIN.exists(),
        "profiles": {
            "printer": [str(PRINTER_PATH)],
            "process": [str(PROCESS_PATH)],
            "filament": [str(FILAMENT_PATH)],
        },
    }
    if BUNDLE_STRUCTURE_PATH.exists():
        try:
            info["bundle_structure"] = load_json(BUNDLE_STRUCTURE_PATH)
        except Exception as e:
            info["bundle_structure_error"] = str(e)
    return info

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    # Eingabedatei in Tempordner ablegen
    with tempfile.TemporaryDirectory(prefix="fixedp_") as td:
        tdir = Path(td)
        stl_path = tdir / ("input.stl" if file.filename.lower().endswith(".stl") else "input.3mf")
        stl_bytes = await file.read()
        stl_path.write_bytes(stl_bytes)

        # Profile laden & normalisieren
        try:
            norm = normalize_profiles(PRINTER_PATH, PROCESS_PATH, FILAMENT_PATH)
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Profil-Parsing fehlgeschlagen: {e}"}
            )

        # on-the-fly anpassen mit Formwerten (nur die, die sinnvoll sind)
        norm["process"]["layer_height"] = float(layer_height)
        # infill: akzeptiere float 0..1 → in Prozent-String umwandeln
        if 0.0 <= infill <= 1.0:
            perc = int(round(infill * 100))
            norm["process"]["sparse_infill_density"] = f"{perc}%"
        else:
            norm["process"]["sparse_infill_density"] = f"{infill}%"

        # nozzle → überschreibt machine.nozzle_diameter[0]
        if nozzle > 0:
            norm["machine"]["nozzle_diameter"] = [float(nozzle)]

        # Slicer ausführen
        result = run_orca(tdir, norm, stl_path)

        payload = {
            "ok": result["code"] == 0,
            "code": result["code"],
            "cmd": result["cmd"],
            "stdout_tail": result["stdout_tail"],
            "stderr_tail": result["stderr_tail"],
            "out_3mf_exists": result["out_3mf_exists"],
            "slicedata_exists": result["slicedata_exists"],
            "merged_settings_exists": result["merged_settings_exists"],
            "result_json": result["result_json"],
            "normalized_preview": result["normalized_preview"],
            "profiles_used": {
                "printer_path": str(PRINTER_PATH),
                "process_path": str(PROCESS_PATH),
                "filament_path": str(FILAMENT_PATH),
                "printer_name": norm["printer_name"],
            },
            "inputs": {
                "unit": unit, "material": material,
                "infill": infill, "layer_height": layer_height, "nozzle": nozzle,
                "stl_bytes": len(stl_bytes)
            }
        }

        status = 200 if payload["ok"] else 500
        return JSONResponse(status_code=status, content={"detail": payload})
