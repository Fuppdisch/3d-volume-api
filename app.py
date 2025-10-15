# ---------- app.py ----------
import os
import shutil
import tempfile
import subprocess
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# --------------------------------------------------------------------------------------
# FastAPI-App
# --------------------------------------------------------------------------------------
app = FastAPI(title="Volume API")

# CORS (für deinen Web-Kalkulator). In Produktion: allow_origins auf deine Domain begrenzen!
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # TODO: in Prod auf https://deine-domain.tld o.ä. einschränken
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------------------
# ENV / Pfade / Defaults
# (Dockerfile setzt SLICER_BIN; wir halten Fallbacks für Kompatibilität)
# --------------------------------------------------------------------------------------
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)

# Headless/GL – als Fallback, falls im Container nicht gesetzt
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "LD_LIBRARY_PATH",
    "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH", "")
)
os.environ.setdefault(
    "PATH",
    "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH", "")
)

MAX_STL_BYTES = 25 * 1024 * 1024  # 25 MB Upload-Limit (anpassbar)
CACHE = {}                        # einfache In-Memory-Info, z. B. für spätere Caches

# --------------------------------------------------------------------------------------
# Utils
# --------------------------------------------------------------------------------------
def slicer_exists() -> bool:
    """Prüft, ob die Orca-Binary existiert/ausführbar ist (direkter Pfad ODER via $PATH)."""
    return (
        (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK))
        or (shutil.which(os.path.basename(SLICER_BIN)) is not None)
    )

def run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Subprozess ausführen, stdout/stderr zurückgeben."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
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
    """
    Reality-Check: lässt sich die Binary starten?
    Wir rufen 'xvfb-run -a <bin> --help' auf und geben einen Ausschnitt zurück.
    """
    which = shutil.which(os.path.basename(SLICER_BIN))
    exists = slicer_exists()
    help_out: Optional[str] = None
    code: Optional[int] = None
    try:
        cmd = ["xvfb-run", "-a", which or SLICER_BIN, "--help"]
        code, out, err = run(cmd, timeout=10)
        help_out = (out or err)[:2000] if (out or err) else ""
    except Exception as e:
        help_out = f"exec-error: {e}"
    return {
        "ok": True,
        "bin_exists": exists,
        "which": which,
        "return_code": code,
        "help_snippet": help_out,
    }

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
    - nimmt STL entgegen, prüft Endung/Größe
    - speichert kurz temporär (Proof of Upload)
    - gibt Echo/Meta zurück
    """
    fname = (file.filename or "").lower()
    if not fname.endswith(".stl"):
        raise HTTPException(status_code=400, detail="Nur STL-Dateien werden akzeptiert.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei > {MAX_STL_BYTES // (1024*1024)} MB.")

    # temporär schreiben (nur um FS-Write zu verifizieren)
    with tempfile.NamedTemporaryFile(delete=True, suffix=".stl") as tmp:
        tmp.write(data)
        tmp.flush()
        size_bytes = len(data)

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

@app.post("/slice", response_class=JSONResponse)
async def slice_stub(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
    export_kind: str = Form("3mf"),   # "3mf" | "gcode"
):
    """
    Sicherer Stub fürs echte Slicen:
    - Nimmt Parameter entgegen
    - Antwortet bewusst mit 501, bis Profile/Flags final stehen
    (So verhinderst du 500er im Livebetrieb, kannst Frontend aber schon anbinden.)
    """
    # Minimale Validierung wie in /slice_check
    if not (file.filename or "").lower().endswith(".stl"):
        raise HTTPException(status_code=400, detail="Nur STL-Dateien werden akzeptiert.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei > {MAX_STL_BYTES // (1024*1024)} MB.")

    # Noch nicht implementiert, bis wir deine Orca-Flags/Profiles finalisiert haben
    raise HTTPException(
        status_code=501,
        detail={
            "message": "Slicing ist serverseitig noch nicht aktiviert.",
            "hint": "Nutze /slicer_env zum Prüfen der CLI und /slice_check für den Upload-Test.",
            "params_echo": {
                "unit": unit,
                "material": material,
                "infill": float(infill),
                "layer_height": float(layer_height),
                "nozzle": float(nozzle),
                "export_kind": export_kind,
            },
        },
    )
