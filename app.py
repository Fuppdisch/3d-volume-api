# app.py
import io
import uuid
import logging
from typing import Dict

import numpy as np
import trimesh
from trimesh import repair

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ===== Logging (Render/uvicorn zeigt diesen Logger im Dashboard) =====
log = logging.getLogger("uvicorn.error")

# ===== FastAPI + CORS =====
app = FastAPI(title="3D Volume API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    # TODO: in Produktion auf deine Domains einschränken:
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== In-Memory Store (für /weight mit model_id) =====
MODELS: Dict[str, Dict] = {}

# ===== Dichten (g/cm³) =====
DENSITIES = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC":  1.20,
}

# ===== Helfer =====
def _round3(x: float) -> float:
    return float(f"{x:.3f}")

def _weight_g(volume_mm3: float, material: str, infill: float) -> float:
    mat = (material or "").upper()
    if mat not in DENSITIES:
        raise HTTPException(status_code=400, detail=f"Unbekanntes Material: {material}")
    try:
        f = float(infill)
    except Exception:
        raise HTTPException(status_code=400, detail="Infill ist ungültig.")
    if not (0.0 < f <= 1.0):
        raise HTTPException(status_code=400, detail="Infill muss zwischen 0 und 1 liegen (z. B. 0.2 für 20%).")
    cm3 = volume_mm3 / 1000.0
    return cm3 * DENSITIES[mat] * f

# ===== robuste Mesh-Reparatur über trimesh.repair (in-place) =====
def _repair_mesh(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """
    Versionssichere Reparatur nur über trimesh.repair.* (kein m.fill_holes()).
    Arbeitet in-place; alle Fehler werden geloggt, aber nicht als 500 geworfen.
    """
    try:
        m.remove_duplicate_faces()
        m.remove_degenerate_faces()
        m.remove_unreferenced_vertices()
        m.merge_vertices()
    except Exception as e:
        log.warning(f"basic clean failed: {e}")

    try:
        repair.fix_normals(m)
    except Exception as e:
        log.warning(f"fix_normals failed: {e}")

    # füllt kleine Löcher (ändert m in-place)
    try:
        repair.fill_holes(m)
    except Exception as e:
        log.warning(f"fill_holes failed: {e}")

    # manche repair-Funktionen sind versionsabhängig; still versuchen
    for fn_name in ("remove_degenerate_faces", "remove_duplicate_faces"):
        try:
            fn = getattr(repair, fn_name, None)
            if callable(fn):
                fn(m)
        except Exception as e:
            log.warning(f"repair {fn_name} failed: {e}")

    try:
        m.process(validate=True)  # optional (SciPy kann fehlen)
    except Exception as e:
        log.warning(f"process(validate) failed: {e}")

    return m

# ===== Voxel-Fallback (mm³) =====
def _voxel_fallback_mm3(m: trimesh.Trimesh) -> float:
    # adaptiver Pitch: ~300 Voxel über kleinste Dimension, begrenzt 0.05…0.5 mm
    try:
        ext = (m.bounds[1] - m.bounds[0])
        min_dim = float(np.clip(ext, 1e-9, None).min())
        pitch = max(0.05, min(0.5, min_dim / 300.0))
    except Exception:
        pitch = 0.2
    try:
        vx = m.voxelized(pitch=pitch)
        solid = vx.fill()
        return float(solid.points.shape[0] * (pitch ** 3))
    except Exception as e:
        log.error(f"voxel fallback failed: {e}")
        raise HTTPException(status_code=422, detail=f"Voxel-Fallback fehlgeschlagen: {e}")

# ===== Volumenberechnung (mm³) aus Bytes =====
def compute_volume_mm3_from_bytes(stl_bytes: bytes, unit_is_mm: bool = True) -> float:
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
    except Exception as e:
        log.error(f"load STL failed: {e}")
        raise HTTPException(status_code=422, detail=f"STL konnte nicht geladen werden: {e}")

    if mesh.is_empty:
        raise HTTPException(status_code=400, detail="STL enthält kein gültiges Mesh.")

    # Einheiten-Handling: wir erwarten mm
    if not unit_is_mm:
        try:
            mesh.apply_scale(1000.0)  # z. B. m → mm
        except Exception as e:
            log.warning(f"apply_scale failed: {e}")

    mesh = _repair_mesh(mesh)

    if mesh.is_watertight:
        try:
            return float(mesh.volume)  # mm³
        except Exception as e:
            log.warning(f"mesh.volume failed, fallback to voxel: {e}")

    # Fallback: robust bei Leaks/offenen Meshes
    return _voxel_fallback_mm3(mesh)

# ===== Pydantic Schemas =====
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

# ===== Endpoints =====
@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/docs", "/analyze", "/weight", "/weight_direct", "/health"]}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_model(
    file: UploadFile = File(...),
    unit_is_mm: bool = Form(True),
):
    stl_bytes = await file.read()
    vol_mm3 = compute_volume_mm3_from_bytes(stl_bytes, unit_is_mm=unit_is_mm)

    model_id = str(uuid.uuid4())
    MODELS[model_id] = {"volume_mm3": vol_mm3}

    return AnalyzeResponse(
        model_id=model_id,
        volume_mm3=_round3(vol_mm3),
        volume_cm3=_round3(vol_mm3 / 1000.0),
    )

@app.post("/weight", response_model=WeightResponse)
async def calc_weight(payload: WeightRequest):
    data = MODELS.get(payload.model_id)
    if not data:
        raise HTTPException(status_code=404, detail="model_id unbekannt oder abgelaufen.")
    g = _weight_g(data["volume_mm3"], payload.material, payload.infill)
    return {"weight_g": _round3(g)}

@app.post("/weight_direct", response_model=WeightResponse)
async def weight_direct(payload: WeightDirectRequest):
    g = _weight_g(payload.volume_mm3, payload.material, payload.infill)
    return {"weight_g": _round3(g)}

