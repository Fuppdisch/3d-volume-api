# app.py
import os
import io
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# -------------------------------------------------------------------
# Konstante Pfade (deine Repo-Struktur)
# -------------------------------------------------------------------
APP_ROOT = Path("/app")
PROFILES_DIR = APP_ROOT / "profiles"
PRINTER_DIR = PROFILES_DIR / "printer"
PROCESS_DIR = PROFILES_DIR / "process"
FILAMENT_DIR = PROFILES_DIR / "filament"
BUNDLE_FILE = PROFILES_DIR / "bundle_structure.json"

# Default-Fallbacks (passen zu deiner Struktur)
DEFAULT_PRINTER_FILE = PRINTER_DIR / "X1C.json"
DEFAULT_PROCESS_FILE = PROCESS_DIR / "0.20mm_standard.json"
DEFAULT_FILAMENT_FILE = FILAMENT_DIR / "PLA.json"

# Orca CLI
ORCA_BIN = "/opt/orca/bin/orca-slicer"

# -------------------------------------------------------------------
# FastAPI
# -------------------------------------------------------------------
app = FastAPI(title="Orca CLI API", version="1.0.0")

# -------------------------------------------------------------------
# Hilfsfunktionen: robustes JSON-Laden/Schreiben
# -------------------------------------------------------------------
def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path: Path, data: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), indent=None)

def to_float(x, default: Optional[float] = None) -> float:
    if x is None:
        if default is None:
            raise ValueError("value is None and no default provided")
        return float(default)
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", ".")
        if s.endswith("%"):
            s = s[:-1]
        return float(s)
    if isinstance(x, list) and len(x) == 1:
        return to_float(x[0], default)
    raise ValueError(f"cannot convert to float: {x!r}")

def to_int(x, default: Optional[int] = None) -> int:
    if x is None:
        if default is None:
            raise ValueError("value is None and no default provided")
        return int(default)
    if isinstance(x, (int, float)):
        return int(x)
    if isinstance(x, str):
        return int(float(x.strip().replace(",", ".")))
    if isinstance(x, list) and len(x) == 1:
        return to_int(x[0], default)
    raise ValueError(f"cannot convert to int: {x!r}")

def to_float_list(xs, default: Optional[List[float]] = None) -> List[float]:
    if xs is None:
        if default is None:
            return []
        return [to_float(v) for v in default]
    if isinstance(xs, (int, float, str)):
        return [to_float(xs)]
    if isinstance(xs, list):
        return [to_float(v) for v in xs]
    raise ValueError(f"cannot convert to float list: {xs!r}")

def to_percent_string(x, default: str = "20%") -> str:
    if x is None:
        return default
    if isinstance(x, str):
        s = x.strip()
        if s.endswith("%"):
            return s
        # treat as numeric string -> percent
        val = to_float(s)
        if val <= 1.0:
            val *= 100.0
        return f"{int(round(val))}%"
    # numeric
    val = to_float(x)
    if val <= 1.0:
        val *= 100.0
    return f"{int(round(val))}%"

# -------------------------------------------------------------------
# Normalisierung der drei Profile
# -------------------------------------------------------------------
def normalize_machine(m: Dict[str, Any]) -> Dict[str, Any]:
    # Basis
    name = m.get("name") or "RatRig V-Core 4 400 0.4 nozzle"
    out = {
        "type": "machine",
        "version": "1",
        "from": "user",
        "name": name,
        "printer_technology": m.get("printer_technology", "FFF"),
        "gcode_flavor": m.get("gcode_flavor", "marlin"),
        # Bed shape als Zahlen-Paare
        "bed_shape": [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]],
        "max_print_height": 300.0,
        "min_layer_height": 0.06,
        "max_layer_height": 0.3,
        "extruders": 1,
        "nozzle_diameter": [0.4],
    }

    # Falls im Input korrekte Felder vorhanden sind -> sauber übernehmen
    try:
        if "bed_shape" in m and isinstance(m["bed_shape"], list):
            # Liste von [x,y]
            out["bed_shape"] = [[to_float(p[0]), to_float(p[1])] for p in m["bed_shape"]]
    except Exception:
        # Wenn bed_shape in anderer Form vorlag, fallback auf default
        pass

    # Konvertiere Typen robust
    for k, default in [
        ("max_print_height", 300.0),
        ("min_layer_height", 0.06),
        ("max_layer_height", 0.3),
    ]:
        if k in m:
            try:
                out[k] = to_float(m[k], default)
            except Exception:
                out[k] = default

    # extruders
    if "extruders" in m:
        try:
            out["extruders"] = to_int(m["extruders"], 1)
        except Exception:
            out["extruders"] = 1

    # nozzle_diameter
    if "nozzle_diameter" in m:
        try:
            nd = to_float_list(m["nozzle_diameter"], [0.4])
            out["nozzle_diameter"] = nd
        except Exception:
            out["nozzle_diameter"] = [0.4]

    # Entferne Felder, die Parser verwirren (printable_area/height etc.)
    # → nicht in out aufnehmen
    return out

def normalize_process(p: Dict[str, Any], printer_name: str) -> Dict[str, Any]:
    out = {
        "type": "process",
        "version": "1",
        "from": "user",
        "name": p.get("name", "0.20mm Standard"),
        "layer_height": 0.2,
        "first_layer_height": 0.3,
        "sparse_infill_density": "20%",
        "perimeters": 2,
        "top_solid_layers": 3,
        "bottom_solid_layers": 3,
        "compatible_printers": [printer_name],
        "compatible_printers_condition": ""
    }

    # Zahlenfelder robust
    for k, default in [
        ("layer_height", 0.2),
        ("first_layer_height", p.get("initial_layer_height", 0.3)),
        ("perimeters", 2),
        ("top_solid_layers", 3),
        ("bottom_solid_layers", 3),
    ]:
        try:
            if k in p or default is not None:
                if k in ("perimeters", "top_solid_layers", "bottom_solid_layers"):
                    out[k] = to_int(p.get(k, default))
                else:
                    out[k] = to_float(p.get(k, default))
        except Exception:
            out[k] = default

    # Infill als Prozent-String
    try:
        out["sparse_infill_density"] = to_percent_string(
            p.get("sparse_infill_density", "20%"), "20%"
        )
    except Exception:
        out["sparse_infill_density"] = "20%"

    # Gefahrfelder explizit NICHT übernehmen
    for bad in ("extruders", "nozzle_diameter", "printer_model", "printer_variant", "nozzle_diameter_initial_layer"):
        if bad in out:
            out.pop(bad, None)

    return out

def normalize_filament(f: Dict[str, Any], printer_name: str) -> Dict[str, Any]:
    out = {
        "type": "filament",
        "from": "user",
        "name": f.get("name", "Generic PLA"),
        "filament_flow_ratio": 0.92,
        "nozzle_temperature_initial_layer": 205.0,
        "nozzle_temperature": 200.0,
        "bed_temperature": 0.0,
        "bed_temperature_initial_layer": 0.0,
        "compatible_printers": [printer_name],
        "compatible_printers_condition": ""
    }
    for k, default in [
        ("filament_flow_ratio", 0.92),
        ("nozzle_temperature_initial_layer", 205.0),
        ("nozzle_temperature", 200.0),
        ("bed_temperature", 0.0),
        ("bed_temperature_initial_layer", 0.0),
    ]:
        try:
            out[k] = to_float(f.get(k, default))
        except Exception:
            out[k] = default

    # Keine Listen für diese Felder!
    return out

# -------------------------------------------------------------------
# Bündel/Bundle lesen
# -------------------------------------------------------------------
def read_bundle_structure() -> Optional[Dict[str, Any]]:
    if not BUNDLE_FILE.exists():
        return None
    try:
        return read_json(BUNDLE_FILE)
    except Exception:
        return None

def discover_profiles() -> Dict[str, List[str]]:
    profiles = {
        "printer": [],
        "process": [],
        "filament": [],
    }
    if PRINTER_DIR.exists():
        for p in PRINTER_DIR.glob("*.json"):
            profiles["printer"].append(str(p))
    if PROCESS_DIR.exists():
        for p in PROCESS_DIR.glob("*.json"):
            profiles["process"].append(str(p))
    if FILAMENT_DIR.exists():
        for p in FILAMENT_DIR.glob("*.json"):
            profiles["filament"].append(str(p))
    return profiles

# -------------------------------------------------------------------
# Root-UI (keine f-Strings!)
# -------------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <title>Orca CLI API</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; line-height: 1.4; }
    code, pre { background: #f6f8fa; padding: 4px 6px; border-radius: 6px; }
    .card { border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin: 12px 0; }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    button { padding: 8px 12px; border-radius: 8px; border: 1px solid #e5e7eb; cursor:pointer; }
    input[type=file], input[type=number], select { padding: 8px; border-radius: 8px; border: 1px solid #e5e7eb; }
    .ok { color: #047857; }
    .bad { color: #b91c1c; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;}
  </style>
</head>
<body>
  <h1>Orca CLI API</h1>

  <div class="card">
    <h3>Quick Checks</h3>
    <div class="row">
      <button onclick="check('/health')">/health</button>
      <button onclick="check('/slicer_env')">/slicer_env</button>
      <a href="/docs" target="_blank"><button>Swagger (API)</button></a>
    </div>
    <pre id="out" class="mono"></pre>
  </div>

  <div class="card">
    <h3>/slice_check testen</h3>
    <form id="sliceForm">
      <div class="row">
        <input type="file" name="file" required />
        <select name="unit">
          <option value="mm" selected>mm</option>
          <option value="cm">cm</option>
        </select>
        <select name="material">
          <option value="PLA" selected>PLA</option>
        </select>
        <label>Infill <input type="number" step="0.01" min="0" max="1" name="infill" value="0.2"/></label>
        <label>Layer <input type="number" step="0.01" name="layer_height" value="0.2"/></label>
        <label>Nozzle <input type="number" step="0.01" name="nozzle" value="0.4"/></label>
      </div>
      <div class="row" style="margin-top:8px;">
        <button type="submit">Senden</button>
      </div>
    </form>
    <pre id="sliceOut" class="mono"></pre>
  </div>

<script>
async function check(url) {
  const res = await fetch(url);
  const txt = await res.text();
  document.getElementById('out').textContent = txt;
}

document.getElementById('sliceForm').addEventListener('submit', async function(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const res = await fetch('/slice_check', { method:'POST', body: fd });
  const txt = await res.text();
  document.getElementById('sliceOut').textContent = txt;
});
</script>

</body>
</html>
"""

# -------------------------------------------------------------------
# Endpunkte
# -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

@app.get("/health")
def health():
    return {"ok": True, "version": app.version}

@app.get("/slicer_env")
def slicer_env():
    profiles = discover_profiles()
    bundle = read_bundle_structure()
    return {
        "ok": True,
        "slicer_bin": ORCA_BIN,
        "slicer_present": Path(ORCA_BIN).exists(),
        "profiles": profiles,
        "bundle_structure": bundle or {}
    }

# -------------------------------------------------------------------
# Kern: slice_check
# -------------------------------------------------------------------
@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    # Datei lesen
    stl_bytes = await file.read()
    # Profile laden
    try:
        machine_raw = read_json(DEFAULT_PRINTER_FILE)
        process_raw = read_json(DEFAULT_PROCESS_FILE)
        filament_raw = read_json(DEFAULT_FILAMENT_FILE)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Profil-Parsing fehlgeschlagen: {e}"})

    # Printer-Name ermitteln
    printer_name = machine_raw.get("name") or "RatRig V-Core 4 400 0.4 nozzle"

    # Normalisieren
    try:
        machine_norm = normalize_machine(machine_raw)
        process_norm = normalize_process(process_raw, printer_name)
        filament_norm = normalize_filament(filament_raw, printer_name)

        # Nutzerparameter als sanfte Overrides
        # Layerhöhe (Zahl)
        process_norm["layer_height"] = to_float(layer_height, process_norm.get("layer_height", 0.2))
        # Infill in %
        process_norm["sparse_infill_density"] = to_percent_string(infill, "20%")
        # Düse → nur ins Machine-Profil (Liste von Zahlen)
        machine_norm["nozzle_diameter"] = [to_float(nozzle, 0.4)]

    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Normalisierung fehlgeschlagen: {e}"})

    # Ausführen im Temp-Verzeichnis
    with tempfile.TemporaryDirectory(prefix="fixedp_") as tmp:
        tdir = Path(tmp)
        cfg_dir = tdir / "cfg"
        cfg_dir.mkdir(parents=True, exist_ok=True)

        input_stl = tdir / "input.stl"
        with input_stl.open("wb") as f:
            f.write(stl_bytes)

        # WICHTIG: die normalisierten Dateien schreiben und diese benutzen!
        printer_json = tdir / "printer.json"
        process_json = tdir / "process.json"
        filament_json = tdir / "filament.json"

        write_json(printer_json, machine_norm)
        write_json(process_json, process_norm)
        write_json(filament_json, filament_norm)

        out_3mf = tdir / "out.3mf"
        slicedata_dir = tdir / "slicedata"
        merged_settings = tdir / "merged_settings.json"

        cmd = [
            "xvfb-run", "-a", ORCA_BIN,
            "--debug", "4",
            "--datadir", str(cfg_dir),
            "--load-settings", str(printer_json),
            "--load-settings", str(process_json),
            "--load-filaments", str(filament_json),
            "--arrange", "1",
            "--orient", "1",
            str(input_stl),
            "--export-3mf", str(out_3mf),
            "--export-slicedata", str(slicedata_dir),
            "--export-settings", str(merged_settings),
        ]

        try:
            proc = subprocess.run(
                " ".join(cmd),
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(tdir),
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return JSONResponse(status_code=500, content={"detail": "Slicer-Timeout"})

        ok = proc.returncode == 0
        result = {
            "ok": ok,
            "code": proc.returncode,
            "cmd": " ".join(cmd),
            "stdout_tail": proc.stdout[-1200:],
            "stderr_tail": proc.stderr[-1200:],
            "out_3mf_exists": out_3mf.exists(),
            "slicedata_exists": slicedata_dir.exists() and slicedata_dir.is_dir(),
            "merged_settings_exists": merged_settings.exists(),
            "normalized_preview": {
                "machine": machine_norm,
                "process": {
                    "compatible_printers": process_norm.get("compatible_printers"),
                    "sparse_infill_density": process_norm.get("sparse_infill_density"),
                    "layer_height": process_norm.get("layer_height"),
                },
                "filament": {
                    "name": filament_norm.get("name"),
                    "compatible_printers": filament_norm.get("compatible_printers"),
                },
            },
            "profiles_used": {
                "printer_name": printer_name,
                "printer_path": str(DEFAULT_PRINTER_FILE),
                "process_path": str(DEFAULT_PROCESS_FILE),
                "filament_path": str(DEFAULT_FILAMENT_FILE),
            },
            "inputs": {
                "unit": unit,
                "material": material,
                "infill": infill,
                "layer_height": layer_height,
                "nozzle": nozzle,
                "stl_bytes": len(stl_bytes),
            },
        }

        if not ok:
            # häufigster Fehler: 2310 (process not compatible) – aber NUR wenn Parser sauber war
            return JSONResponse(status_code=500, content={"detail": result})

        # Erfolg
        return JSONResponse(content={"detail": result})

# Optional: ein Endpunkt, der .3mf direkt zurückgeben könnte (hier nur stub)
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

# -------------------------------------------------------------------
# Uvicorn Entrypoint (für Render)
# -------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    # Host/Port aus Env übernehmbar
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
