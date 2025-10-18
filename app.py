# app.py
import os
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

# -------------------------------------------------------------------
# Konstante Pfade (Repo-Struktur auf Render)
# -------------------------------------------------------------------
PROFILES_DIR = Path("/app/profiles")
PRINTER_DIR = PROFILES_DIR / "printer"
PROCESS_DIR = PROFILES_DIR / "process"
FILAMENT_DIR = PROFILES_DIR / "filament"
BUNDLE_STRUCTURE_PATH = PROFILES_DIR / "bundle_structure.json"

SLICER_BIN_CANDIDATES = [
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "/usr/bin/orca-slicer",
]

# -------------------------------------------------------------------
# Hilfsfunktionen: JSON laden/schreiben, Tails
# -------------------------------------------------------------------
def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def tail(text: str, max_lines: int = 50) -> str:
    if not text:
        return ""
    lines = text.splitlines()[-max_lines:]
    return "\n".join(lines)

def find_slicer_bin() -> Optional[str]:
    for p in SLICER_BIN_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

# -------------------------------------------------------------------
# Typ-Konverter (kritisch für Kompatibilität in Orca)
# -------------------------------------------------------------------
def _as_float(x) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", ".")
        return float(s)
    return float(x)

def _as_int(x) -> int:
    if isinstance(x, int):
        return x
    return int(round(_as_float(x)))

def _scalarize(v):
    return v[0] if isinstance(v, list) and v else v

def _ensure_float_pairs_bed_shape(machine: Dict[str, Any]) -> None:
    # Unterstützt sowohl ["0x0","400x0",...] als auch [[0,0],[400,0],...]
    bs = machine.get("bed_shape")
    if isinstance(bs, list) and bs:
        if all(isinstance(p, str) for p in bs):
            pts: List[List[float]] = []
            for s in bs:
                if "x" in s:
                    a, b = s.split("x", 1)
                    pts.append([_as_float(a), _as_float(b)])
            if pts:
                machine["bed_shape"] = pts
        elif all(isinstance(p, (list, tuple)) and len(p) == 2 for p in bs):
            machine["bed_shape"] = [[_as_float(p[0]), _as_float(p[1])] for p in bs]
    if "bed_shape" not in machine:
        # Fallback: 400x400 Quadrat
        machine["bed_shape"] = [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]]

# -------------------------------------------------------------------
# Normalizer: Machine / Process / Filament
# -------------------------------------------------------------------
def normalize_machine(machine: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    machine = dict(machine)  # copy
    machine.setdefault("type", "machine")
    machine.setdefault("printer_technology", "FFF")
    machine.setdefault("gcode_flavor", "marlin")

    _ensure_float_pairs_bed_shape(machine)

    # Numerische Pflichtfelder
    for key in ("max_print_height", "min_layer_height", "max_layer_height"):
        if key in machine:
            val = _scalarize(machine[key])
            machine[key] = _as_float(val)
    machine.setdefault("max_print_height", 300.0)

    if "extruders" in machine:
        machine["extruders"] = _as_int(_scalarize(machine["extruders"]))
    else:
        machine["extruders"] = 1

    nd = machine.get("nozzle_diameter")
    if isinstance(nd, list) and nd:
        machine["nozzle_diameter"] = [_as_float(_scalarize(v)) for v in nd]
    else:
        machine["nozzle_diameter"] = [0.4]

    # Name
    printer_name = machine.get("name") or "Generic 400x400 0.4 nozzle"
    machine["name"] = printer_name

    return machine, printer_name

def normalize_process(process: Dict[str, Any], printer_name: str) -> Dict[str, Any]:
    process = dict(process)
    process.setdefault("type", "process")

    # Kompatibilität: exakten Druckernamen sicherstellen
    cp = process.get("compatible_printers")
    if not isinstance(cp, list):
        cp = []
    if printer_name not in cp:
        cp.append(printer_name)
    process["compatible_printers"] = cp

    # Entferne evtl. druckerspezifische Felder, die Konflikte auslösen können
    for k in ("extruders", "nozzle_diameter"):
        if k in process:
            process.pop(k, None)

    # Layer Height ggf. aus Eingabe überschreiben → passiert später in /slice_check
    return process

def normalize_filament(filament: Dict[str, Any], printer_name: str, material_name: Optional[str]) -> Dict[str, Any]:
    filament = dict(filament)
    filament.setdefault("type", "filament")

    # Anzeigename anreichern, rein kosmetisch
    if material_name:
        base = filament.get("name") or material_name
        if material_name not in base:
            filament["name"] = f"{base} ({material_name})"

    cp = filament.get("compatible_printers")
    if not isinstance(cp, list):
        cp = []
    if printer_name not in cp:
        cp.append(printer_name)
    filament["compatible_printers"] = cp

    for k in ("extruders", "nozzle_diameter"):
        if k in filament:
            filament.pop(k, None)

    return filament

# -------------------------------------------------------------------
# Laden der Repo-Profile + Bundle-Info
# -------------------------------------------------------------------
def list_profiles() -> Dict[str, List[str]]:
    out = {"printer": [], "process": [], "filament": []}
    if PRINTER_DIR.exists():
        out["printer"] = [str(p) for p in sorted(PRINTER_DIR.glob("*.json"))]
    if PROCESS_DIR.exists():
        out["process"] = [str(p) for p in sorted(PROCESS_DIR.glob("*.json"))]
    if FILAMENT_DIR.exists():
        out["filament"] = [str(p) for p in sorted(FILAMENT_DIR.glob("*.json"))]
    return out

def try_load_bundle_structure() -> Optional[Dict[str, Any]]:
    if BUNDLE_STRUCTURE_PATH.exists():
        try:
            return load_json(BUNDLE_STRUCTURE_PATH)
        except Exception:
            return None
    return None

def load_default_repo_profiles() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]:
    # Voreinstellungen aus Repo
    printer_path = PRINTER_DIR / "X1C.json"
    process_path = PROCESS_DIR / "0.20mm_standard.json"
    filament_path = FILAMENT_DIR / "PLA.json"

    machine_raw = load_json(printer_path)
    process_raw = load_json(process_path)
    filament_raw = load_json(filament_path)

    machine_norm, printer_name = normalize_machine(machine_raw)
    process_norm = normalize_process(process_raw, printer_name)
    filament_norm = normalize_filament(filament_raw, printer_name, "PLA")

    return machine_norm, process_norm, filament_norm, printer_name

# -------------------------------------------------------------------
# Slicing-Aufruf
# -------------------------------------------------------------------
def run_orca_slice(
    machine: Dict[str, Any],
    process: Dict[str, Any],
    filament: Dict[str, Any],
    stl_bytes: bytes,
    unit: str = "mm",
    layer_height: Optional[float] = None,
    infill: Optional[float] = None,
    nozzle: Optional[float] = None,
) -> Dict[str, Any]:
    slicer_bin = find_slicer_bin()
    if not slicer_bin:
        return {"ok": False, "error": "orca-slicer binary not found"}

    with tempfile.TemporaryDirectory(prefix="fixedp_") as tmpdir:
        tdir = Path(tmpdir)
        (tdir / "cfg").mkdir(parents=True, exist_ok=True)

        # STL speichern
        stl_path = tdir / "input.stl"
        with stl_path.open("wb") as f:
            f.write(stl_bytes)

        # Eingaben (optional) in Process eintragen
        p = dict(process)
        if layer_height is not None:
            # Orca akzeptiert Strings, aber wir belassen Zahlen.
            p["layer_height"] = layer_height
        if infill is not None:
            # Als Prozent-String
            pct = int(round(infill * 100))
            p["sparse_infill_density"] = f"{pct}%"
        # nozzle lassen wir bei Machine (nozzle_diameter[0])

        # Profile in tmp schreiben
        printer_json = tdir / "printer.json"
        process_json = tdir / "process.json"
        filament_json = tdir / "filament.json"

        save_json(printer_json, machine)
        save_json(process_json, p)
        save_json(filament_json, filament)

        out_3mf = tdir / "out.3mf"
        slicedata_dir = tdir / "slicedata"
        merged_settings = tdir / "merged_settings.json"

        cmd = [
            "xvfb-run", "-a", slicer_bin,
            "--debug", "4",
            "--datadir", str(tdir / "cfg"),
            "--load-settings", f"{printer_json};{process_json}",
            "--load-filaments", str(filament_json),
            "--arrange", "1",
            "--orient", "1",
            str(stl_path),
            "--slice", "1",
            "--export-3mf", str(out_3mf),
            "--export-slicedata", str(slicedata_dir),
            "--export-settings", str(merged_settings),
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
            timeout=300,
        )

        result: Dict[str, Any] = {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "cmd": " ".join(cmd),
            "stdout_tail": tail(proc.stdout, 120),
            "stderr_tail": tail(proc.stderr, 120),
            "out_3mf_exists": out_3mf.exists(),
            "slicedata_exists": slicedata_dir.exists(),
            "merged_settings_exists": merged_settings.exists(),
            "normalized_preview": {
                "machine": machine,
                "process": {
                    "compatible_printers": p.get("compatible_printers"),
                    "sparse_infill_density": p.get("sparse_infill_density"),
                    "layer_height": p.get("layer_height"),
                },
                "filament": {
                    "name": filament.get("name"),
                    "compatible_printers": filament.get("compatible_printers"),
                },
            },
        }

        return result

# -------------------------------------------------------------------
# FastAPI App
# -------------------------------------------------------------------
app = FastAPI(title="Orca Slice API", version="1.0.0")

@app.get("/", response_class=HTMLResponse)
def index():
    # KEIN f-string → vermeidet SyntaxError (JS/{}).
    html = """
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Orca Slice API – Test</title>
<style>
  body {font-family: system-ui, Arial, sans-serif; margin: 2rem; line-height: 1.4;}
  code, pre {background:#f6f6f6; padding: 0.25rem 0.5rem; border-radius: 6px;}
  .box {border:1px solid #ddd; border-radius:8px; padding:1rem; margin:1rem 0;}
  button {padding:0.5rem 0.9rem; border-radius:6px; border:1px solid #ccc; background:#fafafa; cursor:pointer;}
  button:hover {background:#f0f0f0;}
  input[type=file] {margin: 0.25rem 0;}
  .row {display:flex; gap:1rem; flex-wrap:wrap;}
  .col {flex:1 1 320px;}
  .mono {font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;}
</style>
</head>
<body>
  <h1>Orca Slice API</h1>
  <div class="row">
    <div class="col box">
      <h3>Health</h3>
      <button onclick="getJSON('/health')">/health</button>
      <pre id="out-health" class="mono"></pre>
    </div>
    <div class="col box">
      <h3>Slicer-Umgebung</h3>
      <button onclick="getJSON('/slicer_env')">/slicer_env</button>
      <pre id="out-env" class="mono"></pre>
    </div>
  </div>

  <div class="box">
    <h3>/slice_check</h3>
    <form id="sliceForm">
      <div>STL: <input type="file" name="file" required /></div>
      <div>Unit: <select name="unit"><option>mm</option><option>inch</option></select></div>
      <div>Material: <input name="material" value="PLA"/></div>
      <div>Infill (0..1): <input name="infill" value="0.2" /></div>
      <div>Layer Height (mm): <input name="layer_height" value="0.2" /></div>
      <div>Nozzle (mm): <input name="nozzle" value="0.4" /></div>
      <button type="submit">Check</button>
    </form>
    <pre id="out-slice" class="mono"></pre>
  </div>

  <div class="box">
    <a href="/docs" target="_blank"><button>Swagger (API-Doku)</button></a>
  </div>

<script>
async function getJSON(path) {
  const out = document.getElementById(path === '/health' ? 'out-health' : 'out-env');
  out.textContent = 'Lade...';
  try {
    const r = await fetch(path);
    const j = await r.json();
    out.textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    out.textContent = 'Fehler: ' + e;
  }
}
document.getElementById('sliceForm').addEventListener('submit', async function(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const out = document.getElementById('out-slice');
  out.textContent = 'Lade...';
  try {
    const r = await fetch('/slice_check', { method: 'POST', body: fd });
    const j = await r.json();
    out.textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    out.textContent = 'Fehler: ' + e;
  }
});
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)

@app.get("/health")
def health():
    return {"ok": True, "version": "1.0.0"}

@app.get("/slicer_env")
def slicer_env():
    binpath = find_slicer_bin()
    profiles = list_profiles()
    bundle = try_load_bundle_structure()
    return {
        "ok": True,
        "slicer_bin": binpath,
        "slicer_present": bool(binpath),
        "profiles": profiles,
        "bundle_structure": bundle,
    }

# -------------------------------------------------------------------
# /slice_check: lädt Repo-Profile, normalisiert, zwingt Kompatibilität,
# ruft CLI mit xvfb-run auf, gibt Diagnose zurück.
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
    try:
        stl_bytes = await file.read()

        # Repo-Profile laden & normieren
        machine, process, filament, printer_name = load_default_repo_profiles()

        # Materialname in Filament übernehmen (nur kosmetisch)
        filament = normalize_filament(filament, printer_name, material)

        # Nozzle optional (wir verändern machine.nozzle_diameter[0] NICHT hier, nur falls gewünscht):
        # Wenn du zwingend Nozzle übernehmen willst, entkommentieren:
        # machine["nozzle_diameter"] = [float(nozzle)]

        result = run_orca_slice(
            machine=machine,
            process=process,
            filament=filament,
            stl_bytes=stl_bytes,
            unit=unit,
            layer_height=layer_height,
            infill=infill,
            nozzle=nozzle,
        )

        # Für bessere Diagnose: verwendete Profil-Pfade und Namen zurückgeben
        used = {
            "printer_name": printer_name,
            "printer_path": str(PRINTER_DIR / "X1C.json"),
            "process_path": str(PROCESS_DIR / "0.20mm_standard.json"),
            "filament_path": str(FILAMENT_DIR / "PLA.json"),
        }

        return JSONResponse(
            {
                "detail": {
                    **result,
                    "profiles_used": used,
                    "inputs": {
                        "unit": unit,
                        "material": material,
                        "infill": infill,
                        "layer_height": layer_height,
                        "nozzle": nozzle,
                        "stl_bytes": len(stl_bytes),
                    },
                }
            },
            status_code=200 if result.get("ok") else 400,
        )

    except subprocess.TimeoutExpired as e:
        return JSONResponse(
            {"detail": {"ok": False, "message": "Slicing timeout", "error": str(e)}},
            status_code=504,
        )
    except Exception as e:
        return JSONResponse(
            {"detail": {"ok": False, "message": "Unexpected error", "error": str(e)}},
            status_code=500,
        )
