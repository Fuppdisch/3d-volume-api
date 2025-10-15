# ---------- app.py ----------
import os, shutil, tempfile, subprocess
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Volume API")

# CORS – passe origins an deine Domains an (oder "*" für schnelles Testen)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # TODO: produktiv restriktiver setzen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# bevorzugte/kompatible ENV-Variablen
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)

# Headless/GL-Setup
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LD_LIBRARY_PATH", "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH",""))
os.environ.setdefault("PATH", "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH",""))

CACHE = {}
MAX_STL_BYTES = 25 * 1024 * 1024  # 25 MB Upload-Limit für Tests

def slicer_exists() -> bool:
    return (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK)) or \
           (shutil.which(os.path.basename(SLICER_BIN)) is not None)

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/health", response_class=JSONResponse)
def health():
    return {
        "ok": True,
        "cache_size": len(CACHE),
        "slicer_bin": SLICER_BIN,
        "PRUSASLICER_BIN": os.getenv("PRUSASLICER_BIN"),
        "bin_exists": slicer_exists(),
        "shutil_which": shutil.which(os.path.basename(SLICER_BIN)),
    }

@app.get("/slicer_env", response_class=JSONResponse)
def slicer_env():
    """Kleiner Reality-Check: ist die Binary vorhanden & startbar?"""
    which = shutil.which(os.path.basename(SLICER_BIN))
    exists = slicer_exists()
    help_out: Optional[str] = None
    code: Optional[int] = None
    try:
        # Viele AppImages liefern Hilfe nur auf stderr – wir sammeln beides
        p = subprocess.run(
            [which or SLICER_BIN, "--help"],
            capture_output=True, text=True, timeout=8
        )
        code = p.returncode
        help_out = (p.stdout or p.stderr or "")[:2000]
    except Exception as e:
        help_out = f"exec-error: {e}"
    return {"ok": True, "bin_exists": exists, "which": which, "return_code": code, "help_snippet": help_out}

@app.post("/slice_check", response_class=JSONResponse)
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    """
    Reiner Upload-/Parameter-Test:
    - nimmt STL entgegen,
    - prüft Größe/Endung,
    - speichert temporär,
    - gibt Echo/Meta zurück.
    Noch KEIN echtes Slicen – das hängen wir danach dran.
    """
    fname = (file.filename or "").lower()
    if not fname.endswith(".stl"):
        raise HTTPException(status_code=400, detail="Nur STL-Dateien werden akzeptiert.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei > {MAX_STL_BYTES//(1024*1024)} MB.")

    # Temporär speichern – beweist, dass Upload & FS gehen
    with tempfile.NamedTemporaryFile(delete=True, suffix=".stl") as tmp:
        tmp.write(data)
        tmp.flush()
        size_bytes = len(data)

    # Noch kein Slicen – nur positive Bestätigung
    return {
        "ok": True,
        "received_bytes": size_bytes,
        "unit": unit,
        "material": material.upper(),
        "infill": float(infill),
        "layer_height": float(layer_height),
        "nozzle": float(nozzle),
        "slicer_bin": SLICER_BIN,
        "slicer_present": slicer_exists(),
    }
