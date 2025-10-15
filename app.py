# ---------- app.py ----------
import os
import re
import json
import shutil
import tempfile
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Volume API")

# --- CORS (in Produktion einschränken) ----------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ENV / Pfade --------------------------------------------------------------
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "LD_LIBRARY_PATH",
    "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH", "")
)
os.environ.setdefault(
    "PATH",
    "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH", "")
)

MAX_STL_BYTES = 25 * 1024 * 1024  # 25 MB
CACHE: dict[str, str] = {}

# --- Utils --------------------------------------------------------------------
def slicer_exists() -> bool:
    return (
        (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK))
        or (shutil.which(os.path.basename(SLICER_BIN)) is not None)
    )

def run(cmd: list[str], timeout: int = 900) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def find_profiles() -> dict:
    """Suche optionale Profile unter /app/profiles/{printers,process,filaments}."""
    base = Path("/app/profiles")
    res = {"printer": [], "process": [], "filament": []}
    if base.exists():
        for key, sub in [("printer", "printers"), ("process", "process"), ("filament", "filaments")]:
            d = base / sub
            if d.exists():
                res[key] = sorted(str(p) for p in d.glob("*.json"))
    return res

def parse_meta_from_gcode(text: str) -> dict:
    meta = {"duration_s": None, "filament_mm": None, "filament_g": None}
    m = re.search(r";\s*(estimated_print_time|print_time_sec)\s*=\s*([0-9.]+)", text)
    if m: meta["duration_s"] = float(m.group(2))
    m = re.search(r";\s*filament_used_mm\s*=\s*([0-9.]+)", text)
    if m: meta["filament_mm"] = float(m.group(1))
    m = re.search(r";\s*filament_used_g\s*=\s*([0-9.]+)", text)
    if m: meta["filament_g"] = float(m.group(1))
    return meta

# --- Mini UI auf "/" ----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<meta charset="utf-8">
<title>Volume API – Tester</title>
<style>
  :root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
  body{font-family:system-ui,Segoe UI,Arial;margin:24px;line-height:1.45;color:var(--fg)}
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
  <form id="f1" onsubmit="return sendSliceCheck(event)">
    <input type="file" name="file" accept=".stl,.3mf" required>
    <label>unit</label><select name="unit"><option>mm</option><option>cm</option></select>
    <label>material</label><select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select>
    <label>infill</label><input name="infill" type="number" step="0.01" value="0.2" style="width:90px">
    <label>layer_height</label><input name="layer_height" type="number" step="0.01" value="0.2" style="width:90px">
    <label>nozzle</label><input name="nozzle" type="number" step="0.1" value="0.4" style="width:90px">
    <input type="submit" value="Hochladen & prüfen">
    <div><small>Max. 25 MB • akzeptiert: .stl, .3mf</small></div>
  </form>
</div>

<div class="card">
  <h3 style="margin-top:0">Echt slicen (<code>/slice</code>)</h3>
  <form id="f2" onsubmit="return sendSlice(event)">
    <input type="file" name="file" accept=".stl,.3mf" required>
    <label>export_kind</label>
    <select name="export_kind">
      <option value="gcode">G-Code (.gcode)</option>
      <option value="3mf_project">3MF-Projekt (ungesliced)</option>
      <option value="3mf_sliced">3MF (gesliced, nur bei 3MF Input)</option>
    </select>
    <input type="submit" value="Slicen">
    <div><small>STL: am besten G-Code wählen. 3MF_sliced setzt --slice 0.</small></div>
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
    const isJson = (r.headers.get('content-type')||'').includes('json');
    out.textContent = isJson ? JSON.stringify(await r.json(), null, 2) : await r.text();
  }catch(e){ out.textContent = 'Fehler: ' + e; }
}
async function sendSliceCheck(e){
  e.preventDefault(); const fd = new FormData(e.target); const out = document.querySelector('#out');
  out.textContent = 'Lade /slice_check …';
  try{ const r = await fetch(base + '/slice_check', { method:'POST', body: fd });
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent = 'Fehler: ' + err; } return false;
}
async function sendSlice(e){
  e.preventDefault(); const fd = new FormData(e.target); const out = document.querySelector('#out');
  out.textContent = 'Slicen …';
  try{ const r = await fetch(base + '/slice', { method:'POST', body: fd });
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent = 'Fehler: ' + err; } return false;
}
function openDocs(){ window.open(base + '/docs', '_blank'); }
</script>
"""

# --- Health -------------------------------------------------------------------
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

# --- Slicer-Env ---------------------------------------------------------------
@app.get("/slicer_env", response_class=JSONResponse)
def slicer_env():
    which = shutil.which(os.path.basename(SLICER_BIN)) or SLICER_BIN
    exists = slicer_exists()

    def try_cmd(cmd):
        try:
            code, out, err = run(cmd, timeout=15)
            text = (out or err or "")
            return code, text[:2000]
        except Exception as e:
            return None, f"exec-error: {e}"

    # 1) Direkt --help
    code1, out1 = try_cmd([which, "--help"])
    if code1 is not None and out1:
        return {"ok": True, "bin_exists": exists, "which": which, "return_code": code1, "help_snippet": out1}

    # 2) xvfb-run Fallback
    xvfb = shutil.which("xvfb-run") or "xvfb-run"
    code2, out2 = try_cmd([xvfb, "-a", which, "--help"])
    return {"ok": True, "bin_exists": exists, "which": which, "return_code": code2, "help_snippet": out2}

# --- Upload/Param-Test --------------------------------------------------------
@app.post("/slice_check", response_class=JSONResponse)
async def slice_check(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.2),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
):
    fname = (file.filename or "").lower()
    if not (fname.endswith(".stl") or fname.endswith(".3mf")):
        raise HTTPException(status_code=400, detail="Nur STL- oder 3MF-Dateien werden akzeptiert.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei > {MAX_STL_BYTES // (1024*1024)} MB.")
    return {
        "ok": True,
        "received_bytes": len(data),
        "unit": unit,
        "material": material.upper(),
        "infill": float(infill),
        "layer_height": float(layer_height),
        "nozzle": float(nozzle),
        "slicer_bin": SLICER_BIN,
        "slicer_present": slicer_exists(),
    }

# --- Echtes Slicen: Auto-Fix-Prozess + Export-Matrix --------------------------
@app.post("/slice", response_class=JSONResponse)
async def slice_model(
    file: UploadFile = File(...),
    export_kind: str = Form("gcode"),  # "gcode" | "3mf_project" | "3mf_sliced"
):
    name = (file.filename or "").lower()
    if not (name.endswith(".stl") or name.endswith(".3mf")):
        raise HTTPException(400, "Nur STL- oder 3MF-Dateien werden akzeptiert.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_STL_BYTES:
        raise HTTPException(413, f"Datei > {MAX_STL_BYTES // (1024*1024)} MB.")

    work = Path(tempfile.mkdtemp(prefix="slice_"))
    is_3mf = name.endswith(".3mf")
    inp = work / ("input.3mf" if is_3mf else "input.stl")
    inp.write_bytes(data)

    datadir  = work / "cfg";       datadir.mkdir(parents=True, exist_ok=True)
    out_meta = work / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
    out_g    = work / "output.gcode"
    out_3mf  = work / "output.3mf"

    # --- Auto-Fix-Prozessprofil gegen "relative E"-Fehler ---
    tmp_process = work / "process_fix.json"
    tmp_process.write_text(json.dumps({
        "name": "auto_relative_e_fix",
        "use_relative_e_distances": False,   # absolute E erzwingen
        "layer_gcode": "G92 E0\n"            # zusätzlich pro Layer reset (unschädlich bei absoluten E)
    }, ensure_ascii=False))

    # optionale Profile auto-laden
    prof = find_profiles()
    settings_chain = [str(tmp_process)]
    if prof.get("printer"):  settings_chain.append(prof["printer"][0])
    if prof.get("process"):  settings_chain.append(prof["process"][0])
    filament_chain = []
    if prof.get("filament"): filament_chain.append(prof["filament"][0])

    def base_cmd():
        cmd = [SLICER_BIN, "--datadir", str(datadir), "--info", "--export-slicedata", str(out_meta), inp.as_posix()]
        if settings_chain:  cmd += ["--load-settings", ";".join(settings_chain)]
        if filament_chain:  cmd += ["--load-filaments", ";".join(filament_chain)]
        return cmd

    # --- Export-Matrix (ohne arrange/orient, ohne overrides) ---
    if export_kind == "gcode":
        cmd = base_cmd() + ["--export-gcode", str(out_g)]                   # slict implizit
    elif export_kind == "3mf_project":
        cmd = base_cmd() + ["--export-3mf", str(out_3mf)]                   # ungesliced Projekt
    elif export_kind == "3mf_sliced":
        if not is_3mf:
            raise HTTPException(400, "3mf_sliced erfordert eine .3mf Eingabedatei.")
        cmd = base_cmd() + ["--slice", "0", "--export-3mf", str(out_3mf)]   # geslictes 3MF (Platte 0 = alle)
    else:
        raise HTTPException(400, "export_kind muss 'gcode' | '3mf_project' | '3mf_sliced' sein.")

    code, out, err = run(["xvfb-run","-a"] + cmd, timeout=900)
    if code != 0:
        raise HTTPException(500, detail=f"Slicing fehlgeschlagen (exit {code}): {(err or out)[-1000:]}")

    # Metadaten einsammeln
    meta = {"duration_s": None, "filament_mm": None, "filament_g": None}
    for jf in out_meta.glob("*.json"):
        try:
            j = json.loads(jf.read_text()[:2_000_000])
            meta["duration_s"] = meta["duration_s"] or j.get("print_time_sec") or j.get("time_sec")
            meta["filament_mm"] = meta["filament_mm"] or j.get("filament_used_mm")
            meta["filament_g"]  = meta["filament_g"]  or j.get("filament_used_g")
        except Exception:
            pass

    if export_kind == "gcode" and out_g.exists():
        head = out_g.read_text(errors="ignore")[:120000]
        from_hdr = parse_meta_from_gcode(head)
        for k, v in from_hdr.items():
            if v is not None:
                meta[k] = v

    out_file = {"gcode": out_g, "3mf_project": out_3mf, "3mf_sliced": out_3mf}[export_kind]
    if not out_file.exists() or out_file.stat().st_size == 0:
        raise HTTPException(500, "Slicing erfolgreich, aber Ausgabedatei fehlt/leer.")

    return {
        "ok": True,
        "input_ext": ".3mf" if is_3mf else ".stl",
        "export_kind": export_kind,
        "out_size_bytes": out_file.stat().st_size,
        "meta": meta,
        "notes": "Auto-Fix-Prozess aktiv; STL ohne --slice, 3mf_sliced nutzt --slice 0.",
    }
