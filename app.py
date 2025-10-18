import io
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import trimesh
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# -----------------------------
# FastAPI Grund-Setup + CORS
# -----------------------------

app = FastAPI(title="Online 3D-Druck Kalkulator & Slicer", version="0.5.0")

# CORS: in Produktion auf deine Domain(s) begrenzen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: später auf https://deinedomain.tld begrenzen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Konstanten / Profile / Dichte
# -----------------------------

API_BASE = os.environ.get("API_BASE", "")
ORCA_BIN_FALLBACKS = [
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "/usr/bin/orca-slicer",
]

PROFILES_ROOT = Path("/app/profiles")
PRINTER_DIR = PROFILES_ROOT / "printers"
PROCESS_DIR = PROFILES_ROOT / "process"
FILAMENT_DIR = PROFILES_ROOT / "filaments"

DEFAULT_PRINTER = "X1C.json"                 # passe bei Bedarf an
DEFAULT_PROCESS = "0.20mm_standard.json"     # passe bei Bedarf an

DENSITIES_G_CM3 = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC": 1.20,
}

# -----------------------------
# Utils
# -----------------------------

def _which_orca() -> Tuple[bool, str]:
    for path in ORCA_BIN_FALLBACKS:
        if Path(path).exists():
            return True, path
    # last resort: whatever "orca-slicer" resolves to
    try:
        out = subprocess.check_output(["which", "orca-slicer"], text=True).strip()
        if out:
            return True, out
    except Exception:
        pass
    return False, ""

def _tail(s: str, n: int = 40) -> str:
    lines = s.splitlines()
    return "\n".join(lines[-n:])

def _run(cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 180) -> Dict:
    """
    Führt einen Prozess aus und liefert Exitcode, stdout/stderr (Tail).
    """
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env or os.environ.copy(),
        )
        return {
            "code": p.returncode,
            "stdout_tail": _tail(p.stdout),
            "stderr_tail": _tail(p.stderr),
        }
    except subprocess.TimeoutExpired as e:
        return {"code": 124, "stdout_tail": _tail(e.stdout or ""), "stderr_tail": _tail(e.stderr or "TIMEOUT")}
    except Exception as e:
        return {"code": 1, "stdout_tail": "", "stderr_tail": f"{type(e).__name__}: {e}"}

def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _pick_profile(dirpath: Path, preferred_filename: str = "") -> Optional[Path]:
    if preferred_filename:
        p = dirpath / preferred_filename
        if p.exists():
            return p
    # else: first json file
    files = sorted(dirpath.glob("*.json"))
    return files[0] if files else None

def _match_filament(material: str) -> Optional[Path]:
    if not material:
        return _pick_profile(FILAMENT_DIR)
    material = material.upper()
    for p in sorted(FILAMENT_DIR.glob("*.json")):
        if material in p.stem.upper():
            return p
    return _pick_profile(FILAMENT_DIR)

# -----------------------------
# Volumen / Reparatur
# -----------------------------

def _repair_and_volume(file_bytes: bytes, unit: str = "mm") -> Dict[str, float]:
    """
    Robust: versucht normales Laden & Reparatur. Falls leaky, voxel-Fallback.
    """
    mesh = None
    try:
        mesh = trimesh.load(io.BytesIO(file_bytes), file_type=None, force="mesh")
    except Exception:
        pass

    if mesh is None or not isinstance(mesh, trimesh.Trimesh):
        raise HTTPException(400, detail="Datei ist kein gültiges Mesh (STL/3MF).")

    # Einheitenfaktor
    unit = (unit or "mm").lower()
    unit_scale = {"mm": 1.0, "cm": 10.0, "m": 1000.0}.get(unit, 1.0)

    # Reparatur (ohne fill_holes)
    try:
        trimesh.repair.fix_normals(mesh)
    except Exception:
        pass
    try:
        trimesh.repair.fill_degenerate_faces(mesh)
    except Exception:
        pass
    try:
        trimesh.repair.remove_degenerate_faces(mesh)
    except Exception:
        pass
    try:
        trimesh.repair.stitch(mesh)
    except Exception:
        pass

    if not mesh.is_volume:  # leaky? -> Voxel-Fallback
        try:
            # Voxel-Größe grob aus bounding box ableiten
            bbox = mesh.bounding_box.extents
            voxel_pitch = max(bbox) / 128.0 if max(bbox) > 0 else 1.0
            vox = mesh.voxelized(pitch=voxel_pitch)
            vol_mm3 = float(vox.as_boxes().volume)  # Näherung
        except Exception:
            vol_mm3 = float(mesh.volume) if mesh.is_volume else 0.0
    else:
        vol_mm3 = float(mesh.volume)  # volumetrisch korrekt

    # Einheitenskalierung (Mesh war in mm)
    if unit_scale != 1.0:
        # Volumen skaliert mit Faktor^3
        vol_mm3 = vol_mm3 * (unit_scale ** 3)

    return {
        "volume_mm3": round(vol_mm3, 3),
        "volume_cm3": round(vol_mm3 / 1000.0, 3),
    }

# -----------------------------
# Orca-Profil Normalisierung
# -----------------------------

def _parse_bed_shape(value):
    """
    JSON-Profile für Orca: Liste aus Zahlenpaaren ([[x,y],...]).
    Akzeptiert sowohl "0x0"-Strings als auch [[0,0],...]; normalisiert zu [[x,y],...].
    """
    pts = []
    if isinstance(value, list):
        for p in value:
            if isinstance(p, str) and "x" in p.lower():
                try:
                    x, y = p.lower().split("x", 1)
                    pts.append([float(x), float(y)])
                except Exception:
                    pass
            elif isinstance(p, (list, tuple)) and len(p) == 2:
                try:
                    pts.append([float(p[0]), float(p[1])])
                except Exception:
                    pass
    if len(pts) < 3:
        pts = [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]]
    return pts

def _normalize_machine(pj: dict) -> dict:
    pj = dict(pj or {})
    pj["type"] = "machine"
    pj.setdefault("name", "RatRig V-Core 4 400 0.4 nozzle")
    pj["printer_technology"] = "FFF"

    # JSON: bed_shape als Zahlenpaare
    pj["bed_shape"] = _parse_bed_shape(pj.get("bed_shape"))

    # Diese Felder als STRINGS (Orca-JSON-Parser erwartet das)
    try:
        pj["max_print_height"] = str(float(pj.get("max_print_height", 300)))
    except Exception:
        pj["max_print_height"] = "300"
    try:
        pj["extruders"] = str(int(pj.get("extruders", 1)))
    except Exception:
        pj["extruders"] = "1"

    # nozzle_diameter als Liste von Strings
    nd = pj.get("nozzle_diameter", ["0.4"])
    if isinstance(nd, (int, float)):
        nd = [nd]
    pj["nozzle_diameter"] = [str(x) for x in (nd if isinstance(nd, list) else [nd])]

    pj.setdefault("gcode_flavor", "marlin")
    return pj

def _normalize_process(pr: dict, infill: float) -> dict:
    pr = dict(pr or {})
    pr["type"] = "process"
    pr.setdefault("name", "0.20mm Standard")

    # Nur EIN Infill-Feld (Prozent-String). Entferne alte "fill_density" Kollisionsquelle.
    pr.pop("fill_density", None)
    pr["sparse_infill_density"] = f"{int(round(max(0, min(1, float(infill))) * 100))}%"

    # Offen kompatibel (eliminiert „process not compatible with printer“)
    base = set(map(str, pr.get("compatible_printers", [])))
    base.update({"*", "RatRig V-Core 4 400 0.4 nozzle"})
    pr["compatible_printers"] = sorted(base)
    pr["compatible_printers_condition"] = ""

    # Drucker-spezifische Keys entfernen (reduziert Prüfungen/Kollisionen)
    for k in ("printer_model", "printer_variant", "printer_technology", "gcode_flavor"):
        pr.pop(k, None)

    return pr

def _harden_filament(fj: dict) -> dict:
    fj = dict(fj or {})
    fj.setdefault("type", "filament")
    # Konservativ: kompatibel mit allen
    fj["compatible_printers"] = ["*"]
    fj["compatible_printers_condition"] = ""
    return fj

# -----------------------------
# Schemas
# -----------------------------

class AnalyzeResp(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float

class WeightDirectReq(BaseModel):
    volume_mm3: float = Field(..., ge=0)
    material: str
    infill: float = Field(..., ge=0.0, le=1.0)

class WeightDirectResp(BaseModel):
    weight_g: float

# -----------------------------
# Routen
# -----------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        textwrap.dedent(
            f"""
            <!doctype html>
            <meta charset="utf-8"/>
            <title>Orca Tester · Online 3D-Druck</title>
            <style>
              body {{ font-family: ui-sans-serif, system-ui, Arial; margin: 40px; max-width: 900px }}
              h1 {{ margin-top: 0 }}
              button, input[type=submit] {{ padding: 8px 12px; }}
              .row {{ display:flex; gap:12px; flex-wrap: wrap; margin-bottom:16px }}
              .card {{ border:1px solid #ddd; border-radius:10px; padding:16px; }}
              pre {{ background:#0b1020; color:#d6e1ff; padding:12px; border-radius:8px; overflow:auto }}
              code {{ color:#ffe28a }}
              .muted {{ color:#666 }}
            </style>
            <h1>Orca-Slicer Self-Test & Upload</h1>

            <div class="row">
              <button onclick="fetchJSON('/health')">Health</button>
              <button onclick="fetchJSON('/slicer_env')">Slicer&nbsp;Env</button>
              <a href="/docs" target="_blank"><button>Swagger (API-Doku)</button></a>
            </div>

            <div class="card">
              <h3>/slice_check</h3>
              <form id="sliceForm">
                <div class="row">
                  <input type="file" name="file" required />
                  <label>unit:
                    <select name="unit">
                      <option>mm</option>
                      <option>cm</option>
                      <option>m</option>
                    </select>
                  </label>
                  <label>material:
                    <select name="material">
                      <option>PLA</option>
                      <option>PETG</option>
                      <option>ASA</option>
                      <option>PC</option>
                    </select>
                  </label>
                  <label>infill: <input type="number" step="0.01" min="0" max="1" name="infill" value="0.35"/></label>
                  <label class="muted">arrange: <input type="number" name="arrange" value="1"/></label>
                  <label class="muted">orient: <input type="number" name="orient" value="1"/></label>
                  <label class="muted">debug: <input type="number" name="debug" value="1"/></label>
                  <input type="submit" value="Slice starten"/>
                </div>
              </form>
            </div>

            <div class="card">
              <h3>/analyze</h3>
              <form id="volForm">
                <div class="row">
                  <input type="file" name="file" required />
                  <label>unit:
                    <select name="unit">
                      <option>mm</option>
                      <option>cm</option>
                      <option>m</option>
                    </select>
                  </label>
                  <input type="submit" value="Volumen berechnen"/>
                </div>
              </form>
            </div>

            <h3>Antwort</h3>
            <pre id="out"><code>...</code></pre>

            <script>
              async function fetchJSON(path) {{
                const res = await fetch(path);
                const txt = await res.text();
                document.querySelector('#out code').textContent = txt;
              }}
              document.querySelector('#sliceForm').addEventListener('submit', async (e) => {{
                e.preventDefault();
                const fd = new FormData(e.target);
                const res = await fetch('/slice_check', {{ method:'POST', body: fd }});
                document.querySelector('#out code').textContent = await res.text();
              }});
              document.querySelector('#volForm').addEventListener('submit', async (e) => {{
                e.preventDefault();
                const fd = new FormData(e.target);
                const res = await fetch('/analyze', {{ method:'POST', body: fd }});
                document.querySelector('#out code').textContent = await res.text();
              }});
            </script>
            """
        )
    )

@app.get("/health")
def health():
    ok, orca = _which_orca()
    return {"ok": True, "orca_found": ok, "orca_bin": orca, "time": int(time.time())}

@app.get("/slicer_env")
def slicer_env():
    ok, orca = _which_orca()
    resp = {
        "ok": ok,
        "bin_exists": ok,
        "which": orca,
        "return_code": None,
        "help_snippet": "",
        "profiles": {
            "printer": [str(p) for p in sorted(PRINTER_DIR.glob("*.json"))],
            "process": [str(p) for p in sorted(PROCESS_DIR.glob("*.json"))],
            "filament": [str(p) for p in sorted(FILAMENT_DIR.glob("*.json"))],
        },
    }
    if ok:
        r = _run([orca, "--help"])
        resp["return_code"] = r["code"]
        resp["help_snippet"] = _tail(r["stdout_tail"] or r["stderr_tail"], 60)
    return resp

@app.post("/analyze", response_model=AnalyzeResp)
async def analyze(file: UploadFile = File(...), unit: str = Form("mm")):
    data = await file.read()
    vols = _repair_and_volume(data, unit=unit)
    model_id = f"{hash((len(data), vols['volume_mm3'])) & 0xffffffff:x}"
    return AnalyzeResp(model_id=model_id, **vols)

@app.post("/weight_direct", response_model=WeightDirectResp)
def weight_direct(req: WeightDirectReq):
    material = (req.material or "PLA").upper()
    density = DENSITIES_G_CM3.get(material)
    if not density:
        raise HTTPException(400, detail=f"Unbekanntes Material '{req.material}'. Erlaubt: {', '.join(DENSITIES_G_CM3)}")
    # Volumen (mm³) -> cm³
    vol_cm3 = req.volume_mm3 / 1000.0
    # Einfaches Infill-Modell: nur Infill frisst Material (Perimeter ignoriert)
    weight_g = vol_cm3 * density * float(req.infill)
    return WeightDirectResp(weight_g=round(weight_g, 3))

# -----------------------------
# Slicing mit Orca-CLI
# -----------------------------

def _build_orca_cmd(
    orca_bin: str,
    datadir: Path,
    printer_json: Path,
    process_json: Path,
    filament_json: Path,
    input_model: Path,
    out_3mf: Path,
    slicedata_dir: Path,
    arrange: Optional[int] = None,
    orient: Optional[int] = None,
    debug: int = 0,
) -> List[str]:
    """
    Baut die Orca-CLI auf Basis der JSON-Profile.
    Wichtige Flags laut Orca 2.3.1: --load-settings, --load-filaments, --slice <option>
    """
    cmd = [
        "xvfb-run", "-a",
        orca_bin,
        "--debug", str(debug),
        "--datadir", str(datadir),
        "--load-settings", f"{printer_json};{process_json}",
        "--load-filaments", str(filament_json),
    ]
    if arrange is not None:
        cmd += ["--arrange", str(arrange)]  # 0=aus, 1=an, andere=auto
    if orient is not None:
        cmd += ["--orient", str(orient)]    # 0=aus, 1=an, andere=auto
    cmd += [
        str(input_model),
        "--slice", "1",                    # Plate 1
        "--export-3mf", str(out_3mf),
        "--export-slicedata", str(slicedata_dir),
    ]
    return cmd

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.35),
    arrange: Optional[int] = Form(1),
    orient: Optional[int] = Form(1),
    debug: int = Form(0),
):
    ok, orca = _which_orca()
    if not ok:
        raise HTTPException(500, detail={"message": "Orca-Slicer nicht gefunden", "which": orca})

    # Profile bestimmen
    printer_src = _pick_profile(PRINTER_DIR, DEFAULT_PRINTER)
    process_src = _pick_profile(PROCESS_DIR, DEFAULT_PROCESS)
    filament_src = _match_filament(material)

    if not printer_src or not process_src or not filament_src:
        raise HTTPException(500, detail={
            "message": "Erforderliche Profile fehlen",
            "printer": str(printer_src) if printer_src else None,
            "process": str(process_src) if process_src else None,
            "filament": str(filament_src) if filament_src else None,
        })

    # Dateien lesen
    try:
        pj_raw = _load_json(printer_src)
        pr_raw = _load_json(process_src)
        fj_raw = _load_json(filament_src)
    except Exception as e:
        raise HTTPException(500, detail=f"Profile konnten nicht geladen werden: {e}")

    # Normalisieren / härten
    pj = _normalize_machine(pj_raw)
    pr = _normalize_process(pr_raw, infill=float(infill))
    fj = _harden_filament(fj_raw)

    # Temporärer Arbeitsbereich
    tmp = Path(tempfile.mkdtemp(prefix="fixedp_"))
    try:
        cfg = tmp / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)

        printer_json = tmp / "printer_hardened.json"
        process_json = tmp / "process_hardened.json"
        filament_json = tmp / "filament_hardened.json"

        _save_json(printer_json, pj)
        _save_json(process_json, pr)
        _save_json(filament_json, fj)

        # Upload speichern
        input_path = tmp / "input_model"
        # Dateiendung beibehalten (stl/3mf)
        suffix = Path(file.filename or "model.stl").suffix or ".stl"
        input_path = input_path.with_suffix(suffix)
        data = await file.read()
        input_path.write_bytes(data)

        # Optional: Einheiten-Konvertierung (Orca kann auch --convert-unit; wir lassen Orca arbeiten)
        # Für reine Slicing-Kompatibilität reicht es meist ohne.

        out_3mf = tmp / "out.3mf"
        slicedata_dir = tmp / "slicedata"

        cmd = _build_orca_cmd(
            orca_bin=orca,
            datadir=cfg,
            printer_json=printer_json,
            process_json=process_json,
            filament_json=filament_json,
            input_model=input_path,
            out_3mf=out_3mf,
            slicedata_dir=slicedata_dir,
            arrange=arrange,
            orient=orient,
            debug=debug,
        )

        result = _run(cmd, timeout=240)

        if result["code"] != 0:
            # Diagnose zurückgeben
            return JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "message": "Slicing fehlgeschlagen.",
                        "cmd": " ".join(cmd),
                        "code": result["code"],
                        "stdout_tail": result["stdout_tail"],
                        "stderr_tail": result["stderr_tail"],
                        "printer_hardened_json": json.dumps(pj, ensure_ascii=False),
                        "process_hardened_json": json.dumps(pr, ensure_ascii=False),
                        "filament_hardened_json": json.dumps(fj, ensure_ascii=False),
                    }
                },
            )

        # Erfolg: ein paar Metadaten zurück
        meta = {
            "ok": True,
            "cmd": " ".join(cmd),
            "out_3mf_exists": out_3mf.exists(),
            "slicedata_exists": slicedata_dir.exists(),
        }
        # Volumen mit unserer robusten Analyse (optional, praktisch fürs Frontend)
        try:
            vols = _repair_and_volume(data, unit=unit)
            meta.update(vols)
        except Exception:
            pass

        return meta

    finally:
        # Artefakte NICHT auto-löschen, wenn du debuggen möchtest:
        # shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

# -----------------------------
# Fallback-Plain-Index (JSON)
# -----------------------------

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
