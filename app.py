# app.py
import os
import io
import json
import hashlib
import tempfile
import subprocess
from typing import Optional, Literal, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel

try:
    import trimesh
except Exception:
    trimesh = None  # Volumenanalyse fällt sonst aus -> Fallbacks

APP_NAME = "Online 3D-Druck Kalkulator (FastAPI)"
ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")

PROFILES_ROOT = "/app/profiles"
PRINTERS_DIR = os.path.join(PROFILES_ROOT, "printers")
PROCESS_DIR  = os.path.join(PROFILES_ROOT, "process")
FILAMENT_DIR = os.path.join(PROFILES_ROOT, "filaments")

MATERIALS = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC": 1.20,
}

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------- Models -----------------------------
class AnalyzeResp(BaseModel):
    model_id: str
    volume_mm3: float
    volume_cm3: float

class WeightDirectReq(BaseModel):
    volume_mm3: float
    material: Literal["PLA", "PETG", "ASA", "PC"]
    infill: float

class WeightDirectResp(BaseModel):
    weight_g: float

# ----------------------------- Utils -----------------------------
def _sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

def _round3(x: float) -> float:
    return float(f"{x:.3f}")

def _safe_json(o: Any) -> str:
    try:
        return json.dumps(o, ensure_ascii=False, indent=2)
    except Exception:
        return str(o)

def _list_profiles() -> Dict[str, list]:
    def _ls(d, exts=(".json", ".ini")):
        if not os.path.isdir(d): return []
        return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(exts)]
    return {
        "printer": _ls(PRINTERS_DIR),
        "process": _ls(PROCESS_DIR),
        "filament": _ls(FILAMENT_DIR),
    }

def _pick_first_or(name_part: Optional[str], files: list) -> Optional[str]:
    if not files: return None
    if name_part:
        for p in files:
            if name_part.lower() in os.path.basename(p).lower():
                return p
    return files[0]

def _slicer_bin_candidates():
    return [
        os.environ.get("ORCA_SLICER_BIN"),
        "/opt/orca/bin/orca-slicer",
        "/usr/local/bin/orca-slicer",
        "/usr/bin/orca-slicer",
    ]

def _find_slicer() -> Optional[str]:
    for p in _slicer_bin_candidates():
        if p and os.path.exists(p):
            return p
    return None

def _run(cmd: list[str], cwd: Optional[str]=None, env: Optional[dict]=None, timeout: Optional[int]=180):
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, text=True,
        )
        return {"code": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
    except subprocess.TimeoutExpired as e:
        return {"code": 124, "stdout": e.stdout or "", "stderr": (e.stderr or "") + "\nTIMEOUT"}

def _orca_help_snippet(slicer_bin: str) -> Dict[str, Any]:
    r = _run([slicer_bin, "--help"])
    return {
        "ok": r["code"] == 0 or r["stdout"] or r["stderr"],
        "bin_exists": os.path.exists(slicer_bin),
        "which": slicer_bin,
        "return_code": r["code"],
        "help_snippet": (r["stdout"] or r["stderr"] or "")[:1200],
    }

# -------- Normalisierung von Profilen (wichtig gegen deine aktuellen Fehler) --------
def _parse_bed_shape(value):
    """
    Akzeptiert:
      - ["0x0","400x0","400x400","0x400"]
      - [[0,0],[400,0],[400,400],[0,400]]
    Liefert float-Paare.
    """
    pts = []
    if isinstance(value, list):
        for p in value:
            if isinstance(p, str) and "x" in p.lower():
                try:
                    x, y = p.lower().split("x", 1)
                    pts.append([float(x), float(y)])
                except Exception:
                    pass
            elif isinstance(p, (list, tuple)) and len(p) == 2:
                try:
                    pts.append([float(p[0]), float(p[1])])
                except Exception:
                    pass
    if len(pts) < 3:
        pts = [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]]
    return pts

def _normalize_machine(pj: dict) -> dict:
    pj = dict(pj or {})
    pj["type"] = "machine"
    pj.setdefault("name", "RatRig V-Core 4 400 0.4 nozzle")
    pj["printer_technology"] = "FFF"
    pj["bed_shape"] = _parse_bed_shape(pj.get("bed_shape"))
    try:
        pj["max_print_height"] = float(pj.get("max_print_height", 300))
    except Exception:
        pj["max_print_height"] = 300.0
    try:
        pj["extruders"] = int(pj.get("extruders", 1))
    except Exception:
        pj["extruders"] = 1
    nd = pj.get("nozzle_diameter", ["0.4"])
    if isinstance(nd, (int, float)): nd = [nd]
    pj["nozzle_diameter"] = [str(x) for x in (nd if isinstance(nd, list) else [nd])]
    # optionale harmlose Defaults (nicht zwingend, aber konsistent)
    pj.setdefault("gcode_flavor", "marlin")
    return pj

def _normalize_process(pr: dict, infill: float) -> dict:
    pr = dict(pr or {})
    pr["type"] = "process"
    pr.setdefault("name", "0.20mm Standard")
    pr["sparse_infill_density"] = f"{int(round(float(infill)*100))}%"
    base = set(map(str, pr.get("compatible_printers", [])))
    base.update({"*","RatRig V-Core 4 400 0.4 nozzle"})
    pr["compatible_printers"] = sorted(base)
    pr["compatible_printers_condition"] = ""
    # druckerspezifische Keys entfernen, damit keine Kompatibilitätsprüfung triggert
    for k in ("printer_model","printer_variant","printer_technology","gcode_flavor"):
        pr.pop(k, None)
    return pr

def _normalize_filament(fj: dict) -> dict:
    fj = dict(fj or {})
    fj["type"] = "filament"
    fj["compatible_printers"] = ["*"]
    fj["compatible_printers_condition"] = ""
    return fj

# ----------------------------- Profile-Setup -----------------------------
def _prepare_profiles_tmp(material: str, infill: float,
                          printer_hint: Optional[str]=None,
                          process_hint: Optional[str]=None) -> dict:
    """
    Wählt Profile aus /app/profiles, normalisiert/härtet sie
    und schreibt sie als JSON in ein Temp-Verzeichnis.
    """
    files = _list_profiles()
    sel_printer  = _pick_first_or(printer_hint, files["printer"])
    sel_process  = _pick_first_or(process_hint, files["process"])
    sel_filament = _pick_first_or(material, files["filament"])

    if not sel_printer:  raise FileNotFoundError("Kein Druckerprofil unter /app/profiles/printers gefunden.")
    if not sel_process:  raise FileNotFoundError("Kein Prozessprofil unter /app/profiles/process gefunden.")
    if not sel_filament: raise FileNotFoundError(f"Kein Filamentprofil mit '{material}' unter /app/profiles/filaments gefunden.")

    tdir = tempfile.mkdtemp(prefix="fixedp_")
    cfg  = os.path.join(tdir, "cfg")
    os.makedirs(cfg, exist_ok=True)
    out  = {
        "tmp": tdir,
        "cfg": cfg,
        "printer_json": os.path.join(tdir, "printer_hardened.json"),
        "process_json": os.path.join(tdir, "process_hardened.json"),
        "filament_json": os.path.join(tdir, "filament_hardened.json"),
        "slicedata_dir": os.path.join(tdir, "slicedata"),
        "out_3mf": os.path.join(tdir, "out.3mf"),
    }
    os.makedirs(out["slicedata_dir"], exist_ok=True)

    def _load_json(path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    try:    pj_raw = _load_json(sel_printer)
    except: pj_raw = {}
    try:    pr_raw = _load_json(sel_process)
    except: pr_raw = {}
    try:    fj_raw = _load_json(sel_filament)
    except: fj_raw = {}

    pj = _normalize_machine(pj_raw)
    pr = _normalize_process(pr_raw, infill=infill)
    fj = _normalize_filament(fj_raw)

    for path, obj in [(out["printer_json"], pj),
                      (out["process_json"], pr),
                      (out["filament_json"], fj)]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    out["printer_snippet"]  = _safe_json(pj)
    out["process_snippet"]  = _safe_json(pr)
    out["filament_snippet"] = _safe_json(fj)
    return out

# ----------------------------- Routes -----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return f"""
<!doctype html><html><head><meta charset="utf-8"><title>{APP_NAME}</title>
<style>body{{font-family:ui-sans-serif,system-ui;max-width:900px;margin:32px auto;line-height:1.45}}
code,pre{{background:#f6f8fa;padding:.2em .4em;border-radius:6px}}
button{{padding:.6em 1em;margin:.3em .2em}}</style></head><body>
<h1>{APP_NAME}</h1>
<p>Quick-Tester für <code>/health</code>, <code>/slicer_env</code> und <code>/slice_check</code>.</p>
<div>
  <button onclick="fetch('/health').then(r=>r.text()).then(alert)">/health</button>
  <button onclick="fetch('/slicer_env').then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">/slicer_env</button>
  <button onclick="location.href='/docs'">Swagger (API)</button>
</div>
<hr/>
<h2>/slice_check</h2>
<form id="f" enctype="multipart/form-data" onsubmit="event.preventDefault(); run()">
  <input type="file" name="file" required />
  <label>unit:
    <select name="unit"><option>mm</option><option>cm</option><option>m</option></select>
  </label>
  <label>material:
    <select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select>
  </label>
  <label>infill: <input name="infill" type="number" step="0.05" value="0.35" /></label>
  <label>printer hint: <input name="printer_hint" placeholder="z.B. X1C" /></label>
  <label>process hint: <input name="process_hint" placeholder="z.B. 0.20mm" /></label>
  <label>arrange: <input name="arrange" type="number" value="1" /></label>
  <label>orient: <input name="orient" type="number" value="1" /></label>
  <button>Slice</button>
</form>
<pre id="out"></pre>
<script>
async function run(){{
  const el = document.getElementById('out');
  el.textContent = 'Running...';
  const fd = new FormData(document.getElementById('f'));
  const r = await fetch('/slice_check', {{method:'POST', body: fd}});
  const t = await r.text();
  el.textContent = t;
}}
</script>
</body></html>
    """

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/slicer_env")
def slicer_env():
    slicer_bin = _find_slicer()
    profiles = _list_profiles()
    info = {
        "ok": True,
        "slicer_bin": slicer_bin or None,
        "slicer_present": bool(slicer_bin),
        "profiles": profiles,
    }
    if slicer_bin:
        info.update(_orca_help_snippet(slicer_bin))
    return JSONResponse(info)

@app.post("/analyze", response_model=AnalyzeResp)
async def analyze(file: UploadFile = File(...), unit: Literal["mm","cm","m"] = Form("mm")):
    raw = await file.read()
    model_id = _sha256_bytes(raw)[:16]
    if trimesh is None:
        return AnalyzeResp(model_id=model_id, volume_mm3=0.0, volume_cm3=0.0)

    mesh = trimesh.load(io.BytesIO(raw), file_type=file.filename.split(".")[-1].lower(), force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) and hasattr(mesh, "dump"):
        mesh = trimesh.util.concatenate(tuple(g for g in mesh.geometry.values()))
    # Reparatur (best effort)
    try:
        trimesh.repair.fix_inversion(mesh)
        # fill_holes ist nicht in allen Builds vorhanden → try/except
        try: trimesh.repair.fill_holes(mesh)
        except Exception: pass
    except Exception:
        pass

    if not getattr(mesh, "is_watertight", False):
        # Fallback Voxelisierung
        vox = mesh.voxelized(pitch=max(mesh.scale/200, 0.5) if getattr(mesh, "scale", None) else 1.0)
        mesh = vox.marching_cubes

    scale = {"mm":1.0,"cm":10.0,"m":1000.0}[unit]
    vol_mm3 = float(mesh.volume * (scale**3))
    return AnalyzeResp(model_id=model_id, volume_mm3=_round3(vol_mm3), volume_cm3=_round3(vol_mm3/1000.0))

@app.post("/weight_direct", response_model=WeightDirectResp)
def weight_direct(req: WeightDirectReq):
    rho = MATERIALS[req.material]  # g/cm³
    vol_cm3 = req.volume_mm3 / 1000.0
    weight = vol_cm3 * rho * max(0.0, min(1.0, req.infill))
    return WeightDirectResp(weight_g=_round3(weight))

@app.post("/analyze_upload")
async def analyze_upload(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 50*1024*1024:
        return JSONResponse({"ok": False, "error": "File too large (>50MB)"}, status_code=413)
    resp = {
        "ok": True,
        "filename": file.filename,
        "filesize_bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "filetype": file.filename.split(".")[-1].lower(),
    }
    if trimesh:
        try:
            mesh = trimesh.load(io.BytesIO(raw), file_type=resp["filetype"], force="mesh")
            if not isinstance(mesh, trimesh.Trimesh) and hasattr(mesh, "dump"):
                mesh = trimesh.util.concatenate(tuple(g for g in mesh.geometry.values()))
            resp["stl"] = {
                "mesh_is_watertight": bool(getattr(mesh, "is_watertight", False)),
                "triangles": int(mesh.faces.shape[0]) if hasattr(mesh, "faces") else None,
                "volume_mm3": float(mesh.volume) if hasattr(mesh, "volume") else None,
                "surface_area_mm2": float(mesh.area) if hasattr(mesh, "area") else None,
            }
        except Exception as e:
            resp["mesh_error"] = str(e)
    return JSONResponse(resp)

@app.post("/estimate_time")
async def estimate_time(
    file: UploadFile = File(...),
    unit: Literal["mm","cm","m"] = Form("mm"),
    layer_height: float = Form(0.2),
    nozzle: float = Form(0.4),
    infill: float = Form(0.35),
    material: Literal["PLA","PETG","ASA","PC"] = Form("PLA"),
):
    """Grobe Zeit-Heuristik ohne OrcaSlicer (Backup)."""
    raw = await file.read()
    if trimesh is None:
        return JSONResponse({"ok": False, "reason": "trimesh unavailable"}, status_code=500)
    mesh = trimesh.load(io.BytesIO(raw), file_type=file.filename.split(".")[-1].lower(), force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) and hasattr(mesh, "dump"):
        mesh = trimesh.util.concatenate(tuple(g for g in mesh.geometry.values()))
    scale = {"mm":1.0,"cm":10.0,"m":1000.0}[unit]
    vol_mm3 = float(mesh.volume * (scale**3))
    q = 8.0 if material=="PLA" else 6.5  # mm³/s (grobe Annahme)
    eff_vol = vol_mm3 * max(0.05, min(1.0, infill+0.20))  # +Perimeter/Solid grob
    secs = eff_vol / q
    return JSONResponse({"ok": True, "estimate_seconds": int(secs), "volume_mm3": _round3(vol_mm3)})

@app.post("/slice_check")
async def slice_check(
    file: UploadFile = File(...),
    unit: Literal["mm","cm","m"] = Form("mm"),
    material: Literal["PLA","PETG","ASA","PC"] = Form("PLA"),
    infill: float = Form(0.35),
    printer_hint: Optional[str] = Form(None),
    process_hint: Optional[str] = Form(None),
    arrange: int = Form(1),  # 0=disable, 1=enable
    orient: int = Form(1),   # 0=disable, 1=enable
    debug: int = Form(1),
):
    """Slicing mit OrcaSlicer-CLI + klare Diagnose bei Fehlern."""
    slicer_bin = _find_slicer()
    if not slicer_bin:
        return JSONResponse({"detail": {"message": "Slicer nicht gefunden", "candidates": _slicer_bin_candidates()}}, status_code=500)

    raw = await file.read()
    t = _prepare_profiles_tmp(material=material, infill=infill, printer_hint=printer_hint, process_hint=process_hint)
    try:
        in_path = os.path.join(t["tmp"], "input.stl")
        with open(in_path, "wb") as f:
            f.write(raw)

        cmd_common = [
            "xvfb-run","-a", slicer_bin,
            "--debug", str(int(debug)),
            "--datadir", t["cfg"],
            "--load-settings", f"{t['printer_json']};{t['process_json']}",
            "--load-filaments", t["filament_json"],
        ]
        if isinstance(arrange, int): cmd_common += ["--arrange", str(arrange)]
        if isinstance(orient, int):  cmd_common += ["--orient",  str(orient)]

        attempts = []

        def _attempt(tag: str, extra_tail: list[str]):
            cmd = cmd_common + extra_tail
            r = _run(cmd)
            attempts.append({
                "tag": tag,
                "cmd": " ".join(cmd),
                "code": r["code"],
                "stderr_tail": r["stderr"][-500:],
                "stdout_tail": r["stdout"][-500:]
            })
            return r

        # Versuch 1
        r1 = _attempt("try-1-join",
            [in_path, "--slice", "1", "--export-3mf", t["out_3mf"], "--export-slicedata", t["slicedata_dir"]])
        if r1["code"] == 0:
            return JSONResponse({"ok": True, "out_3mf": t["out_3mf"], "slicedata_dir": t["slicedata_dir"]})

        # Versuch 2 (gleiche Flags, redundanzhalber)
        r2 = _attempt("try-2-split",
            [in_path, "--slice", "1", "--export-3mf", t["out_3mf"], "--export-slicedata", t["slicedata_dir"]])
        if r2["code"] == 0:
            return JSONResponse({"ok": True, "out_3mf": t["out_3mf"], "slicedata_dir": t["slicedata_dir"]})

        detail = {
            "message": "Slicing fehlgeschlagen (alle Strategien).",
            "attempts": attempts,
            "printer_hardened_json": t["printer_snippet"],
            "process_hardened_json": t["process_snippet"],
            "filament_hardened_json": t["filament_snippet"],
        }
        return JSONResponse({"detail": detail}, status_code=500)

    finally:
        # Für Debug-Zwecke NICHT sofort löschen. Zum Auto-Cleanup ggf. aktivieren:
        # import shutil; shutil.rmtree(t["tmp"], ignore_errors=True)
        pass
