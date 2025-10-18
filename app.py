# app.py
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------
# Konstante Pfade (Variante A)
# ----------------------------
REPO_DIR = Path("/app/profiles")
PRINTER_FILE = REPO_DIR / "printers" / "X1C.json"
PROCESS_FILE = REPO_DIR / "process" / "0.20mm_standard.json"
FILAMENT_DIR = REPO_DIR / "filaments"

SUPPORTED_FILAMENTS = {"PLA", "PETG", "PC", "ASA"}

# ----------------------------
# FastAPI Setup
# ----------------------------
app = FastAPI(title="OrcaSlicer API (Variante A)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # bei Bedarf einschränken
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Utility
# ----------------------------
def _read_json_file(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Datei nicht gefunden: {path}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON fehlerhaft in {path}: {e}")

def _write_json_file(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _to_float(x: Any) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            pass
    raise ValueError(f"Kann nicht in float konvertieren: {x!r}")

def _to_int(x: Any) -> int:
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x)
    if isinstance(x, str):
        try:
            return int(float(x.strip()))
        except ValueError:
            pass
    raise ValueError(f"Kann nicht in int konvertieren: {x!r}")

def _string_pair_to_xy(s: str) -> Tuple[float, float]:
    # "400x0" -> (400.0, 0.0)
    if isinstance(s, str) and "x" in s:
        a, b = s.split("x", 1)
        return (_to_float(a), _to_float(b))
    raise ValueError(f"Kein 'AxB'-String: {s!r}")

def _ensure_bed_shape(machine: Dict[str, Any]) -> None:
    """
    Orca erwartet bed_shape als Liste von Float-Paaren. Konvertiere ggf. von printable_area Strings.
    """
    if "bed_shape" in machine and isinstance(machine["bed_shape"], list):
        # bereits korrekt? (Liste von Paaren)
        pairs = []
        for p in machine["bed_shape"]:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                pairs.append((_to_float(p[0]), _to_float(p[1])))
            elif isinstance(p, str):
                pairs.append(_string_pair_to_xy(p))
            else:
                raise ValueError("bed_shape-Element hat unerwartetes Format")
        machine["bed_shape"] = pairs
        return

    # Fallback aus printable_area: ["0x0","400x0","400x400","0x400"]
    if "printable_area" in machine and isinstance(machine["printable_area"], list):
        pairs = [_string_pair_to_xy(s) for s in machine["printable_area"]]
        machine["bed_shape"] = pairs
        # printable_area optional entfernen (stört nicht – lassen wir stehen)
    else:
        # Minimal-Bett, wenn gar nichts da ist
        machine["bed_shape"] = [(0.0, 0.0), (200.0, 0.0), (200.0, 200.0), (0.0, 200.0)]

def _harden_machine(raw: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(raw)
    m.setdefault("type", "machine")
    m.setdefault("version", "1")
    m.setdefault("from", "user")
    m.setdefault("printer_technology", "FFF")
    m.setdefault("gcode_flavor", "marlin")

    # Pflichtfelder sauber tippen:
    _ensure_bed_shape(m)

    if "max_print_height" in m:
        m["max_print_height"] = _to_float(m["max_print_height"])
    else:
        m["max_print_height"] = 300.0

    if "min_layer_height" in m:
        m["min_layer_height"] = _to_float(m["min_layer_height"])
    if "max_layer_height" in m:
        m["max_layer_height"] = _to_float(m["max_layer_height"])

    if "extruders" in m:
        m["extruders"] = _to_int(m["extruders"])
    else:
        m["extruders"] = 1

    # nozzle_diameter als Float-Liste
    nd = m.get("nozzle_diameter", [0.4])
    if not isinstance(nd, list):
        nd = [nd]
    m["nozzle_diameter"] = [_to_float(v) for v in nd]

    # Name ableiten falls nicht gesetzt
    if "name" not in m:
        variant = str(m["nozzle_diameter"][0])
        derived = f'Generic {int(m["bed_shape"][2][0])}x{int(m["bed_shape"][2][1])} {variant} nozzle'
        m["name"] = derived

    return m

def _harden_process(raw: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(raw)
    p.setdefault("type", "process")
    p.setdefault("version", "1")
    p.setdefault("from", "user")
    # nichts Aggressives – nur sicherstellen, dass Strings Zahlen sind, wo sinnvoll
    # (Orca parst viele Felder intern, hier lassen wir sie meist als String)

    # Manche Key-Varianten harmonisieren:
    if "initial_layer_height" in p and "first_layer_height" not in p:
        p["first_layer_height"] = p["initial_layer_height"]

    return p

def _harden_filament(raw: Dict[str, Any]) -> Dict[str, Any]:
    f = dict(raw)
    f.setdefault("type", "filament")
    f.setdefault("from", "user")
    if "filament_diameter" in f:
        # kann Liste oder Zahl/String sein – Orca akzeptiert meist Liste
        d = f["filament_diameter"]
        if not isinstance(d, list):
            d = [d]
        f["filament_diameter"] = [str(v) for v in d]
    return f

def _inject_compat(profile: Dict[str, Any], printer_name: str) -> None:
    """
    Sorgt dafür, dass compatible_printers den exakten Printer-Namen enthält.
    """
    lst = profile.get("compatible_printers")
    if lst is None:
        profile["compatible_printers"] = [printer_name]
    else:
        if not isinstance(lst, list):
            lst = [lst]
        if printer_name not in lst:
            lst.append(printer_name)
        profile["compatible_printers"] = lst
    # optional: compatible_printers_condition leer halten
    profile.setdefault("compatible_printers_condition", "")

def _choose_slicer_bin() -> Optional[str]:
    candidates = [
        "/opt/orca/bin/orca-slicer",
        "/usr/local/bin/orca-slicer",
        "/usr/bin/orca-slicer",
        "orca-slicer",
    ]
    for c in candidates:
        if shutil.which(c):
            return shutil.which(c)
    return None

def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 120) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=True,
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 997, "", f"{type(e).__name__}: {e}"

def _profile_paths_for_material(material: str) -> Tuple[Path, Path, Path]:
    prn = PRINTER_FILE
    proc = PROCESS_FILE
    fil = FILAMENT_DIR / f"{material.upper()}.json"
    if material.upper() not in SUPPORTED_FILAMENTS:
        raise HTTPException(status_code=400, detail=f"Unbekanntes Material '{material}'. Erlaubt: {sorted(SUPPORTED_FILAMENTS)}")
    if not prn.exists():
        raise HTTPException(status_code=404, detail=f"Printer-Profil fehlt: {prn}")
    if not proc.exists():
        raise HTTPException(status_code=404, detail=f"Process-Profil fehlt: {proc}")
    if not fil.exists():
        raise HTTPException(status_code=404, detail=f"Filament-Profil fehlt: {fil}")
    return prn, proc, fil

def _harden_and_stage_profiles(printer: Path, process: Path, filament: Path, work: Path) -> Tuple[Path, Path, Path, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    raw_machine = _read_json_file(printer)
    raw_process = _read_json_file(process)
    raw_filament = _read_json_file(filament)

    machine = _harden_machine(raw_machine)
    process_h = _harden_process(raw_process)
    filament_h = _harden_filament(raw_filament)

    printer_name = machine.get("name", "Generic")

    _inject_compat(process_h, printer_name)
    _inject_compat(filament_h, printer_name)

    out_prn = work / "printer_hardened.json"
    out_proc = work / "process_hardened.json"
    out_fil = work / "filament_hardened.json"

    _write_json_file(out_prn, machine)
    _write_json_file(out_proc, process_h)
    _write_json_file(out_fil, filament_h)

    return out_prn, out_proc, out_fil, machine, process_h, filament_h

# ----------------------------
# Root: Mini-UI zum Testen
# ----------------------------
ROOT_HTML = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>OrcaSlicer API – Test</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; line-height: 1.4; }
    code, pre { background: #f6f8fa; padding: 2px 6px; border-radius: 4px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
    button { padding: 8px 12px; border-radius: 8px; border: 1px solid #ccc; background: #fff; cursor: pointer; }
    button:hover { background: #f0f0f0; }
    .card { border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin: 12px 0; }
    .ok { color: #0a7f27; font-weight: 600; }
    .err { color: #b00020; font-weight: 600; }
    input[type="file"] { padding: 8px; }
    label { font-size: 14px; color: #444; }
    .grid { display:grid; grid-template-columns: 160px 1fr; gap:8px 12px; align-items:center; }
  </style>
</head>
<body>
  <h1>OrcaSlicer API – Testseite</h1>
  <div class="row">
    <button id="btnHealth">Health</button>
    <button id="btnEnv">Slicer Env</button>
    <a href="/docs" target="_blank"><button>Swagger (API-Doku)</button></a>
  </div>

  <pre id="out" class="card" style="white-space:pre-wrap;">Output…</pre>

  <h2>/slice_check</h2>
  <form id="sliceForm" class="card">
    <div class="grid">
      <label>STL:</label><input type="file" name="file" accept=".stl" required>
      <label>unit:</label><select name="unit">
        <option value="mm">mm</option>
        <option value="in">in</option>
      </select>
      <label>material:</label><select name="material">
        <option>PLA</option><option>PETG</option><option>PC</option><option>ASA</option>
      </select>
      <label>infill:</label><input name="infill" type="number" step="0.01" value="0.2">
      <label>layer_height:</label><input name="layer_height" type="number" step="0.01" value="0.2">
      <label>nozzle:</label><input name="nozzle" type="number" step="0.01" value="0.4">
    </div>
    <div style="margin-top:12px;">
      <button type="submit">Check starten</button>
    </div>
  </form>

<script>
const outEl = document.getElementById('out');

document.getElementById('btnHealth').onclick = async () => {
  const r = await fetch('/health');
  outEl.textContent = await r.text();
};

document.getElementById('btnEnv').onclick = async () => {
  const r = await fetch('/slicer_env');
  outEl.textContent = JSON.stringify(await r.json(), null, 2);
};

document.getElementById('sliceForm').onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice_check', { method: 'POST', body: fd });
  const ct = r.headers.get('content-type')||'';
  if (ct.includes('application/json')) {
    outEl.textContent = JSON.stringify(await r.json(), null, 2);
  } else {
    outEl.textContent = await r.text();
  }
};
</script>
</body>
</html>
"""

# ----------------------------
# Endpoints
# ----------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    # Kein f-string – roher String wegen JS-Klammern
    return HTMLResponse(content=ROOT_HTML)

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/slicer_env")
def slicer_env():
    binpath = _choose_slicer_bin()
    data = {
        "slicer_present": bool(binpath),
        "slicer_bin": binpath,
        "profiles": {
            "printer": [str(PRINTER_FILE)] if PRINTER_FILE.exists() else [],
            "process": [str(PROCESS_FILE)] if PROCESS_FILE.exists() else [],
            "filament": [str(p) for p in FILAMENT_DIR.glob("*.json")],
        },
    }
    if binpath:
        # OrcaSlicer –help als „Version/Hilfe“-Schnipsel
        code, out, err = _run([binpath, "--help"])
        data["return_code"] = code
        data["help_snippet"] = (out or err or "").splitlines()[:30]
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
    Führt einen 'trocken'-Slicinglauf mit Repo-Profilen aus:
    - Profile werden gehärtet und kompatibel gemacht
    - Modell wird optional orientiert/arrangiert
    - Es wird 3MF + slicedata exportiert (zur Validierung)
    """
    binpath = _choose_slicer_bin()
    if not binpath:
        raise HTTPException(status_code=500, detail="Orca-Slicer CLI nicht gefunden.")

    if unit not in {"mm", "in"}:
        raise HTTPException(status_code=400, detail="unit muss 'mm' oder 'in' sein.")

    # Profilpfade wählen
    prn_path, proc_path, fil_path = _profile_paths_for_material(material)

    # Arbeitsverzeichnis
    with tempfile.TemporaryDirectory(prefix="fixedp_") as td:
        tdir = Path(td)

        # Eingabedatei schreiben
        input_name = "input.stl"
        model_path = tdir / input_name
        model_bytes = await file.read()
        model_path.write_bytes(model_bytes)

        # Profile härten + stagen
        out_prn, out_proc, out_fil, machine, proc_h, fil_h = _harden_and_stage_profiles(
            prn_path, proc_path, fil_path, tdir
        )

        # Felder überschreiben, falls gewünscht (layer_height, nozzle, infill)
        try:
            # Layerhöhe in Process
            if layer_height:
                proc_h["layer_height"] = str(layer_height)
                # first_layer_height nur überschreiben, wenn nicht gesetzt
                if "first_layer_height" not in proc_h:
                    proc_h["first_layer_height"] = str(max(layer_height, 0.2))

            # Düse im Machine-Profil (nur erste Düse)
            if nozzle:
                machine["nozzle_diameter"] = [float(nozzle)]

            # Infill
            if infill is not None:
                # Orca akzeptiert z. B. "35%" oder Zahl. Wir verwenden Prozent-String.
                pct = f"{int(round(infill * 100))}%"
                proc_h["sparse_infill_density"] = pct

            # aktualisierte Profile erneut schreiben
            _write_json_file(out_proc, proc_h)
            _write_json_file(out_prn, machine)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Profil-Override fehlgeschlagen: {e}")

        # Ausgabepfade
        out_3mf = tdir / "out.3mf"
        out_slicedata = tdir / "slicedata"

        # CLI Kommando
        cmd = [
            "xvfb-run", "-a", binpath,
            "--debug", "0",
            "--datadir", str(tdir / "cfg"),
            "--load-settings", f"{out_prn};{out_proc}",
            "--load-filaments", str(out_fil),
            "--arrange", "1",
            "--orient", "1",
            str(model_path),
            "--slice", "1",
            "--export-3mf", str(out_3mf),
            "--export-slicedata", str(out_slicedata),
        ]

        code, out, err = _run(cmd, cwd=tdir, timeout=300)

        ok = (code == 0)
        detail = {
            "message": "Slicing erfolgreich." if ok else "Slicing fehlgeschlagen.",
            "ok": ok,
            "code": code,
            "cmd": " ".join(cmd),
            "stdout_tail": (out or "")[-500:],
            "stderr_tail": (err or "")[-500:],
            "profiles_used": {
                "printer": str(out_prn),
                "process": str(out_proc),
                "filament": str(out_fil),
            },
            "profile_debug": {
                "printer_preview": machine,
                "process_preview": proc_h,
                "filament_preview": fil_h,
            },
            "model": {
                "filename": file.filename,
                "bytes": len(model_bytes),
                "unit": unit,
            },
        }

        return JSONResponse(content={"detail": detail}, status_code=200 if ok else 500)

# Optionaler „Produktiv“-Slice (gleiches Prinzip wie slice_check).
@app.post("/slice")
async def slice_endpoint(
    file: UploadFile = File(...),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    # Für jetzt delegieren wir auf slice_check – später könnte hier G-code Export hinzukommen.
    return await slice_check(
        file=file,
        unit="mm",
        material=material,
        infill=infill,
        layer_height=layer_height,
        nozzle=nozzle,
    )

# ----------------------------
# Uvicorn Entrypoint
# ----------------------------
if __name__ == "__main__":
    # Lokaler Start: uvicorn app:app --reload --host 0.0.0.0 --port 8000
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
