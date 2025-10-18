# app.py
import os
import io
import json
import shutil
import tempfile
import hashlib
import subprocess
from typing import Optional, Literal, Dict, Any, List

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

# ---- Konfiguration ---------------------------------------------------------

API_TITLE = "Online 3D-Druck Kalkulator & Slicer API"
DESCRIPTION = "FastAPI-Backend für Volumen, Gewicht & OrcaSlicer-Checks"
VERSION = "2025-10-18"

# Pfade und Binaries
ORCA_CANDIDATES = [
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "orca-slicer",
]
XVFB = os.environ.get("XVFB_BIN", "/usr/bin/xvfb-run")

# Profile im Image/Repo
PROFILES_ROOT = os.environ.get("PROFILES_ROOT", "/app/profiles")
PRINTERS_DIR = os.path.join(PROFILES_ROOT, "printers")
PROCESS_DIR = os.path.join(PROFILES_ROOT, "process")
FILAMENTS_DIR = os.path.join(PROFILES_ROOT, "filaments")

# Materialdichten (g/cm³) – deckungsgleich mit deinem Frontend
MATERIAL_DENSITY = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC": 1.20,
}

# CORS – später auf deine Domain(s) begrenzen!
ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")

# ---- FastAPI Setup ---------------------------------------------------------

app = FastAPI(title=API_TITLE, description=DESCRIPTION, version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Modelle ---------------------------------------------------------------

class AnalyzeOut(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float

class WeightDirectIn(BaseModel):
    volume_mm3: float
    material: Literal["PLA", "PETG", "ASA", "PC"]
    infill: float  # 0..1

class WeightDirectOut(BaseModel):
    weight_g: float

# ---- Hilfsfunktionen: Volumen ---------------------------------------------

def _safe_float3(x: float) -> float:
    return float(f"{x:.3f}")

def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _analyze_mesh_bytes(data: bytes, unit: Literal["mm","cm","m"]) -> Dict[str, Any]:
    # Lazy Import: trimesh ist groß, erst hier laden
    import trimesh
    import numpy as np

    # Laden
    mesh = None
    try:
        mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    except Exception:
        # Retry: Autodetect
        mesh = trimesh.load(io.BytesIO(data), force="mesh")

    if not isinstance(mesh, trimesh.Trimesh):
        # Szenario: Scene -> vereinigen
        if hasattr(mesh, "dump"):
            mesh = mesh.dump().sum()

    # Einheiten-Skalierung
    if unit == "cm":
        mesh.apply_scale(10.0)  # 1 cm = 10 mm
    elif unit == "m":
        mesh.apply_scale(1000.0)  # 1 m = 1000 mm
    # bei "mm" nix tun

    # Reparatur
    try:
        trimesh.repair.fix_normals(mesh)
    except Exception:
        pass
    try:
        trimesh.repair.fill_holes(mesh)  # manche Orca-Versionen tolerieren offene Kanten schlecht
    except Exception:
        pass

    # Volumen robust bestimmen
    volume_mm3 = None
    if mesh.is_watertight:
        try:
            volume_mm3 = float(mesh.volume)
        except Exception:
            volume_mm3 = None

    if volume_mm3 is None or volume_mm3 <= 0:
        # Fallback: Voxelisierung
        try:
            extents = mesh.extents
            # Zielvoxelgröße: ~0.5 mm
            pitch = 0.5
            # bounding box volume ⇒ Anzahl Voxel abschätzen
            shape = np.maximum((extents / pitch).astype(int), 1)
            vox = mesh.voxelized(pitch=pitch)
            volume_mm3 = float(vox.points.shape[0]) * (pitch ** 3)
        except Exception:
            # Zweiter Fallback: Konvexe Hülle
            try:
                hull = mesh.convex_hull
                volume_mm3 = float(hull.volume)
            except Exception:
                raise HTTPException(status_code=422, detail="Volumen konnte nicht bestimmt werden")

    return {
        "volume_mm3": volume_mm3,
        "volume_cm3": volume_mm3 / 1000.0,
        "triangles": int(mesh.faces.shape[0]) if hasattr(mesh, "faces") else None,
        "watertight": bool(getattr(mesh, "is_watertight", False)),
        "bbox_mm": mesh.bounds.tolist() if hasattr(mesh, "bounds") else None,
    }

# ---- Hilfsfunktionen: Orca / Profile --------------------------------------

def _which_orca() -> Optional[str]:
    for cand in ORCA_CANDIDATES:
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    return None

def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _first_file(dirpath: str, exts: List[str]) -> Optional[str]:
    if not os.path.isdir(dirpath):
        return None
    for name in sorted(os.listdir(dirpath)):
        p = os.path.join(dirpath, name)
        if os.path.isfile(p) and any(name.lower().endswith(e) for e in exts):
            return p
    return None

def _parse_bed_shape(value) -> List[List[float]]:
    """
    Bed-Polygon als Liste von Float-Paaren. Akzeptiert:
    - bereits [[0,0],[400,0],...]
    - String-Liste ["0x0","400x0",...]
    - Fallback: 400x400
    """
    if isinstance(value, list) and value and isinstance(value[0], list):
        return [[float(a), float(b)] for a, b in value]

    pts: List[List[float]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and "x" in item:
                a, b = item.split("x", 1)
                try:
                    pts.append([float(a), float(b)])
                except Exception:
                    pass
    if len(pts) >= 3:
        return pts
    # Fallback: Quadrat 400x400
    return [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]]

def _normalize_machine(machine_in: Optional[dict]) -> dict:
    """
    Erzeuge ein *minimal kompatibles* Machine-JSON für Orca 2.3.1.
    Kritisch: numerische Typen für max_print_height & extruders!
    """
    bed_shape = _parse_bed_shape(
        (machine_in or {}).get("bed_shape")
        or (machine_in or {}).get("printable_area")
        or []
    )
    max_h = (machine_in or {}).get("max_print_height") \
            or (machine_in or {}).get("printable_height") \
            or 300.0

    try:
        max_h_val = float(max_h)
    except Exception:
        max_h_val = 300.0

    # Düsenliste
    nozzle = (machine_in or {}).get("nozzle_diameter", ["0.4"])
    if isinstance(nozzle, (int, float, str)):
        nozzle = [nozzle]
    nozzle = [str(x) for x in nozzle]

    m = {
        "type": "machine",
        "version": "1",
        "from": "user",
        "name": (machine_in or {}).get("name") or "Generic 0.4 nozzle",
        "printer_technology": "FFF",
        "gcode_flavor": "marlin",
        "bed_shape": bed_shape,             # Liste von Zahlenpaaren
        "max_print_height": max_h_val,      # NUMERISCH!
        "extruders": 1,                     # NUMERISCH!
        "nozzle_diameter": nozzle,          # Liste von Strings
    }
    return m

def _normalize_process(proc_in: Optional[dict], infill: float) -> dict:
    """
    Process-JSON mit erwarteten Schlüsseln. Nutzt first_layer_height statt initial_layer_height.
    Kompatibilität offen (["*"]).
    """
    layer_h = str((proc_in or {}).get("layer_height", "0.2"))
    first_layer = str((proc_in or {}).get("first_layer_height")
                      or (proc_in or {}).get("initial_layer_height")
                      or "0.3")

    # Infill als Prozentstring
    try:
        infill_pct = max(0.0, min(1.0, float(infill)))
    except Exception:
        infill_pct = 0.35
    infill_str = f"{int(round(infill_pct * 100))}%"

    # Unkritische Defaults
    line_w = str((proc_in or {}).get("line_width", "0.45"))
    perim = str((proc_in or {}).get("perimeters", "2"))
    top_l = str((proc_in or {}).get("top_solid_layers", (proc_in or {}).get("solid_layers", "3")))
    bot_l = str((proc_in or {}).get("bottom_solid_layers", (proc_in or {}).get("solid_layers", "3")))

    p = {
        "type": "process",
        "version": "1",
        "from": "user",
        "name": (proc_in or {}).get("name", "0.20mm Standard"),

        "layer_height": layer_h,
        "first_layer_height": first_layer,

        # möglichst wenig druckerspezifisches hier:
        "compatible_printers": ["*"],
        "compatible_printers_condition": "",

        # Infill
        "sparse_infill_density": infill_str,

        # ein paar harmlose Parameter
        "line_width": line_w,
        "perimeters": perim,
        "top_solid_layers": top_l,
        "bottom_solid_layers": bot_l,

        "outer_wall_speed": str((proc_in or {}).get("outer_wall_speed", "250")),
        "inner_wall_speed": str((proc_in or {}).get("inner_wall_speed", "350")),
        "travel_speed": str((proc_in or {}).get("travel_speed", "500")),

        "before_layer_gcode": (proc_in or {}).get("before_layer_gcode", ""),
        "layer_gcode": (proc_in or {}).get("layer_gcode", ""),
        "toolchange_gcode": (proc_in or {}).get("toolchange_gcode", ""),
        "printing_by_object_gcode": (proc_in or {}).get("printing_by_object_gcode", ""),
    }

    # Problematische Keys explizit entfernen
    for k in ("printer_model", "printer_variant", "printer_technology", "gcode_flavor", "fill_density", "nozzle_diameter"):
        p.pop(k, None)

    return p

def _load_profile_or_none(dirpath: str) -> Optional[dict]:
    p = _first_file(dirpath, [".json", ".ini"])  # wir bevorzugen JSON; INI nicht für diesen Pfad genutzt
    return _read_json(p) if (p and p.endswith(".json")) else None

def _load_filament_for_material(material: str) -> Optional[str]:
    """
    Liefert Pfad zur Filament-JSON, deren Dateiname das Material enthält (PLA, PETG, ASA, PC),
    sonst None.
    """
    if not os.path.isdir(FILAMENTS_DIR):
        return None
    names = sorted(os.listdir(FILAMENTS_DIR))
    for n in names:
        p = os.path.join(FILAMENTS_DIR, n)
        if os.path.isfile(p) and n.lower().endswith(".json") and material.lower() in n.lower():
            return p
    return None

def _run_orca(orcapath: str, work: str, input_stl: str, machine_json: str, process_json: str, filament_json: Optional[str],
              arrange: int = 1, orient: int = 1, debug: int = 1) -> Dict[str, Any]:
    out3mf = os.path.join(work, "out.3mf")
    slicedata = os.path.join(work, "slicedata")

    os.makedirs(slicedata, exist_ok=True)

    # Build CLI
    load_settings = f"{machine_json};{process_json}"
    cmd = [
        XVFB, "-a",
        orcapath,
        "--debug", str(int(debug)),
        "--datadir", os.path.join(work, "cfg"),
        "--load-settings", load_settings,
    ]
    if filament_json:
        cmd += ["--load-filaments", filament_json]

    # optionale Plate-Hilfen
    if arrange is not None:
        cmd += ["--arrange", str(int(arrange))]  # 0=aus, 1=an, sonst auto
    if orient is not None:
        cmd += ["--orient", str(int(orient))]    # 0=aus, 1=an, sonst auto

    cmd += [
        input_stl,
        "--slice", "1",                    # Platte 1
        "--export-3mf", out3mf,
        "--export-slicedata", slicedata,
    ]

    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "code": proc.returncode,
        "cmd": " ".join(cmd),
        "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-500:] if proc.stderr else "",
        "out_3mf": out3mf if ok and os.path.exists(out3mf) else None,
        "slicedata_dir": slicedata if ok and os.path.isdir(slicedata) else None,
    }

# ---- Routen ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return f"""
<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{API_TITLE}</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
h1{{margin:.2rem 0}} .row{{display:flex;gap:.5rem;flex-wrap:wrap;margin:.5rem 0}}
button,a.button{{padding:.6rem 1rem;border-radius:.6rem;border:1px solid #ccc;background:#fafafa;cursor:pointer;text-decoration:none;color:#111}}
code,pre{{background:#f6f8fa;border-radius:6px;padding:.5rem}}
.card{{border:1px solid #eee;border-radius:12px;padding:1rem;margin:1rem 0}}
label{{display:block;margin:.3rem 0 .1rem}}
input,select{{padding:.4rem;border:1px solid #ccc;border-radius:.4rem}}
</style>
</head>
<body>
<h1>{API_TITLE}</h1>
<p>Version: {VERSION}</p>

<div class="row">
  <a class="button" href="/health" target="_blank">Health</a>
  <a class="button" href="/slicer_env" target="_blank">Slicer-Env</a>
  <a class="button" href="/docs" target="_blank">Swagger (API)</a>
</div>

<div class="card">
  <h3>Test: /slice_check</h3>
  <form action="/slice_check" method="post" enctype="multipart/form-data" target="_blank">
    <label>STL-Datei</label>
    <input type="file" name="file" required />
    <div class="row">
      <div>
        <label>Unit</label>
        <select name="unit">
          <option value="mm">mm</option>
          <option value="cm">cm</option>
          <option value="m">m</option>
        </select>
      </div>
      <div>
        <label>Material</label>
        <select name="material">
          <option>PLA</option>
          <option>PETG</option>
          <option>ASA</option>
          <option>PC</option>
        </select>
      </div>
      <div>
        <label>Infill (0..1)</label>
        <input name="infill" type="number" step="0.01" min="0" max="1" value="0.35"/>
      </div>
      <div>
        <label>Arrange</label>
        <select name="arrange"><option value="1">1</option><option>0</option></select>
      </div>
      <div>
        <label>Orient</label>
        <select name="orient"><option value="1">1</option><option>0</option></select>
      </div>
      <div>
        <label>Debug</label>
        <select name="debug"><option value="1">1</option><option>0</option></select>
      </div>
    </div>
    <div style="margin-top:.5rem">
      <button type="submit">Slicen</button>
    </div>
  </form>
</div>

<details class="card">
  <summary>Tipps</summary>
  <ul>
    <li>Falls Kompatibilitätsfehler: Diese App erzeugt <em>minimale</em> kompatible JSON-Profile zur Entschärfung.</li>
    <li>Eigene Profile unter <code>/app/profiles/</code> ablegen (printer/process/filaments).</li>
    <li><code>--arrange</code>/<code>--orient</code> akzeptieren 0 (aus) oder 1 (an).</li>
  </ul>
</details>
</body>
</html>
    """

@app.get("/health")
def health():
    return {"ok": True, "service": "fastapi", "version": VERSION}

@app.get("/slicer_env")
def slicer_env():
    orca = _which_orca()
    help_snippet = None
    ret = None
    if orca:
        p = subprocess.run([orca, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ret = p.returncode
        help_snippet = (p.stdout or "")[:700]
    profiles = {
        "printer": [os.path.join(PRINTERS_DIR, n) for n in sorted(os.listdir(PRINTERS_DIR))] if os.path.isdir(PRINTERS_DIR) else [],
        "process": [os.path.join(PROCESS_DIR, n) for n in sorted(os.listdir(PROCESS_DIR))] if os.path.isdir(PROCESS_DIR) else [],
        "filament": [os.path.join(FILAMENTS_DIR, n) for n in sorted(os.listdir(FILAMENTS_DIR))] if os.path.isdir(FILAMENTS_DIR) else [],
    }
    return {
        "ok": bool(orca),
        "slicer_bin": orca,
        "slicer_present": bool(orca),
        "return_code": ret,
        "help_snippet": help_snippet,
        "profiles": profiles,
    }

@app.post("/analyze", response_model=AnalyzeOut)
async def analyze(file: UploadFile = File(...), unit: Literal["mm","cm","m"] = Form("mm")):
    data = await file.read()
    res = _analyze_mesh_bytes(data, unit=unit)
    model_id = _hash_bytes(data)
    return AnalyzeOut(
        model_id=model_id,
        volume_mm3=_safe_float3(res["volume_mm3"]),
        volume_cm3=_safe_float3(res["volume_cm3"]),
    )

@app.post("/weight_direct", response_model=WeightDirectOut)
def weight_direct(payload: WeightDirectIn):
    vol_cm3 = payload.volume_mm3 / 1000.0
    density = MATERIAL_DENSITY[payload.material]
    # Infill linear ansetzen (Gehäuse/Wände ignoriert – wie besprochen)
    weight = vol_cm3 * density * float(max(0.0, min(1.0, payload.infill)))
    return WeightDirectOut(weight_g=_safe_float3(weight))

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: Literal["mm","cm","m"] = Form("mm"),
    material: Literal["PLA","PETG","ASA","PC"] = Form("PLA"),
    infill: float = Form(0.35),
    arrange: int = Form(1),
    orient: int = Form(1),
    debug: int = Form(1),
):
    """
    Sliced die übergebene STL mit:
    - minimal gehärtetem Machine-Profil
    - minimal kompatiblem Process-Profil (first_layer_height, offene Kompatibilität)
    - Filament-Profil aus /app/profiles/filaments/<Material>.json (falls vorhanden)
    """
    orca = _which_orca()
    if not orca:
        raise HTTPException(status_code=500, detail="OrcaSlicer binary nicht gefunden")

    # Datei lesen & evtl. in mm konvertieren (wir skalieren im Analyze-Schritt; Orca bekommt mm)
    data = await file.read()
    # Optional: Mesh analysieren (validieren & unit->mm up-front), ansonsten direkt an Orca geben
    try:
        _ = _analyze_mesh_bytes(data, unit=unit)  # validiert grob und repariert minimal
    except Exception:
        pass  # selbst bei Analysefehler versuchen wir Orca-Slicing (Orca hat eigene Checks)

    # Profile aus Repo (wenn vorhanden)
    base_machine_json = _load_profile_or_none(PRINTERS_DIR)
    base_process_json = _load_profile_or_none(PROCESS_DIR)

    # Gehärtete (minimale) JSONs erzeugen
    machine_norm = _normalize_machine(base_machine_json)
    process_norm = _normalize_process(base_process_json, infill=infill)

    # Filamentprofil: aus Repo, sonst None (dann lädt Orca Standard)
    filament_path = _load_filament_for_material(material)

    with tempfile.TemporaryDirectory(prefix="fixedp_") as work:
        input_path = os.path.join(work, "input_model.stl")
        with open(input_path, "wb") as f:
            f.write(data)

        mach_path = os.path.join(work, "printer_hardened.json")
        proc_path = os.path.join(work, "process_hardened.json")
        with open(mach_path, "w", encoding="utf-8") as f:
            json.dump(machine_norm, f, ensure_ascii=False)
        with open(proc_path, "w", encoding="utf-8") as f:
            json.dump(process_norm, f, ensure_ascii=False)

        result = _run_orca(
            orcapath=orca,
            work=work,
            input_stl=input_path,
            machine_json=mach_path,
            process_json=proc_path,
            filament_json=filament_path,
            arrange=arrange,
            orient=orient,
            debug=debug,
        )

        if not result["ok"]:
            # Fehler transparent zurückgeben inkl. der verwendeten JSONs (gekürzt)
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Slicing fehlgeschlagen.",
                    "cmd": result["cmd"],
                    "code": result["code"],
                    "stdout_tail": result["stdout_tail"],
                    "stderr_tail": result["stderr_tail"],
                    "printer_hardened_json": json.dumps(machine_norm),
                    "process_hardened_json": json.dumps(process_norm),
                    "filament_used": filament_path,
                },
            )

        return {
            "ok": True,
            "cmd": result["cmd"],
            "out_3mf": result["out_3mf"],
            "slicedata_dir": result["slicedata_dir"],
        }

# ---- CLI Start (Render Dockerfile startet uvicorn mit dynamischem Port) ----

# Beispiel Dockerfile CMD:
# CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
