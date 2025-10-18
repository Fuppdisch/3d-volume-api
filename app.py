# app.py
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

# ------------------------------------------------------------
# Pfad-Helfer
# ------------------------------------------------------------
REPO_DIR = Path("/app/profiles")

def _first_existing(*candidates: Path) -> Path:
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]

PRINTER_FILE = _first_existing(REPO_DIR / "printer" / "X1C.json",
                               REPO_DIR / "printers" / "X1C.json")
PROCESS_FILE = _first_existing(REPO_DIR / "process" / "0.20mm_standard.json",
                               REPO_DIR / "processes" / "0.20mm_standard.json")
FILAMENT_DIR = _first_existing(REPO_DIR / "filament",
                               REPO_DIR / "filaments")
BUNDLE_FILE = REPO_DIR / "bundle_structure.json"

def _choose_slicer_bin() -> Optional[str]:
    for p in ("/opt/orca/bin/orca-slicer", "/usr/local/bin/orca-slicer"):
        if Path(p).exists():
            return p
    return None

def _run(cmd: List[str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 997, "", f"{type(e).__name__}: {e}"

def _read_json(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(path: Path, data: Dict):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _tail(s: str, n: int = 24) -> str:
    if not s:
        return ""
    lines = s.splitlines()
    return "\n".join(lines[-n:])

# ------------------------------------------------------------
# Normalisierung & Binding
# ------------------------------------------------------------
def _normalize_machine(machine: Dict) -> Dict:
    m = dict(machine)

    # bed_shape: Liste von [x, y] floats (Strings wie "400x0" konvertieren)
    def _to_pair(v):
        if isinstance(v, str) and "x" in v:
            x, y = v.split("x")
            return [float(x), float(y)]
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return [float(v[0]), float(v[1])]
        return None

    if "printable_area" in m and not m.get("bed_shape"):
        fixed = []
        for p in m["printable_area"]:
            pair = _to_pair(p)
            if pair:
                fixed.append(pair)
        if fixed:
            m["bed_shape"] = fixed

    if "bed_shape" in m:
        fixed = []
        for p in m["bed_shape"]:
            pair = _to_pair(p)
            if pair:
                fixed.append(pair)
        if fixed:
            m["bed_shape"] = fixed

    # Zahlenfelder erzwingen
    for k in ("max_print_height", "min_layer_height", "max_layer_height"):
        if k in m:
            try:
                m[k] = float(m[k])
            except Exception:
                pass

    # extruders -> int
    if "extruders" in m:
        try:
            m["extruders"] = int(m["extruders"])
        except Exception:
            pass
    else:
        m["extruders"] = 1

    # nozzle_diameter -> Liste von floats
    if "nozzle_diameter" in m and isinstance(m["nozzle_diameter"], list):
        nd = []
        for v in m["nozzle_diameter"]:
            try:
                nd.append(float(v))
            except Exception:
                continue
        if nd:
            m["nozzle_diameter"] = nd
    elif "nozzle_diameter" in m:
        try:
            m["nozzle_diameter"] = [float(m["nozzle_diameter"])]
        except Exception:
            pass
    else:
        m["nozzle_diameter"] = [0.4]

    # defaults
    m.setdefault("gcode_flavor", "marlin")
    m.setdefault("printer_technology", "FFF")
    if not m.get("bed_shape"):
        # Fallback: 200x200
        m["bed_shape"] = [[0.0, 0.0], [200.0, 0.0], [200.0, 200.0], [0.0, 200.0]]
    if "name" not in m:
        # Notfalls aus model/variant zusammenstecken
        nm = (m.get("printer_model") or "Generic 400x400")
        var = (m.get("printer_variant") or "0.4")
        m["name"] = f"{nm} {var} nozzle"

    return m

def _bind_compat(process: Dict, filament: Dict, machine: Dict) -> Tuple[Dict, Dict]:
    """Bindet exakten Drucker-Namen und spiegelt relevante Felder."""
    mname = machine.get("name", "")
    bound_keys = {
        "printer_technology": machine.get("printer_technology", "FFF"),
        "printer_model": machine.get("printer_model", "Generic 400x400"),
        "printer_variant": machine.get("printer_variant", "0.4"),
        "gcode_flavor": machine.get("gcode_flavor", "marlin"),
        "extruders": machine.get("extruders", 1),
        "nozzle_diameter": machine.get("nozzle_diameter", [0.4]),
    }

    def patch_preset(d: Dict) -> Dict:
        out = dict(d)
        # kompatible_printers
        lst = out.get("compatible_printers")
        if not isinstance(lst, list):
            lst = []
        if mname not in lst:
            lst = [mname] + [x for x in lst if x != mname]
        out["compatible_printers"] = lst
        out.setdefault("compatible_printers_condition", "")

        # Meta spiegeln
        for k, v in bound_keys.items():
            out[k] = v
        # nozzle als floats
        if isinstance(out.get("nozzle_diameter"), list):
            out["nozzle_diameter"] = [float(x) for x in out["nozzle_diameter"]]
        else:
            try:
                out["nozzle_diameter"] = [float(out["nozzle_diameter"])]
            except Exception:
                out["nozzle_diameter"] = [0.4]
        try:
            out["extruders"] = int(out.get("extruders", 1))
        except Exception:
            out["extruders"] = 1
        return out

    return patch_preset(process), patch_preset(filament)

# ------------------------------------------------------------
# FastAPI Grundgerüst
# ------------------------------------------------------------
app = FastAPI(title="Orca Cloud Helper", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "status": "healthy"}

@app.get("/slicer_env")
def slicer_env():
    binpath = _choose_slicer_bin()
    data = {
        "slicer_present": bool(binpath),
        "slicer_bin": binpath,
        "profiles": {
            "printer": [str(PRINTER_FILE)] if PRINTER_FILE.exists() else [],
            "process": [str(PROCESS_FILE)] if PROCESS_FILE.exists() else [],
            "filament": [str(p) for p in FILAMENT_DIR.glob("*.json")] if FILAMENT_DIR.exists() else [],
        },
        "bundle_structure_present": BUNDLE_FILE.exists(),
    }
    if binpath:
        code, out, err = _run([binpath, "--help"])
        data["return_code"] = code
        data["help_snippet"] = _tail(out or err, 40)
    if BUNDLE_FILE.exists():
        try:
            data["bundle_structure"] = _read_json(BUNDLE_FILE)
        except Exception as e:
            data["bundle_structure_error"] = f"{type(e).__name__}: {e}"
    return JSONResponse(content=data)

# ------------------------------------------------------------
# Slicing
# ------------------------------------------------------------
@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    binpath = _choose_slicer_bin()
    if not binpath:
        return JSONResponse(status_code=500, content={"detail": "orca-slicer nicht gefunden"})

    if not PRINTER_FILE.exists():
        return JSONResponse(status_code=400, content={"detail": f"Printer-Profil fehlt: {PRINTER_FILE}"})
    if not PROCESS_FILE.exists():
        return JSONResponse(status_code=400, content={"detail": f"Process-Profil fehlt: {PROCESS_FILE}"})
    if not FILAMENT_DIR.exists():
        return JSONResponse(status_code=400, content={"detail": f"Filament-Ordner fehlt: {FILAMENT_DIR}"})

    # Filament wählen (PLA bevorzugt)
    target = material.upper().strip()
    filament_file = None
    for f in FILAMENT_DIR.glob("*.json"):
        if f.stem.upper() == target:
            filament_file = f
            break
    if filament_file is None:
        # Fallback erstes Profil
        filament_file = next(iter(FILAMENT_DIR.glob("*.json")), None)
    if filament_file is None:
        return JSONResponse(status_code=400, content={"detail": "Kein Filament-Profil gefunden"})

    raw = await file.read()

    with tempfile.TemporaryDirectory(prefix="fixedp_") as tmpd:
        tmp = Path(tmpd)
        stl_name = "input.stl" if unit.lower() == "mm" else "input_model.stl"
        in_stl = tmp / stl_name
        in_stl.write_bytes(raw)

        # Profile laden
        try:
            machine = _normalize_machine(_read_json(PRINTER_FILE))
            process = _read_json(PROCESS_FILE)
            filament = _read_json(filament_file)
        except Exception as e:
            return JSONResponse(status_code=400, content={"detail": f"Profil-Leseproblem: {type(e).__name__}: {e}"})

        printer_name = machine.get("name") or "Generic 400x400 0.4 nozzle"
        process, filament = _bind_compat(process, filament, machine)

        # Benutzerwerte in den Process übernehmen
        process["layer_height"] = str(layer_height)
        process["first_layer_height"] = process.get("first_layer_height") or str(max(layer_height, 0.2))
        process["initial_layer_height"] = process.get("initial_layer_height") or process["first_layer_height"]
        process["sparse_infill_density"] = f"{int(round(infill * 100))}%"
        process["perimeter_extrusion_width"] = process.get("perimeter_extrusion_width") or str(nozzle + 0.05)
        process["line_width"] = process.get("line_width") or str(nozzle + 0.05)

        # temporär schreiben
        mfile = tmp / "printer.json"
        pfile = tmp / "process.json"
        ffile = tmp / "filament.json"
        _write_json(mfile, machine)
        _write_json(pfile, process)
        _write_json(ffile, filament)

        out3mf = tmp / "out.3mf"
        slicedata = tmp / "slicedata"
        exported_settings = tmp / "merged_settings.json"

        cmd = [
            "xvfb-run", "-a", binpath,
            "--debug", "4",
            "--datadir", str(tmp / "cfg"),
            "--load-settings", f"{mfile};{pfile}",
            "--load-filaments", str(ffile),
            "--arrange", "1",
            "--orient", "1",
            str(in_stl),
            "--slice", "1",
            "--export-3mf", str(out3mf),
            "--export-slicedata", str(slicedata),
            "--export-settings", str(exported_settings),
        ]

        code, out, err = _run(cmd)

        # Settings-Schnappschuss einlesen (falls entstanden)
        settings_tail = ""
        if exported_settings.exists():
            try:
                with exported_settings.open("r", encoding="utf-8") as f:
                    settings_tail = _tail(f.read(), 100)
            except Exception:
                settings_tail = "(Konnte exportierte Settings nicht lesen)"

        resp = {
            "ok": code == 0,
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": _tail(out, 60),
            "stderr_tail": _tail(err, 60),
            "settings_tail": settings_tail,
            "profiles_used": {
                "printer_name": printer_name,
                "printer_path": str(PRINTER_FILE),
                "process_path": str(PROCESS_FILE),
                "filament_path": str(filament_file),
            },
            "inputs": {
                "unit": unit,
                "material": material,
                "infill": infill,
                "layer_height": layer_height,
                "nozzle": nozzle,
                "stl_bytes": len(raw),
            },
        }

        if code == 0:
            resp["artifacts"] = {
                "export_3mf_exists": out3mf.exists(),
                "slicedata_exists": slicedata.exists(),
                "export_settings_exists": exported_settings.exists(),
            }
            return JSONResponse(content=resp, status_code=200)

        # Spezielles Hilfspaket bei -17
        if code == 239 or code == -17:
            resp["hint"] = {
                "summary": "process not compatible with printer – Orca lehnt die Kombination der Presets ab.",
                "likely_causes": [
                    "Der exakte Druckername fehlt in compatible_printers (wird jetzt hart hinzugefügt).",
                    "Unpassende/inkonsistente Felder: nozzle_diameter (Typ/Liste), extruders, printer_model/variant.",
                    "Zahlen als Strings (max_print_height, layer heights) → werden jetzt normalisiert.",
                    "Bed-Shape war/ist nicht als Float-Paare vorhanden → wird konvertiert.",
                ],
                "what_we_bound": {
                    "printer_name": printer_name,
                    "copied_fields_into_process_and_filament": [
                        "printer_technology", "printer_model", "printer_variant",
                        "gcode_flavor", "extruders", "nozzle_diameter"
                    ]
                }
            }
            return JSONResponse(content={"detail": resp}, status_code=500)

        # generischer Fehler
        return JSONResponse(content={"detail": resp}, status_code=500)

# ------------------------------------------------------------
# Mini-UI auf "/"
# ------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Orca Cloud Helper</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui, sans-serif; max-width: 1000px; margin: 24px auto; padding: 0 16px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 16px; margin: 16px 0; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    button { padding: 8px 12px; cursor: pointer; }
    input, select { padding: 6px 8px; }
    pre { background: #f6f8fa; padding: 12px; border-radius: 8px; max-height: 420px; overflow:auto; }
  </style>
</head>
<body>
  <h1>Orca Cloud Helper</h1>

  <div class="card">
    <div class="row">
      <button onclick="hit('/health')">/health</button>
      <button onclick="hit('/slicer_env')">/slicer_env</button>
      <a href="/docs" target="_blank"><button>Swagger</button></a>
    </div>
    <pre id="out"></pre>
  </div>

  <div class="card">
    <h3>/slice_check</h3>
    <form id="sliceForm">
      <div class="row">
        <input type="file" name="file" required />
        <label>Unit:
          <select name="unit"><option>mm</option><option>inch</option></select>
        </label>
        <label>Material:
          <select name="material"><option>PLA</option><option>PETG</option><option>PC</option><option>ASA</option></select>
        </label>
        <label>Infill <input type="number" step="0.01" min="0" max="1" name="infill" value="0.2"/></label>
        <label>Layer <input type="number" step="0.01" min="0.05" max="0.6" name="layer_height" value="0.2"/></label>
        <label>Nozzle <input type="number" step="0.01" min="0.2" max="1.2" name="nozzle" value="0.4"/></label>
        <button type="submit">Slicing testen</button>
      </div>
    </form>
    <pre id="sliceOut"></pre>
  </div>

<script>
async function hit(path){
  const r = await fetch(path);
  const t = await r.text();
  document.getElementById('out').textContent = t;
}
document.getElementById('sliceForm').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice_check', { method:'POST', body: fd });
  const t = await r.text();
  document.getElementById('sliceOut').textContent = t;
});
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=INDEX_HTML)

# Optionaler Alias
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
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
