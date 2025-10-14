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

# ===== Logging (Render zeigt uvicorn.error im Dashboard) =====
log = logging.getLogger("uvicorn.error")

# ===== FastAPI-Grundgerüst + CORS =====
app = FastAPI(title="3D Volume API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # TODO: in Produktion auf deine Domains einschränken:
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== In-Memory Store (für Volumen pro Upload) =====
MODELS: Dict[str, Dict] = {}

# ===== Dichten (g/cm³) fürs Gewicht =====
DENSITIES = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC":  1.20,
}

# ===== kleine Helfer =====
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

# ===== robuste Mesh-Reparatur über trimesh.repair =====
def _repair_mesh(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """Versionssichere Reparatur ohne m.fill_holes(); alles über trimesh.repair.*"""
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

    # füllt kleine Löcher (ändert m in-place; kein Rückgabemesh)
    try:
        repair.fill_holes(m)
    except Exception as e:
        log.warning(f"fill_holes failed: {e}")

    # nochmal doppelte/degenerate entfernen
    try:
        repair.remove_degenerate_faces(m)
        repair.remove_duplicate_faces(m)
    except Exception as e:
        log.warning(f"repair remove_* failed: {e}")

    try:
        m.process(validate=True)
    except Exception as e:
        log.warning(f"process(validate) failed: {e}")

    return m

# ===== Voxel-Fallback (mm³) für nicht-wasserdichte Meshes =====
def _voxel_fallback_mm3(m: trimesh.Trimesh) -> float:
    # adaptiver Pitch: ~300 Voxel über die kleinste Dimension, begrenzt 0.05…0.5 mm
    try:
        ext = (m.bounds[1] - m.bounds[0])
        min_dim = float(np.clip(ext, 1e-9, None).min())
        pitch = max(0.05, min(0.5, min_dim / 300.0))
    except Exception:
        pitch = 0.2  # konservativer Default
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

    # Einheiten: wir erwarten mm (falls nicht, z. B. von m → mm skalieren)
    if not unit_is_mm:
        try:
            mesh.apply_scale(1000.0)
        except Exception as e:
            log.warning(f"apply_scale failed: {e}")

    mesh = _repair_mesh(mesh)

    if mesh.is_watertight:
        try:
            return float(mesh.volume)  # mm³
        except Exception as e:
            log.warning(f"mesh.volume failed, fallback to voxel: {e}")

    # Fallback für nicht-dichte oder problematische Meshes
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

# ===== Endpoints =====
@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/docs", "/analyze", "/weight", "/health"]}

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
