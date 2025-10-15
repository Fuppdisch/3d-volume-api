# ---------- app.py ----------
import os
import shutil
import tempfile
import subprocess
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# --------------------------------------------------------------------------------------
# FastAPI-App & CORS
# --------------------------------------------------------------------------------------
app = FastAPI(title="Volume API")

# In Produktion allow_origins auf deine Domain einschränken
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------------------
# ENV / Pfade / Defaults
# --------------------------------------------------------------------------------------
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)

# Headless/GL – Fallbacks (Dockerfile setzt das ebenfalls)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "LD_LIBRARY_PATH",
    "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH", "")
)
os.environ.setdefault(
    "PATH",
    "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH", "")
)

MAX_STL_BYTES = 25 * 1024 * 1024  # 25 MB Uploadlimit
CACHE = {}

# --------------------------------------------------------------------------------------
# Utils
# --------------------------------------------------------------------------------------
def slicer_exists() -> bool:
    """Check: Orca-Binary vorhanden/ausführbar?"""
    return (
        (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK))
        or (shutil.which(os.path.basename(SLICER_BIN)) is not None)
    )

def run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Subprozess ausführen und Rückgaben liefern."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

# --------------------------------------------------------------------------------------
# UI (Root) – kleine Testseite mit Buttons & Upload
# --------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<meta charset="utf-8">
<title>Volume API – Tester</title>
<style>
  :root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
  body{font-family:system-ui,Segoe UI,Arial;margin:24px;line-height:1.45;color:var(--fg);background:#fff}
  h1{margin:0 0 16px;font-size:22px}
  .row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  button,input[type=submit]{padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#111827;color:#fff;cursor:pointer}
  button.secondary{background:#fff;color:#111827}
  pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:360px;overflow:auto}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px;background:#fff}
  label{font-weight:600;margin:0 6px 0 12px}
  small{color:var(--muted)}
</style>

<h1>Volume API – Schnelltester</h1>
<div class="row">
  <button class="secondary" onclick="openDocs()">Swagger (API-Doku)</button>
  <button onclick="hit('/health','#out')">Health</button>
  <button onclick="hit('/slicer_env','#out')">Slicer-Env</button>
</div>

<div class="card">
  <h3 style="margin-top:0">Upload-Test (<code>/slice_check</code>)</h3>
  <form id="f" onsubmit="return sendSliceCheck(event)">
    <input type="file" name="file" accept=".stl" required>
    <label>unit</label>
    <select name="unit"><option>mm</option><option>cm</option></select>
    <label>material</label>
    <select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select>
    <label>infill</label><input name="infill" type="number" step="0.01" value="0.2" style="width:90px">
    <label>layer_height</label><input name="layer_height" type="number" step="0.01" value="0.2" style="width:90px">
    <label>nozzle</label><input name="nozzle" type="number" step="0.1" value="0.4" style="width:90px">
    <input type="submit" value="Hochladen & prüfen">
    <div><small>Max. 25 MB • akzeptiert: .stl</small></div>
  </form>
</div>

<pre id="out">Output erscheint hier …</pre>

<script>
const base = location.origin;

async function hit(path, sel){
  const out = document.querySelector(sel);
  out.textContent = 'Lade ' + path + ' …';
  try{
    const r = await fetch(base + path);
    const isJson = (r.headers.get('content-type')||'').includes('application/json');
    const txt = isJson ? JSON.stringify(await r.json(), null, 2) : await r.text();
    out.textContent = txt;
  }catch(e){ out.textContent = 'Fehler: ' + e; }
}

async function sendSliceCheck(e){
  e.preventDefault();
  const fd = new FormData(e.target);
  const out = document.querySelector('#out');
  out.textContent = 'Lade /slice_check …';
  try{
    const r = await fetch(base + '/slice_check', { method:'POST', body: fd });
    const j = await r.json();
    out.textContent = JSON.stringify(j, null, 2);
  }catch(err){ out.textContent = 'Fehler: ' + err; }
  return false;
}

function openDocs(){ window.open(base + '/docs', '_blank'); }
</script>
"""

# --------------------------------------------------------------------------------------
# API: Health & Slicer-Env
# --------------------------------------------------------------------------------------
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
    """Reality-Check: Orca via xvfb-run mit --help starten und Ausschnitt zeigen."""
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
    return {"ok": True, "bin_exists": exists, "which": which, "return_code": code, "help_snippet": help_out}

# --------------------------------------------------------------------------------------
# API: Upload-/Parameter-Test (ohne echtes Slicen)
# --------------------------------------------------------------------------------------
@app.post("/slice_check", response_class=JSONResponse)
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    """Nimmt STL entgegen, prüft Größe/Endung, schreibt kurz temp und echo’t Parameter."""
    fname = (file.filename or "").lower()
    if not fname.endswith(".stl"):
        raise HTTPException(status_code=400, detail="Nur STL-Dateien werden akzeptiert.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei > {MAX_STL_BYTES // (1024*1024)} MB.")

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

# --------------------------------------------------------------------------------------
# API: Platzhalter fürs echte Slicen (sicherer Stub)
# --------------------------------------------------------------------------------------
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
    # Basisprüfung (wie /slice_check)
    if not (file.filename or "").lower().endswith(".stl"):
        raise HTTPException(status_code=400, detail="Nur STL-Dateien werden akzeptiert.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei > {MAX_STL_BYTES // (1024*1024)} MB.")

    # Noch nicht implementiert – bis Flags/Profiles final sind
    raise HTTPException(
        status_code=501,
        detail={
            "message": "Slicing serverseitig noch nicht aktiviert.",
            "hint": "Nutze /slicer_env für CLI-Check und /slice_check für Upload-Test.",
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
