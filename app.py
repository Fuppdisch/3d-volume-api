import io, uuid
from typing import Dict
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import numpy as np
import trimesh

try:
    import pymeshfix
    HAS_MESHFIX = True
except Exception:
    HAS_MESHFIX = False

app = FastAPI(title="3D Volume API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # PROD: auf deine Domain(en) begrenzen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MODELS: Dict[str, Dict] = {}

def _round3(x: float) -> float:
    return float(f"{x:.3f}")

def _repair_mesh(m: trimesh.Trimesh) -> trimesh.Trimesh:
    m.remove_duplicate_faces()
    m.remove_degenerate_faces()
    m.remove_unreferenced_vertices()
    m.merge_vertices()
    try: m.process(validate=True)
    except Exception: pass

    closed = m.fill_holes()
    m = closed or m

    if not m.is_watertight and HAS_MESHFIX:
        try:
            v = m.vertices.copy()
            f = m.faces.copy()
            mf = pymeshfix.MeshFix(v, f)
            mf.repair(verbose=False)
            v2, f2 = mf.return_arrays()
            m = trimesh.Trimesh(vertices=v2, faces=f2, process=True)
        except Exception:
            pass
    return m

def _voxel_fallback_mm3(m: trimesh.Trimesh) -> float:
    ext = (m.bounds[1] - m.bounds[0])
    min_dim = float(np.clip(ext, 1e-9, None).min())
    pitch = max(0.05, min(0.5, min_dim / 300.0))
    vx = m.voxelized(pitch=pitch)
    solid = vx.fill()
    return float(solid.points.shape[0] * (pitch ** 3))

def compute_volume_mm3_from_bytes(stl_bytes: bytes, unit_is_mm: bool = True) -> float:
    m = trimesh.load(io.BytesIO(stl_bytes), file_type='stl', force='mesh')
    if m.is_empty:
        raise HTTPException(status_code=400, detail="STL enthält kein gültiges Mesh.")
    if not unit_is_mm:
        m.apply_scale(1000.0)  # z. B. m → mm
    m = _repair_mesh(m)
    if m.is_watertight:
        return float(m.volume)
    return _voxel_fallback_mm3(m)

class AnalyzeResponse(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_model(file: UploadFile = File(...), unit_is_mm: bool = Form(True)):
    stl_bytes = await file.read()
    vol_mm3 = compute_volume_mm3_from_bytes(stl_bytes, unit_is_mm=unit_is_mm)
    model_id = str(uuid.uuid4())
    MODELS[model_id] = {"volume_mm3": vol_mm3}
    return AnalyzeResponse(
        model_id=model_id,
        volume_mm3=_round3(vol_mm3),
        volume_cm3=_round3(vol_mm3 / 1000.0),
    )
