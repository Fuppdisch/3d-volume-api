# app.py
import os
import io
import json
import shutil
import hashlib
import tempfile
import subprocess
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from string import Template

API_TITLE = "Volume Slicer API"

ORCA_BIN_CANDIDATES = [
    os.environ.get("ORCA_BIN") or "",
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "orca-slicer",
]

app = FastAPI(title=API_TITLE)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ----------------- helpers -----------------
def find_orca_bin() -> Optional[str]:
    for cand in ORCA_BIN_CANDIDATES:
        if cand and (shutil.which(cand) or os.path.exists(cand)):
            return cand
    return shutil.which("orca-slicer")

def run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 420) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", e.stderr or "timeout"
    except Exception as e:
        return 1, "", str(e)

def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

def percent_str(p: float) -> str:
    val = p/100.0 if p > 1.0 else p
    val = min(max(val, 0.0), 1.0)
    return f"{round(val*100)}%"

# ----------------- models -----------------
class SliceParams(BaseModel):
    unit: str = Field(default="mm")
    material: str = Field(default="PLA")
    infill: float = Field(default=0.35, ge=0)
    layer_height: float = Field(default=0.20, gt=0)
    first_layer_height: float = Field(default=0.30, gt=0)
    line_width: float = Field(default=0.45, gt=0)
    perimeters: int = Field(default=2, ge=1)
    top_solid_layers: int = Field(default=3, ge=0)
    bottom_solid_layers: int = Field(default=3, ge=0)
    outer_wall_speed: int = Field(default=250, ge=1)
    inner_wall_speed: int = Field(default=350, ge=1)
    travel_speed: int = Field(default=500, ge=1)
    use_repo_profiles: bool = Field(default=False)
    printer_name: Optional[str] = None
    process_name: Optional[str] = None
    filament_name: Optional[str] = None

    @validator("unit")
    def _unit_ok(cls, v):
        v = (v or "").lower()
        if v not in {"mm", "in", "inch", "inches"}:
            raise ValueError("unit must be mm or in")
        return v

# ----------------- known-good presets -----------------
# WICHTIG: ein konsistenter Maschinenname, der in process/filament als kompatibel referenziert wird
MACHINE_NAME = "Generic 400x400 0.4 nozzle"

KNOWN_GOOD_MACHINE = {
    "type": "machine",
    "version": "1",
    "from": "user",
    "name": MACHINE_NAME,
    "printer_technology": "FFF",
    "gcode_flavor": "marlin",
    "printer_model": "Generic 400x400",
    "printer_variant": "0.4",
    "bed_shape": [[0.0, 0.0], [400.0, 0.0], [400.0, 400.0], [0.0, 400.0]],
    "max_print_height": 300.0,
    "min_layer_height": 0.06,
    "max_layer_height": 0.32,
    "extruders": 1,
    "nozzle_diameter": [0.4],
}

def build_process_json(p: SliceParams) -> dict:
    return {
        "type": "process",
        "version": "1",
        "from": "user",
        "name": "API process",
        "layer_height": f"{p.layer_height}",
        "first_layer_height": f"{p.first_layer_height}",
        "sparse_infill_density": percent_str(p.infill),
        "line_width": f"{p.line_width}",
        "perimeter_extrusion_width": f"{p.line_width}",
        "external_perimeter_extrusion_width": f"{p.line_width}",
        "infill_extrusion_width": f"{p.line_width}",
        "perimeters": f"{p.perimeters}",
        "top_solid_layers": f"{p.top_solid_layers}",
        "bottom_solid_layers": f"{p.bottom_solid_layers}",
        "outer_wall_speed": f"{p.outer_wall_speed}",
        "inner_wall_speed": f"{p.inner_wall_speed}",
        "travel_speed": f"{p.travel_speed}",
        # explizit an die Maschine binden → verhindert "process not compatible with printer"
        "compatible_printers": [MACHINE_NAME],
        "compatible_printers_condition": "",
        "before_layer_gcode": "",
        "layer_gcode": "",
        "toolchange_gcode": "",
        "printing_by_object_gcode": "",
    }

def build_filament_json(material: str) -> dict:
    temps = {
        "PLA": (200, 205, 0, 0),
        "PETG": (240, 245, 60, 60),
        "ASA": (250, 255, 100, 100),
        "PC": (260, 265, 110, 110),
    }
    t_noz, t_noz_first, t_bed, t_bed_first = temps.get(material.upper(), (200, 205, 0, 0))
    return {
        "type": "filament",
        "from": "user",
        "name": f"Generic {material.upper()}",
        "filament_diameter": ["1.75"],
        "filament_density": ["1.24"],
        "filament_flow_ratio": ["0.95"],
        "nozzle_temperature": [f"{t_noz}"],
        "nozzle_temperature_initial_layer": [f"{t_noz_first}"],
        "bed_temperature": [f"{t_bed}"],
        "bed_temperature_initial_layer": [f"{t_bed_first}"],
        # ebenfalls an MACHINE_NAME koppeln
        "compatible_printers": [MACHINE_NAME],
        "compatible_printers_condition": "",
    }

# ----------------- routes -----------------
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/slicer_env")
def slicer_env():
    bin_path = find_orca_bin()
    exists = bool(bin_path and os.path.exists(bin_path))
    help_snippet = ""
    if exists:
        code, out, err = run([bin_path, "--help"])
        help_snippet = (out or err)[:1200]
    # profile listing
    profiles_root = "/app/profiles"
    listing = {"printer": [], "process": [], "filament": []}
    for sub in ("printers", "process", "filaments"):
        subdir = os.path.join(profiles_root, sub)
        if os.path.isdir(subdir):
            for fn in sorted(os.listdir(subdir)):
                if fn.lower().endswith((".json", ".ini")):
                    listing["printer" if sub=="printers" else ("process" if sub=="process" else "filament")]\
                        .append(os.path.join("/app/profiles", sub, fn))
    return {
        "ok": True,
        "slicer_bin": bin_path,
        "slicer_present": exists,
        "help_snippet": help_snippet,
        "profiles": listing,
    }

@app.get("/", response_class=HTMLResponse)
def index():
    tpl = Template("""<!doctype html><html lang="de"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>$TITLE</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Inter,Arial,sans-serif;max-width:980px;margin:40px auto;padding:0 16px}
section{border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0}
button,input,select{padding:10px 14px;border-radius:8px;border:1px solid #ccc}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
pre{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:12px;white-space:pre-wrap}
</style></head><body>
<h1>$TITLE</h1>
<section><h3>Quick</h3>
<button onclick="fetch('/health').then(r=>r.text()).then(alert)">/health</button>
<button onclick="fetch('/slicer_env').then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">/slicer_env</button>
<a href="/docs" target="_blank"><button>Swagger</button></a>
</section>
<section><h3>Slice</h3>
<form id="f">
  <div class="row">
    <input type="file" name="file" required />
    <select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select>
    <input type="number" step="0.01" name="infill" value="0.35"/>
    <input type="number" step="0.01" name="layer_height" value="0.2"/>
    <input type="number" step="0.01" name="first_layer_height" value="0.3"/>
    <label><input type="checkbox" name="use_repo_profiles"/> Repo-Profile nutzen</label>
  </div>
  <div class="row">
    <input type="text" name="printer_name" placeholder="z.B. X1C.json"/>
    <input type="text" name="process_name" placeholder="z.B. 0.20mm_standard.json"/>
    <input type="text" name="filament_name" placeholder="z.B. PLA.json"/>
  </div>
  <div class="row" style="margin-top:8px"><button>Slice</button></div>
</form>
<pre id="out"></pre>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice', {method:'POST', body:fd});
  document.getElementById('out').textContent = await r.text();
});
</script>
</section></body></html>""")
    return tpl.substitute(TITLE=API_TITLE)

def _repo_profile_path(kind: str, name: Optional[str]) -> Optional[str]:
    if not name: return None
    base = "/app/profiles"
    sub = {"printer":"printers","process":"process","filament":"filaments"}[kind]
    return os.path.join(base, sub, name)

def _ensure_repo_profile(path: Optional[str]) -> str:
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"profile not found: {path}")
    return path

@app.post("/slice")
async def slice_endpoint(
    file: UploadFile = File(...),
    unit: str = Form("mm"),
    material: str = Form("PLA"),
    infill: float = Form(0.35),
    layer_height: float = Form(0.20),
    first_layer_height: float = Form(0.30),
    use_repo_profiles: bool = Form(False),
    printer_name: Optional[str] = Form(None),
    process_name: Optional[str] = Form(None),
    filament_name: Optional[str] = Form(None),
):
    try:
        params = SliceParams(
            unit=unit, material=material, infill=infill,
            layer_height=layer_height, first_layer_height=first_layer_height,
            use_repo_profiles=use_repo_profiles,
            printer_name=printer_name, process_name=process_name, filament_name=filament_name,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid params: {e}")

    orca = find_orca_bin()
    if not orca or not os.path.exists(orca):
        raise HTTPException(status_code=500, detail="orca-slicer not found")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    sha = sha256_bytes(data)

    work = tempfile.mkdtemp(prefix="fixedp_")
    try:
        input_path = os.path.join(work, "input.stl")
        with open(input_path, "wb") as f:
            f.write(data)

        cfg_dir = os.path.join(work, "cfg")
        slicedata_dir = os.path.join(work, "slicedata")
        os.makedirs(cfg_dir, exist_ok=True)
        os.makedirs(slicedata_dir, exist_ok=True)

        # prepare profiles
        if params.use_repo_profiles:
            p_prn = _ensure_repo_profile(_repo_profile_path("printer", params.printer_name or "X1C.json"))
            p_pro = _ensure_repo_profile(_repo_profile_path("process", params.process_name or "0.20mm_standard.json"))
            p_fil = _ensure_repo_profile(_repo_profile_path("filament", params.filament_name or f"{material.upper()}.json"))
        else:
            p_prn = os.path.join(work, "printer.json")
            p_pro = os.path.join(work, "process.json")
            p_fil = os.path.join(work, "filament.json")
            # Machine
            write_text(p_prn, json.dumps(KNOWN_GOOD_MACHINE, ensure_ascii=False))
            # Process & Filament EXPLIZIT an MACHINE_NAME koppeln
            proc_json = build_process_json(params)
            proc_json["compatible_printers"] = [MACHINE_NAME]
            write_text(p_pro, json.dumps(proc_json, ensure_ascii=False))
            fil_json = build_filament_json(material)
            fil_json["compatible_printers"] = [MACHINE_NAME]
            write_text(p_fil, json.dumps(fil_json, ensure_ascii=False))

        cmd = [
            "xvfb-run", "-a", orca,
            "--debug", "0",
            "--datadir", cfg_dir,
            "--load-settings", f"{p_prn};{p_pro}",
            "--load-filaments", p_fil,
            "--arrange", "1",
            "--orient", "1",
            input_path,
            "--slice", "1",
            "--export-3mf", os.path.join(work, "out.3mf"),
            "--export-slicedata", slicedata_dir,
        ]
        code, out, err = run(cmd, cwd=work, timeout=420)

        meta = {
            "ok": code == 0,
            "code": code,
            "cmd": " ".join(cmd),
            "sha256": sha,
            "bytes": len(data),
            "stdout_tail": (out or "")[-900:],
            "stderr_tail": (err or "")[-900:],
            "profiles_used": {"printer": p_prn, "process": p_pro, "filament": p_fil},
        }

        if code != 0:
            # zusätzliche Debug-Hilfen: Profile zurückgeben (nur bei temp erzeugten)
            dbg = {}
            if not params.use_repo_profiles:
                try:
                    dbg["printer_preview"] = json.loads(open(p_prn, "r", encoding="utf-8").read())
                except Exception:
                    pass
                try:
                    dbg["process_preview"] = json.loads(open(p_pro, "r", encoding="utf-8").read())
                except Exception:
                    pass
                try:
                    dbg["filament_preview"] = json.loads(open(p_fil, "r", encoding="utf-8").read())
                except Exception:
                    pass
            return JSONResponse(status_code=500, content={"detail": {"message": "Slicing fehlgeschlagen.", **meta, **({"profile_debug": dbg} if dbg else {})}})

        # success (in echten Systemen: Dateien persistieren/ausliefern)
        meta["result"] = {
            "three_mf_temp_path": os.path.join(work, "out.3mf"),
            "slicedata_temp_dir": slicedata_dir,
        }
        return {"ok": True, **meta}

    finally:
        # Für Debugging kann man das Löschen auskommentieren
        shutil.rmtree(work, ignore_errors=True)

# --------------- uvicorn entry ---------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
