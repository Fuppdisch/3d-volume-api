# app.py
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

# --------------------------------------------------------------------------------------
# Pfade & Helfer
# --------------------------------------------------------------------------------------

REPO_DIR = Path("/app/profiles")

def _first_existing(*candidates: Path) -> Path:
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # für klare Fehlermeldungen

# Standard-Dateinamen in deinem Repo
PRINTER_FILE = _first_existing(
    REPO_DIR / "printer"  / "X1C.json",
    REPO_DIR / "printers" / "X1C.json",
)

PROCESS_FILE = _first_existing(
    REPO_DIR / "process"   / "0.20mm_standard.json",
    REPO_DIR / "processes" / "0.20mm_standard.json",
)

FILAMENT_DIR = _first_existing(
    REPO_DIR / "filament",
    REPO_DIR / "filaments",
)

BUNDLE_FILE = REPO_DIR / "bundle_structure.json"

SUPPORTED_FILAMENTS = {"PLA", "PETG", "PC", "ASA"}

def _choose_slicer_bin() -> Optional[str]:
    # Bevorzugt /opt/orca/bin (Render-Image), fallback /usr/local/bin
    for p in ("/opt/orca/bin/orca-slicer", "/usr/local/bin/orca-slicer"):
        if Path(p).exists():
            return p
    return None

def _run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=env or os.environ.copy(),
            check=False,
            text=True,
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 997, "", f"{type(e).__name__}: {e}"

def _read_json(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(path: Path, data: Dict):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _printer_name_from_json(machine: Dict) -> Optional[str]:
    # Der Name, den Orca zum Abgleich nutzt (Preset-Name):
    return machine.get("name") or machine.get("printer_model")

def _normalize_machine(machine: Dict) -> Dict:
    """Robuste Typ-Normalisierung für bekannte Stolpersteine."""
    out = dict(machine)

    # bed_shape: Liste von [x,y] floats (nicht "0x0"-Strings)
    bs = out.get("bed_shape")
    if isinstance(bs, list):
        # falls versehentlich Strings drin sind -> konvertieren
        fixed = []
        for p in bs:
            if isinstance(p, str) and "x" in p:
                x, y = p.split("x")
                fixed.append([float(x), float(y)])
            elif isinstance(p, list) and len(p) == 2:
                fixed.append([float(p[0]), float(p[1])])
        if fixed:
            out["bed_shape"] = fixed
    # printable_area (älteres Schema) -> ignoriert Orca meist, aber falls vorhanden konvertieren
    pa = out.get("printable_area")
    if isinstance(pa, list):
        fixed = []
        for s in pa:
            if isinstance(s, str) and "x" in s:
                x, y = s.split("x")
                fixed.append([float(x), float(y)])
        if fixed:
            out["bed_shape"] = fixed  # Prefer bed_shape

    # Zahlenfelder sicherstellen
    for k in ("max_print_height", "min_layer_height", "max_layer_height"):
        if k in out:
            try:
                out[k] = float(out[k])
            except Exception:
                pass

    # extruders int
    if "extruders" in out:
        try:
            out["extruders"] = int(out["extruders"])
        except Exception:
            pass

    # nozzle_diameter -> Liste von floats
    if "nozzle_diameter" in out and isinstance(out["nozzle_diameter"], list):
        nd = []
        for v in out["nozzle_diameter"]:
            try:
                nd.append(float(v))
            except Exception:
                continue
        if nd:
            out["nozzle_diameter"] = nd

    # gcode_flavor & printer_technology beibehalten, falls gesetzt
    if "gcode_flavor" not in out:
        out["gcode_flavor"] = "marlin"
    if "printer_technology" not in out:
        out["printer_technology"] = "FFF"

    return out

def _ensure_compatibility(process: Dict, filament: Dict, exact_printer_name: str) -> Tuple[Dict, Dict]:
    """Sorgt dafür, dass der exakte Printer-Name in den kompatiblen Listen enthalten ist."""
    p = dict(process)
    f = dict(filament)

    def upsert_name(d: Dict):
        arr = d.get("compatible_printers")
        if not isinstance(arr, list):
            arr = []
        if exact_printer_name not in arr:
            arr = [exact_printer_name] + [x for x in arr if x != exact_printer_name]
        d["compatible_printers"] = arr
        # ergänze Metadaten (optional, schadet nicht)
        d.setdefault("printer_technology", "FFF")
        d.setdefault("printer_model", p.get("printer_model") or "Generic 400x400")
        d.setdefault("printer_variant", p.get("printer_variant") or "0.4")
        # nozzle_diameter als Zahl(en)
        nd = d.get("nozzle_diameter")
        if nd is not None:
            if isinstance(nd, list):
                d["nozzle_diameter"] = [float(x) for x in nd]
            else:
                try:
                    d["nozzle_diameter"] = [float(nd)]
                except Exception:
                    pass

    upsert_name(p)
    upsert_name(f)
    return p, f

def _find_filament_file(material: str) -> Optional[Path]:
    if not FILAMENT_DIR.exists():
        return None
    candidates = []
    # Bevorzugt exakt <MATERIAL>.json, sonst erster Treffer
    target = material.upper().strip()
    for f in FILAMENT_DIR.glob("*.json"):
        if f.stem.upper() == target:
            candidates.insert(0, f)
        else:
            candidates.append(f)
    return candidates[0] if candidates else None

def _tail(s: str, n: int = 12) -> str:
    if not s:
        return ""
    lines = s.splitlines()
    return "\n".join(lines[-n:])

# --------------------------------------------------------------------------------------
# FastAPI
# --------------------------------------------------------------------------------------

app = FastAPI(title="Orca Cloud Helper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

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
    }
    if BUNDLE_FILE.exists():
        try:
            data["bundle_structure"] = _read_json(BUNDLE_FILE)
        except Exception as e:
            data["bundle_structure_error"] = f"{type(e).__name__}: {e}"

    if binpath:
        code, out, err = _run([binpath, "--help"])
        data["return_code"] = code
        data["help_snippet"] = (_tail(out, 30) or _tail(err, 30))
    return JSONResponse(content=data)

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    """
    Schneidet ein einzelnes STL mit euren Profilen.
    Patches:
      - kompatible_printers: exakter Printer-Name
      - leichte Typ-Normalisierung für Machine
    """
    binpath = _choose_slicer_bin()
    if not binpath:
        return JSONResponse(status_code=500, content={"detail": "orca-slicer nicht gefunden"})

    # Profile prüfen
    if not PRINTER_FILE.exists():
        return JSONResponse(status_code=400, content={"detail": f"Printer-Profil fehlt: {PRINTER_FILE}"})
    if not PROCESS_FILE.exists():
        return JSONResponse(status_code=400, content={"detail": f"Process-Profil fehlt: {PROCESS_FILE}"})

    filament_file = _find_filament_file(material) or _find_filament_file("PLA")
    if not filament_file:
        return JSONResponse(status_code=400, content={"detail": f"Kein Filament-Profil gefunden (gesucht: {material})"})

    # Temporäre Arbeitsumgebung
    with tempfile.TemporaryDirectory(prefix="fixedp_") as tmpd:
        tmp = Path(tmpd)

        # STL speichern
        in_stl = tmp / ("input.stl" if unit.lower() == "mm" else "input_model.stl")
        raw = await file.read()
        in_stl.write_bytes(raw)

        # Profile laden & patchen
        try:
            machine = _read_json(PRINTER_FILE)
            process = _read_json(PROCESS_FILE)
            filament = _read_json(filament_file)
        except Exception as e:
            return JSONResponse(status_code=400, content={"detail": f"Profil-Leseproblem: {type(e).__name__}: {e}"})

        machine = _normalize_machine(machine)
        printer_name = _printer_name_from_json(machine) or "Generic 400x400 0.4 nozzle"
        process, filament = _ensure_compatibility(process, filament, printer_name)

        # override ein paar Process-Felder aus Formular (nicht zwingend, aber praktisch)
        process["layer_height"] = str(layer_height)
        process["first_layer_height"] = process.get("first_layer_height") or str(max(layer_height, 0.2))
        process["sparse_infill_density"] = f"{int(round(infill * 100))}%"
        process["perimeter_extrusion_width"] = process.get("perimeter_extrusion_width") or str(nozzle + 0.05)
        process["line_width"] = process.get("line_width") or str(nozzle + 0.05)

        # in tmp schreiben
        mfile = tmp / "printer.json"
        pfile = tmp / "process.json"
        ffile = tmp / "filament.json"
        _write_json(mfile, machine)
        _write_json(pfile, process)
        _write_json(ffile, filament)

        out3mf = tmp / "out.3mf"
        slicedata = tmp / "slicedata"

        # Orca-CLI zusammenbauen
        # Wichtig: --load-settings nimmt "A;B" (Semikolon) an, --load-filaments ähnliches Schema.
        cmd = [
            "xvfb-run", "-a", binpath,
            "--debug", "0",
            "--datadir", str(tmp / "cfg"),
            "--load-settings", f"{mfile};{pfile}",
            "--load-filaments", str(ffile),
            "--arrange", "1",
            "--orient", "1",
            str(in_stl),
            "--slice", "1",
            "--export-3mf", str(out3mf),
            "--export-slicedata", str(slicedata),
        ]

        code, out, err = _run(cmd)
        ok = (code == 0)
        resp = {
            "ok": ok,
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": _tail(out, 20),
            "stderr_tail": _tail(err, 20),
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

        # packe 3mf/slicedata nur im Erfolgsfall als Hinweise
        if ok:
            resp["artifacts"] = {
                "export_3mf_exists": out3mf.exists(),
                "slicedata_exists": slicedata.exists(),
            }
            return JSONResponse(content=resp, status_code=200)
        else:
            return JSONResponse(content={"detail": resp}, status_code=500)

# Kleine Testoberfläche (kein f-string, damit keine Klammer-Probleme)
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Orca Cloud Helper</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 30px auto; padding: 0 16px; }
    h1 { margin-top: 0; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 16px; margin: 16px 0; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; }
    button { padding: 8px 12px; cursor: pointer; }
    input, select { padding: 6px 8px; }
    pre { background: #f6f8fa; padding: 12px; border-radius: 8px; max-height: 360px; overflow:auto; }
  </style>
</head>
<body>
  <h1>Orca Cloud Helper</h1>

  <div class="card">
    <h3>Quick Checks</h3>
    <div class="row">
      <button onclick="hit('/health')">/health</button>
      <button onclick="hit('/slicer_env')">/slicer_env</button>
      <a href="/docs" target="_blank"><button>Swagger (API)</button></a>
    </div>
    <pre id="out"></pre>
  </div>

  <div class="card">
    <h3>/slice_check</h3>
    <form id="sliceForm">
      <div class="row">
        <input type="file" name="file" required />
        <label>Unit:
          <select name="unit">
            <option>mm</option>
            <option>inch</option>
          </select>
        </label>
        <label>Material:
          <select name="material">
            <option>PLA</option><option>PETG</option><option>PC</option><option>ASA</option>
          </select>
        </label>
        <label>Infill <input type="number" step="0.01" min="0" max="1" name="infill" value="0.2"/></label>
        <label>Layer <input type="number" step="0.01" min="0.05" max="0.4" name="layer_height" value="0.2"/></label>
        <label>Nozzle <input type="number" step="0.01" min="0.2" max="1.2" name="nozzle" value="0.4"/></label>
      </div>
      <div class="row" style="margin-top: 12px;">
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

document.getElementById('sliceForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice_check', { method: 'POST', body: fd });
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

# --------------------------------------------------------------------------------------
# Optional: /slice (direkter Slicing-Export) – hier als Alias zu /slice_check
# --------------------------------------------------------------------------------------
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

# --------------------------------------------------------------------------------------
# Entrypoint (für lokales Starten, auf Render startet gunicorn/uvicorn automatisch)
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)

