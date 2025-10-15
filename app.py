# ---------- app.py ----------
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse
import os, shutil

app = FastAPI(title="Volume API")

# Bevorzugt SLICER_BIN; rückwärtskompatibel zu PRUSASLICER_BIN/ORCASLICER_BIN
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)

CACHE = {}

def slicer_exists() -> bool:
    # Existiert der absolute Pfad ODER findet which den Befehl?
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
