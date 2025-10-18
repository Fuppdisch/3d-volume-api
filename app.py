# app.py
import os
import io
import json
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

APP_VERSION = "1.0.0"

# Standard-Pfade (werden über bundle_structure.json ggf. überschrieben)
PROFILES_DIR = Path("/app/profiles")
PRINTER_PATH  = PROFILES_DIR / "printer"  / "X1C.json"
PROCESS_PATH  = PROFILES_DIR / "process"  / "0.20mm_standard.json"
FILAMENT_PATH = PROFILES_DIR / "filament" / "PLA.json"
BUNDLE_PATH   = PROFILES_DIR / "bundle_structure.json"

ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/bin/orca-slicer")

app = FastAPI(title="OrcaSlicer Service", version=APP_VERSION)


# --------------------------- Utils: JSON & Normalisierung ---------------------------

def load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _strip_list_string(s: str) -> str:
    t = s.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()
        if t.startswith(("'", '"')) and t.endswith(("'", '"')):
            t = t[1:-1]
    return t

def _to_float(x: Any) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, list) and x:
        return _to_float(x[0])
    if isinstance(x, str):
        s = _strip_list_string(x)
        return float(s)
    raise ValueError(f"cannot convert to float: {x!r}")

def _to_int(x: Any) -> int:
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x)
    if isinstance(x, list) and x:
        return _to_int(x[0])
    if isinstance(x, str):
        s = _strip_list_string(x)
        return int(float(s))
    raise ValueError(f"cannot convert to int: {x!r}")

def _to_float_list(xs: Any) -> List[float]:
    if isinstance(xs, (int, float, str)):
        return [_to_float(xs)]
    if isinstance(xs, list):
        return [_to_float(v) for v in xs]
    raise ValueError(f"cannot convert to float list: {xs!r}")

def _normalize_bed_shape(machine: Dict[str, Any]) -> None:
    # akzeptiert u.a. "printable_area": ["0x0","400x0",...]
    # Ziel: "bed_shape": [[0,0],[400,0],[400,400],[0,400]]
    if "bed_shape" in machine and isinstance(machine["bed_shape"], list) and machine["bed_shape"] and isinstance(machine["bed_shape"][0], list):
        # bereits OK
        return
    # fallback: printable_area in "wxh" Stringform
    pa = machine.get("printable_area")
    if isinstance(pa, list) and pa and isinstance(pa[0], str):
        points = []
        for item in pa:
            item = item.strip()
            if "x" in item:
                a, b = item.split("x", 1)
                points.append([float(a), float(b)])
        if points:
            machine["bed_shape"] = points
            return
    # sonst: nichts tun, wenn nicht vorhanden; Caller setzt ggf. Defaults

def normalize_machine(m: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(m)
    _normalize_bed_shape(m)
    # harte Typ-Korrekturen
    for k in ("max_print_height", "min_layer_height", "max_layer_height"):
        if k in m:
            m[k] = _to_float(m[k])
    if "extruders" in m:
        m["extruders"] = _to_int(m["extruders"])
    if "nozzle_diameter" in m:
        m["nozzle_diameter"] = _to_float_list(m["nozzle_diameter"])

    # Fallbacks
    m.setdefault("printer_technology", "FFF")
    m.setdefault("gcode_flavor", "marlin")
    if "bed_shape" not in m:
        m["bed_shape"] = [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]]
    m.setdefault("max_print_height", 300.0)
    m.setdefault("min_layer_height", 0.06)
    m.setdefault("max_layer_height", 0.30)
    m.setdefault("extruders", 1)
    m.setdefault("nozzle_diameter", [0.4])

    return m

def _as_percent(v: Any) -> str:
    # akzeptiert 0.2, "0.2", 20, "20%", etc.
    if isinstance(v, str) and v.strip().endswith("%"):
        return v.strip()
    try:
        f = _to_float(v)
        if f <= 1.0:
            return f"{round(f * 100)}%"
        return f"{round(f)}%"
    except Exception:
        return "20%"

def normalize_process(p: Dict[str, Any], printer_name: Optional[str]) -> Dict[str, Any]:
    p = dict(p)
    # Layer-Höhen: Orca kennt teils "initial_layer_height", teils "first_layer_height".
    if "layer_height" in p:
        p["layer_height"] = _to_float(p["layer_height"])
    if "initial_layer_height" in p:
        p["initial_layer_height"] = _to_float(p["initial_layer_height"])
    elif "first_layer_height" in p:
        p["first_layer_height"] = _to_float(p["first_layer_height"])

    # Infill
    if "sparse_infill_density" in p:
        p["sparse_infill_density"] = _as_percent(p["sparse_infill_density"])

    # riskante Felder entfernen (führt zu „invalid json type“/Kompat-Checks)
    for k in ("extruders", "nozzle_diameter", "printer_technology", "printer_model", "printer_variant"):
        p.pop(k, None)

    # Kompatibilität sicherstellen
    if printer_name:
        cp = set(p.get("compatible_printers") or [])
        cp.add(printer_name)
        p["compatible_printers"] = sorted(cp)
        p["compatible_printers_condition"] = p.get("compatible_printers_condition", "")

    return p

def normalize_filament(f: Dict[str, Any], printer_name: Optional[str]) -> Dict[str, Any]:
    f = dict(f)

    # Übliche Zahlenfelder tolerant glätten (egal ob Liste/Str/Float)
    num_fields = [
        "nozzle_temperature", "nozzle_temperature_initial_layer",
        "bed_temperature", "bed_temperature_initial_layer",
        "filament_flow_ratio", "filament_density", "filament_diameter"
    ]
    for k in num_fields:
        if k in f:
            if isinstance(f[k], list):
                f[k] = [_to_float(v) for v in f[k]]
            else:
                f[k] = [_to_float(f[k])]

    # riskante Felder entfernen
    for k in ("extruders", "nozzle_diameter", "printer_technology", "printer_model", "printer_variant"):
        f.pop(k, None)

    # Kompatibilität
    if printer_name:
        cp = set(f.get("compatible_printers") or [])
        cp.add(printer_name)
        f["compatible_printers"] = sorted(cp)
        f["compatible_printers_condition"] = f.get("compatible_printers_condition", "")

    return f


# --------------------------- Bundle / Profile Discovery ---------------------------

def resolve_profiles() -> Tuple[Path, Path, Path, Optional[str]]:
    """
    Liest bundle_structure.json (falls vorhanden) und liefert die realen Pfade + Printer-Name.
    Fällt ansonsten auf fest verdrahtete Standardpfade zurück.
    """
    printer, process, filament = PRINTER_PATH, PROCESS_PATH, FILAMENT_PATH
    printer_name = None
    if BUNDLE_PATH.exists():
        try:
            b = load_json_file(BUNDLE_PATH)
            # Einträge sind relative Pfade innerhalb /app/profiles/
            if isinstance(b.get("printer_config"), list) and b["printer_config"]:
                printer = PROFILES_DIR / b["printer_config"][0]
            if isinstance(b.get("process_config"), list) and b["process_config"]:
                process = PROFILES_DIR / b["process_config"][0]
            if isinstance(b.get("filament_config"), list) and b["filament_config"]:
                filament = PROFILES_DIR / b["filament_config"][0]
            # Optional: vorgegebener Anzeigename
            if b.get("printer_preset_name"):
                printer_name = str(b["printer_preset_name"])
        except Exception:
            # ignoriere Bundle-Fehler und nutze Defaults
            pass
    return printer, process, filament, printer_name


# --------------------------- FastAPI Endpoints ---------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>OrcaSlicer API</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:2rem;line-height:1.4}
code,pre{background:#f6f8fa;padding:.2rem .4rem;border-radius:4px}
section{margin-bottom:2rem}
button{padding:.5rem 1rem;border-radius:6px;border:1px solid #ccc;background:#fff;cursor:pointer}
input,select{padding:.4rem .6rem;border:1px solid #ccc;border-radius:6px}
</style></head>
<body>
<h1>OrcaSlicer API</h1>
<section>
  <button onclick="fetch('/health').then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">Health</button>
  <button onclick="fetch('/slicer_env').then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">Slicer Env</button>
  <a href="/docs" style="margin-left:1rem">Swagger (API)</a>
</section>
<section>
  <h3>/slice_check testen</h3>
  <form id="f" enctype="multipart/form-data">
    <div>STL: <input type="file" name="file" required></div>
    <div>Material: <select name="material"><option>PLA</option><option>PETG</option><option>ASA</option></select></div>
    <div>Layer height: <input type="number" step="0.01" name="layer_height" value="0.2"></div>
    <div>Nozzle: <input type="number" step="0.01" name="nozzle" value="0.4"></div>
    <div>Infill: <input type="number" step="0.01" name="infill" value="0.2"></div>
    <div>Unit: <select name="unit"><option>mm</option><option>inch</option></select></div>
    <div style="margin-top:.5rem"><button type="submit">Senden</button></div>
  </form>
  <pre id="out"></pre>
</section>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice_check', { method:'POST', body:fd });
  const j = await r.json();
  document.getElementById('out').textContent = JSON.stringify(j,null,2);
});
</script>
</body></html>"""

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}

@app.get("/version")
def version():
    return {"ok": True, "version": APP_VERSION}

@app.get("/slicer_env")
def slicer_env():
    printer, process, filament, printer_name = resolve_profiles()
    env = {
        "ok": True,
        "slicer_bin": ORCA_BIN,
        "slicer_present": Path(ORCA_BIN).exists(),
        "profiles": {
            "printer": [str(printer)],
            "process": [str(process)],
            "filament": [str(filament)],
        }
    }
    if BUNDLE_PATH.exists():
        try:
            env["bundle_structure"] = load_json_file(BUNDLE_PATH)
        except Exception as e:
            env["bundle_structure_error"] = str(e)
    return env


# --------------------------- Slicing (dry-run / real) ---------------------------

def _tail(s: str, n: int = 40) -> str:
    lines = s.splitlines()
    return "\n".join(lines[-n:])

def _write_json(p: Path, data: Dict[str, Any]) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _call_orca(cmd: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate()
    return proc.returncode, out, err


@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    # Profile-Pfade ermitteln
    printer_path, process_path, filament_path, bundle_printer_name = resolve_profiles()

    if not printer_path.exists():
        return JSONResponse(status_code=400, content={"detail": f"Printer-Profil fehlt: {printer_path}"})
    if not process_path.exists():
        return JSONResponse(status_code=400, content={"detail": f"Process-Profil fehlt: {process_path}"})
    if not filament_path.exists():
        return JSONResponse(status_code=400, content={"detail": f"Filament-Profil fehlt: {filament_path}"})

    # Originale Profile laden
    try:
        printer_raw  = load_json_file(printer_path)
        process_raw  = load_json_file(process_path)
        filament_raw = load_json_file(filament_path)
    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Profil-Parsing fehlgeschlagen: {e}"})

    # Printer-Name bestimmen
    printer_name = bundle_printer_name or printer_raw.get("name") or "Generic 400x400 0.4 nozzle"

    # Normalisieren
    try:
        printer_norm  = normalize_machine(printer_raw)
        # überschreibe (falls Bundle einen Namen vorgibt)
        printer_norm["name"] = printer_name

        # process: Benutzer-Overrides hinein
        process_raw = dict(process_raw)
        process_raw["layer_height"] = layer_height
        # „sparse_infill_density“ als Prozent
        process_raw["sparse_infill_density"] = _as_percent(infill)
        process_norm  = normalize_process(process_raw, printer_name=printer_name)

        # filament: (material wird aktuell nur zur Info genutzt)
        filament_norm = normalize_filament(filament_raw, printer_name=printer_name)

    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Normalisierung fehlgeschlagen: {e}"})

    # Temporäre Arbeitsumgebung
    tmpdir = Path(tempfile.mkdtemp(prefix="fixedp_"))
    try:
        in_stl = tmpdir / "input.stl"
        in_stl.write_bytes(await file.read())

        # Bereinigte JSONs schreiben
        p_printer  = tmpdir / "printer.json"
        p_process  = tmpdir / "process.json"
        p_filament = tmpdir / "filament.json"
        _write_json(p_printer, printer_norm)
        _write_json(p_process, process_norm)
        _write_json(p_filament, filament_norm)

        # Ausgabepfade
        out_3mf   = tmpdir / "out.3mf"
        slicedata = tmpdir / "slicedata"
        merged    = tmpdir / "merged_settings.json"

        # Orca-Aufruf (ohne --slice i → „alle Platten“, vermeidet 1630-Warnung)
        cmd = [
            "xvfb-run", "-a", ORCA_BIN,
            "--debug", "4",
            "--datadir", str(tmpdir / "cfg"),
            "--load-settings", str(p_printer),
            "--load-settings", str(p_process),
            "--load-filaments", str(p_filament),
            "--arrange", "1",
            "--orient", "1",
            str(in_stl),
            "--export-3mf", str(out_3mf),
            "--export-slicedata", str(slicedata),
            "--export-settings", str(merged),
        ]

        code, out, err = _call_orca(cmd)

        result = {
            "ok": code == 0,
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": _tail(out, 80),
            "stderr_tail": _tail(err, 80),
            "out_3mf_exists": out_3mf.exists(),
            "slicedata_exists": slicedata.exists(),
            "merged_settings_exists": merged.exists(),
            "profiles_used": {
                "printer_name": printer_name,
                "printer_path": str(printer_path),
                "process_path": str(process_path),
                "filament_path": str(filament_path),
            },
            "inputs": {
                "unit": unit,
                "material": material,
                "infill": infill,
                "layer_height": layer_height,
                "nozzle": nozzle,
                "stl_bytes": in_stl.stat().st_size if in_stl.exists() else None,
            },
            "normalized_preview": {
                "machine": {
                    "name": printer_norm.get("name"),
                    "printer_technology": printer_norm.get("printer_technology"),
                    "gcode_flavor": printer_norm.get("gcode_flavor"),
                    "bed_shape": printer_norm.get("bed_shape"),
                    "max_print_height": printer_norm.get("max_print_height"),
                    "min_layer_height": printer_norm.get("min_layer_height"),
                    "max_layer_height": printer_norm.get("max_layer_height"),
                    "extruders": printer_norm.get("extruders"),
                    "nozzle_diameter": printer_norm.get("nozzle_diameter"),
                },
                "process": {
                    "compatible_printers": process_norm.get("compatible_printers"),
                    "sparse_infill_density": process_norm.get("sparse_infill_density"),
                    "layer_height": process_norm.get("layer_height"),
                },
                "filament": {
                    "name": filament_norm.get("name", "filament"),
                    "compatible_printers": filament_norm.get("compatible_printers"),
                },
            }
        }

        status = 200 if code == 0 else 500
        return JSONResponse(status_code=status, content={"detail": result})

    finally:
        # Tmp behalten? → zum Debuggen hier NICHT löschen
        # shutil.rmtree(tmpdir, ignore_errors=True)
        pass


# --------------------------- Uvicorn Entrypoint ---------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), reload=False)
