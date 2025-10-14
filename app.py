# app.py
import os, io, time, re, tempfile, subprocess, uuid, logging
from typing import Optional, Dict, Any

import numpy as np
import trimesh

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------
# Konfiguration / ENV
# ---------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "3D Volume/Weight API")
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(50 * 1024 * 1024)))  # 50MB
MODEL_TTL_SECONDS = int(os.getenv("MODEL_TTL_SECONDS", "7200"))           # 2h Cache

# PrusaSlicer (für /slice_check)
PRUSASLICER_BIN = os.getenv("PRUSASLICER_BIN", "/usr/bin/prusa-slicer")
PRUSASLICER_TIMEOUT = int(os.getenv("PRUSASLICER_TIMEOUT", "120"))

# CORS
ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# Dichten (g/cm³)
DENSITIES = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC":  1.20
}

# G-Code Header Parser (verschiedene PrusaSlicer-Versionen)
RE_TIME = re.compile(r";\s*estimated printing time.*?=\s*([0-9hms :]+)", re.I)
RE_TIME_ALT = re.compile(r";\s*TIME\s*:\s*(\d+)", re.I)  # Sekunden
RE_FIL_M = re.compile(r";\s*Filament used\s*:\s*([\d\.]+)\s*m", re.I)
RE_FIL_MM = re.compile(r";\s*Filament used\s*:\s*([\d\.]+)\s*mm", re.I)
RE_FIL_CM3 = re.compile(r"\(\s*([\d\.]+)\s*cm3\s*\)", re.I)
RE_FIL_G = re.compile(r";\s*filament used\s*\[g\]\s*=\s*([\d\.]+)", re.I)

def _parse_duration_to_seconds(s: str) -> float:
    s = s.strip()
    if ":" in s:
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 3:
            h, m, sec = parts
            return h*3600 + m*60 + sec
        if len(parts) == 2:
            m, sec = parts
            return m*60 + sec
    sec = 0
    for val, unit in re.findall(r"(\d+)\s*([hms])", s):
        val = int(val)
        if unit == "h": sec += val*3600
        elif unit == "m": sec += val*60
        elif unit == "s": sec += val
    return float(sec)

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

# ---------------------------------------------------------
# Model Cache für Volumen pro Upload
# ---------------------------------------------------------
# { model_id: { "ts": epoch, "volume_mm3": float, "meta": {...} } }
MODEL_CACHE: Dict[str, Dict[str, Any]] = {}

def _gc_cache():
    """Altlasten aus dem Cache werfen."""
    now = time.time()
    stale = [k for k, v in MODEL_CACHE.items() if now - v.get("ts", 0) > MODEL_TTL_SECONDS]
    for k in stale:
        MODEL_CACHE.pop(k, None)

# ---------------------------------------------------------
# Mesh-Reparatur optional mit pymeshfix
# ---------------------------------------------------------
try:
    import pymeshfix  # type: ignore
    HAS_MESHFIX = True
except Exception:
    HAS_MESHFIX = False

def _try_meshfix(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Falls möglich, mit pymeshfix reparieren (watertight)."""
    if not HAS_MESHFIX:
        return mesh
    try:
        v = mesh.vertices.copy()
        f = mesh.faces.copy()
        mf = pymeshfix.MeshFix(v, f)
        mf.repair(verbose=False)
        m2 = trimesh.Trimesh(vertices=mf.v, faces=mf.f, process=False)
        return m2
    except Exception as e:
        log.warning("meshfix failed: %s", e)
        return mesh

def _safe_volume_from_mesh(mesh: trimesh.Trimesh) -> float:
    """
    Volumen robust bestimmen.
    1) Wenn watertight: mesh.volume
    2) Versuch: meshfix → dann volume
    3) Fallback: konvexe Hülle
    """
    try:
        if isinstance(mesh, trimesh.Trimesh) and mesh.is_volume:
            return float(abs(mesh.volume))

        # Reparaturversuch
        m2 = _try_meshfix(mesh)
        if isinstance(m2, trimesh.Trimesh) and m2.is_volume:
            return float(abs(m2.volume))

        # Fallback: konvexe Hülle
        hull = mesh.convex_hull
        if isinstance(hull, trimesh.Trimesh) and hull.is_volume:
            return float(abs(hull.volume))
    except Exception as e:
        log.warning("volume computation fallback failed: %s", e)

    # Letzter Fallback: 0
    return 0.0

def _load_mesh_from_bytes(stl_bytes: bytes, unit: str = "mm") -> trimesh.Trimesh:
    """
    STL robust laden (binary oder ASCII), ohne aggressive Auto-Reparaturen.
    Skalierung: Wenn unit == 'cm', erst in cm laden, anschließend 10x nach mm.
    """
    # Viele STLs werden als application/octet-stream hochgeladen
    file_obj = io.BytesIO(stl_bytes)
    mesh = trimesh.load(file_obj, file_type='stl', process=False, maintain_order=True)
    if not isinstance(mesh, trimesh.Trimesh):
        # Falls mehrere Geometrien: zu einem Mesh zusammenfassen (Scene→Trimesh)
        if isinstance(mesh, trimesh.Scene):
            mesh = trimesh.util.concatenate(tuple(
                g for g in mesh.dump().values() if isinstance(g, trimesh.Trimesh)
            ))
        else:
            raise ValueError("STL konnte nicht als Trimesh geladen werden.")

    # Units -> immer in mm normalisieren
    unit = (unit or "mm").strip().lower()
    if unit == "cm":
        mesh.apply_scale(10.0)
    elif unit == "mm":
        pass
    else:
        # unbekannte Einheit -> mm annehmen
        pass

    # Geometrie aufräumen (duplikate vert/degenerate faces entfernen wo möglich)
    try:
        mesh.remove_duplicate_faces()
        mesh.remove_degenerate_faces()
        mesh.remove_unreferenced_vertices()
    except Exception:
        pass

    return mesh

def compute_volume_mm3_from_bytes(stl_bytes: bytes, unit: str = "mm") -> float:
    """
    Haupt-Volumenroutine. Liefert mm³.
    """
    mesh = _load_mesh_from_bytes(stl_bytes, unit=unit)
    vol_mm3 = _safe_volume_from_mesh(mesh)
    # Sanity: neg/NaN abfangen
    if not np.isfinite(vol_mm3) or vol_mm3 < 0:
        vol_mm3 = 0.0
    return float(vol_mm3)

# ---------------------------------------------------------
# FastAPI Setup
# ---------------------------------------------------------
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# Schemas
# ---------------------------------------------------------
class AnalyzeResponse(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float

class WeightRequest(BaseModel):
    model_id: str
    material: str
    infill: float

class WeightResponse(BaseModel):
    weight_g: float

class WeightDirectRequest(BaseModel):
    volume_mm3: float
    material: str
    infill: float

class SliceResponse(BaseModel):
    printable: bool
    issues: list
    time_s: float
    filament_m: float
    filament_cm3: float | None = None
    filament_g: float | None = None
    gcode_bytes: int | None = None

# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------
@app.get("/")
def root():
    return {"name": APP_NAME, "status": "ok"}

@app.get("/health")
def health():
    _gc_cache()
    return {"ok": True, "cache_size": len(MODEL_CACHE)}

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_model(
    file: UploadFile = File(..., description="STL-Datei"),
    unit: Optional[str] = Form(None),
    unit_is_mm: Optional[bool] = Form(None)  # Backwards-Compat (true/false)
):
    """
    Ermittelt präzises Volumen des hochgeladenen STL.
    Einheit:
      - bevorzugt 'unit' = 'mm' | 'cm'
      - falls 'unit_is_mm' gesetzt: True → mm, False → cm
    """
    if file.content_type not in (
        "application/sla", "application/octet-stream", "model/stl",
        "application/vnd.ms-pki.stl", "application/x-stl"
    ):
        raise HTTPException(400, "Bitte STL-Datei hochladen.")

    # Größenlimit prüfen (wenn Upload-Backend die Größe liefert)
    if getattr(file, "size", None) and file.size > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max 50 MB).")

    # Einheit auflösen
    resolved_unit = "mm"
    if unit:
        if unit.strip().lower() in ("mm","cm"):
            resolved_unit = unit.strip().lower()
    elif unit_is_mm is not None:
        resolved_unit = "mm" if unit_is_mm else "cm"

    raw = await file.read()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max 50 MB).")

    try:
        vol_mm3 = compute_volume_mm3_from_bytes(raw, unit=resolved_unit)
    except Exception as e:
        log.exception("Volumenberechnung fehlgeschlagen: %s", e)
        raise HTTPException(500, "Volumenberechnung fehlgeschlagen.")

    model_id = str(uuid.uuid4())
    MODEL_CACHE[model_id] = {
        "ts": time.time(),
        "volume_mm3": float(vol_mm3),
        "meta": {"unit": resolved_unit}
    }
    _gc_cache()

    return AnalyzeResponse(
        model_id=model_id,
        volume_mm3=round(vol_mm3, 3),
        volume_cm3=round(vol_mm3 / 1000.0, 3)
    )

@app.post("/weight", response_model=WeightResponse)
def weight(req: WeightRequest):
    """
    Gewicht aus zwischengespeichertem Volumen (model_id), Dichte & Infill.
    """
    _gc_cache()
    entry = MODEL_CACHE.get(req.model_id)
    if not entry:
        raise HTTPException(404, "model_id unbekannt oder abgelaufen.")
    volume_mm3 = float(entry.get("volume_mm3", 0.0))
    if volume_mm3 <= 0:
        raise HTTPException(400, "Volumen = 0.")

    mat = (req.material or "PLA").strip().upper()
    density = DENSITIES.get(mat, DENSITIES["PLA"])
    infill = max(0.01, min(1.0, float(req.infill)))

    cm3 = volume_mm3 / 1000.0
    weight_g = cm3 * density * infill
    return WeightResponse(weight_g=round(weight_g, 3))

@app.post("/weight_direct", response_model=WeightResponse)
def weight_direct(req: WeightDirectRequest):
    """
    Stateless: direkt mit volume_mm3 rechnen.
    """
    volume_mm3 = max(0.0, float(req.volume_mm3))
    mat = (req.material or "PLA").strip().upper()
    density = DENSITIES.get(mat, DENSITIES["PLA"])
    infill = max(0.01, min(1.0, float(req.infill)))

    cm3 = volume_mm3 / 1000.0
    weight_g = cm3 * density * infill
    return WeightResponse(weight_g=round(weight_g, 3))

# -----------------------------
# NEU: PrusaSlicer – /slice_check
# -----------------------------
@app.post("/slice_check", response_model=SliceResponse)
async def slice_check(
    file: UploadFile = File(..., description="STL"),
    unit: str = Form("mm"),           # "mm" | "cm"
    material: str = Form("PLA"),      # PLA|PETG|ASA|PC
    infill: float = Form(0.2),        # 0..1
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    # --- 0) Basisschutz & Normalisierung ---
    if file.content_type not in (
        "application/sla","application/octet-stream","model/stl",
        "application/vnd.ms-pki.stl","application/x-stl"
    ):
        raise HTTPException(400, "Bitte STL hochladen.")
    if getattr(file, "size", None) and file.size > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max 50 MB).")

    unit = (unit or "mm").strip().lower()
    if unit not in ("mm","cm"): unit = "mm"

    mat = (material or "PLA").strip().upper()
    if mat not in DENSITIES: mat = "PLA"

    try: infill = float(infill)
    except: infill = 0.2
    infill = max(0.01, min(1.0, infill))

    try: layer_height = float(layer_height)
    except: layer_height = 0.2
    layer_height = max(0.06, min(0.4, layer_height))

    try: nozzle = float(nozzle)
    except: nozzle = 0.4
    nozzle = max(0.2, min(1.0, nozzle))

    # --- 1) Datei speichern ---
    with tempfile.TemporaryDirectory() as td:
        stl_path = os.path.join(td, "model.stl")
        gcode_path = os.path.join(td, "out.gcode")
        cfg_path = os.path.join(td, "cfg.ini")

        raw = await file.read()
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, "Datei zu groß (max 50 MB).")

        with open(stl_path, "wb") as f:
            f.write(raw)

        # --- 2) (optional) Health-Issues (hier leer, kann mit analyze-Checks ergänzt werden)
        issues = []

        # --- 3) Minimal-INI generieren ---
        dens = DENSITIES.get(mat, 1.25)
        ini = f"""
print_settings_id =
filament_settings_id =
printer_settings_id =
layer_height = {layer_height}
nozzle_diameter = {nozzle}
fill_density = {infill*100}%
perimeters = 2
filament_diameter = 1.75
filament_density = {dens}
"""
        with open(cfg_path, "w") as f:
            f.write(ini)

        # --- 4) cm → mm skalieren (vor dem Slicen) ---
        scale_arg = ["--scale", "10"] if unit == "cm" else []

        # --- 5) PrusaSlicer aufrufen ---
        cmd = [
            PRUSASLICER_BIN,
            "--no-gui",
            "--export-gcode",
            "--load", cfg_path,
            "-o", gcode_path,
            stl_path,
            *scale_arg
        ]
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=PRUSASLICER_TIMEOUT, check=False, text=True
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "PrusaSlicer Timeout.")

        if res.returncode != 0 or not os.path.exists(gcode_path):
            log.error("PrusaSlicer stdout: %s", res.stdout[:4000])
            log.error("PrusaSlicer stderr: %s", res.stderr[:4000])
            raise HTTPException(500, "Slicing fehlgeschlagen.")

        # --- 6) G-Code Header parsen ---
        head = ""
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as g:
            for i, line in zip(range(200), g):
                head += line

        # Zeit (s)
        time_s = 0.0
        m = RE_TIME.search(head)
        if m:
            time_s = _parse_duration_to_seconds(m.group(1))
        else:
            m2 = RE_TIME_ALT.search(head)  # Sekunden
            if m2:
                time_s = float(m2.group(1))

        # Filament
        filament_m = None
        filament_cm3 = None
        filament_g = None

        m3 = RE_FIL_M.search(head)
        m4 = RE_FIL_MM.search(head)
        if m3:
            filament_m = float(m3.group(1))
        elif m4:
            filament_m = float(m4.group(1)) / 1000.0

        m5 = RE_FIL_CM3.search(head)
        if m5:
            filament_cm3 = float(m5.group(1))

        m6 = RE_FIL_G.search(head)
        if m6:
            filament_g = float(m6.group(1))
        if filament_g is None and filament_cm3 is not None:
            filament_g = filament_cm3 * dens

        printable = (time_s > 0) and (filament_m is not None)
        gcode_bytes = os.path.getsize(gcode_path) if os.path.exists(gcode_path) else None

        return {
            "printable": printable,
            "issues": issues,
            "time_s": round(time_s, 3),
            "filament_m": round(filament_m or 0.0, 5),
            "filament_cm3": round(filament_cm3, 5) if filament_cm3 is not None else None,
            "filament_g": round(filament_g, 5) if filament_g is not None else None,
            "gcode_bytes": gcode_bytes
        }
