# app.py
import os, io, json, shutil, hashlib, tempfile, subprocess
from typing import Optional, List, Tuple, Dict, Any
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

API_TITLE = "Volume Slicer API"

ORCA_BIN_CANDIDATES = [
    os.environ.get("ORCA_BIN") or "",
    "/opt/orca/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "orca-slicer",
]

MACHINE_FALLBACK_NAME = "Generic 400x400 0.4 nozzle"
MACHINE_FALLBACK = {
    "type": "machine",
    "version": "1",
    "from": "user",
    "name": MACHINE_FALLBACK_NAME,
    "printer_technology": "FFF",
    "gcode_flavor": "marlin",
    "printer_model": "Generic 400x400",
    "printer_variant": "0.4",
    "bed_shape": [[0.0,0.0],[400.0,0.0],[400.0,400.0],[0.0,400.0]],
    "max_print_height": 300.0,
    "min_layer_height": 0.06,
    "max_layer_height": 0.32,
    "extruders": 1,
    "nozzle_diameter": [0.4],
}

def find_orca_bin() -> Optional[str]:
    for c in ORCA_BIN_CANDIDATES:
        if c and (shutil.which(c) or os.path.exists(c)):
            return c
    return shutil.which("orca-slicer")

def run(cmd: List[str], cwd: Optional[str]=None, timeout: int=420):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def write_json(path: str, obj: Dict[str, Any]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

def read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def sha256_bytes(b: bytes) -> str:
    import hashlib; h=hashlib.sha256(); h.update(b); return h.hexdigest()

def tail(s: str, n: int = 1200) -> str:
    return (s or "")[-n:]

def as_float(x, default=None):
    try: return float(x)
    except: return default

def normalize_machine_types(m: Dict[str,Any]) -> Dict[str,Any]:
    # Erzwinge Zahlen/Arrays für problematische Felder
    if isinstance(m.get("max_print_height"), str):
        m["max_print_height"] = as_float(m["max_print_height"], 300.0)
    if isinstance(m.get("min_layer_height"), str):
        m["min_layer_height"] = as_float(m["min_layer_height"], 0.06)
    if isinstance(m.get("max_layer_height"), str):
        m["max_layer_height"] = as_float(m["max_layer_height"], 0.32)
    if isinstance(m.get("extruders"), str):
        try: m["extruders"] = int(m["extruders"])
        except: m["extruders"] = 1
    nd = m.get("nozzle_diameter")
    if isinstance(nd, list):
        m["nozzle_diameter"] = [as_float(nd[0], 0.4)]
    elif isinstance(nd, str):
        m["nozzle_diameter"] = [as_float(nd, 0.4)]
    elif isinstance(nd, (int,float)):
        m["nozzle_diameter"] = [float(nd)]
    else:
        m["nozzle_diameter"] = [0.4]
    # bed_shape muss Liste von Zahlenpaaren sein
    bs = m.get("bed_shape")
    if isinstance(bs, list) and bs and isinstance(bs[0], str):
        pts=[]
        for s in bs:
            if "x" in s:
                a,b=s.split("x",1)
                pts.append([as_float(a,0.0),as_float(b,0.0)])
        if pts: m["bed_shape"]=pts
    return m

def ensure_compat(obj: Dict[str,Any], printer: Dict[str,Any]):
    # sorge dafür, dass der Process/Filament den exakten Printer-Namen kennt
    name = printer.get("name") or MACHINE_FALLBACK_NAME
    cp = obj.get("compatible_printers")
    if not isinstance(cp, list): cp=[]
    if name not in cp: cp.append(name)
    obj["compatible_printers"] = cp
    obj.setdefault("compatible_printers_condition","")
    # Spiegel-Felder helfen älteren Orca-Builds
    obj["printer_technology"] = printer.get("printer_technology","FFF")
    obj["printer_model"]      = printer.get("printer_model","Generic 400x400")
    obj["printer_variant"]    = printer.get("printer_variant","0.4")
    nd = printer.get("nozzle_diameter",[0.4])
    obj["nozzle_diameter"] = [float(nd[0])] if isinstance(nd,list) else [0.4]

def percent_str(p: float) -> str:
    # akzeptiert 0..1 oder 0..100
    if p > 1.0: p = p/100.0
    p = min(max(p,0.0),1.0)
    return f"{round(p*100)}%"

def build_process_json(layer: float, first_layer: float, line_w: float, infill: float) -> Dict[str,Any]:
    return {
        "type":"process","version":"1","from":"user","name":"API process",
        "layer_height": f"{layer}",
        "first_layer_height": f"{first_layer}",
        "sparse_infill_density": percent_str(infill),
        "line_width": f"{line_w}",
        "perimeter_extrusion_width": f"{line_w}",
        "external_perimeter_extrusion_width": f"{line_w}",
        "infill_extrusion_width": f"{line_w}",
        "perimeters":"2","top_solid_layers":"3","bottom_solid_layers":"3",
        "outer_wall_speed":"250","inner_wall_speed":"350","travel_speed":"500",
        "before_layer_gcode":"","layer_gcode":"","toolchange_gcode":"","printing_by_object_gcode":""
    }

def build_filament_json(material: str) -> Dict[str,Any]:
    temps = {
        "PLA": (200,205,0,0),
        "PETG": (240,245,60,60),
        "ASA": (250,255,100,100),
        "PC":  (260,265,110,110),
    }
    tnoz,tnoz0,tbed,tbed0 = temps.get(material.upper(), (200,205,0,0))
    return {
        "type":"filament","version":"1","from":"user","name":f"Generic {material.upper()}",
        "filament_diameter":["1.75"],"filament_density":["1.24"],"filament_flow_ratio":["0.95"],
        "nozzle_temperature":[f"{tnoz}"],"nozzle_temperature_initial_layer":[f"{tnoz0}"],
        "bed_temperature":[f"{tbed}"],"bed_temperature_initial_layer":[f"{tbed0}"],
    }

class SliceParams(BaseModel):
    material: str = Field(default="PLA")
    infill: float = Field(default=0.35, ge=0)
    layer_height: float = Field(default=0.20, gt=0)
    first_layer_height: float = Field(default=0.30, gt=0)
    use_repo_profiles: bool = Field(default=False)
    printer_name: Optional[str] = None
    process_name: Optional[str] = None
    filament_name: Optional[str] = None

# ---------------- FastAPI ----------------
app = FastAPI(title=API_TITLE)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health", response_class=PlainTextResponse)
def health(): return "ok"

@app.get("/slicer_env")
def slicer_env():
    orca = find_orca_bin()
    help_snippet=""
    if orca and os.path.exists(orca):
        _, out, err = run([orca,"--help"])
        help_snippet = tail(out or err, 1600)
    profiles_root="/app/profiles"
    listing={"printer":[], "process":[], "filament":[]}
    for sub in ("printers","process","filaments"):
        d=os.path.join(profiles_root,sub)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith((".json",".ini")):
                    key = "printer" if sub=="printers" else ("process" if sub=="process" else "filament")
                    listing[key].append(os.path.join("/app/profiles",sub,fn))
    return {"ok": True, "slicer_bin": orca, "slicer_present": bool(orca), "help_snippet": help_snippet, "profiles": listing}

def _repo_profile_path(kind: str, name: Optional[str]) -> Optional[str]:
    if not name: return None
    base="/app/profiles"; sub={"printer":"printers","process":"process","filament":"filaments"}[kind]
    return os.path.join(base,sub,name)

def _ensure_exists(path: Optional[str]) -> str:
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"profile not found: {path}")
    return path

def patch_repo_profiles(pr_path: str, pc_path: str, fi_path: str, work: str) -> Dict[str,Any]:
    """Lese Repo-Presets, normalisiere Typen & erzeuge Kompatibilität zum Printer."""
    printer = normalize_machine_types(read_json(pr_path))
    process = read_json(pc_path)
    filament = read_json(fi_path)

    ensure_compat(process, printer)
    ensure_compat(filament, printer)

    # in Arbeitskopie schreiben
    p_prn = os.path.join(work,"printer.json")
    p_pro = os.path.join(work,"process.json")
    p_fil = os.path.join(work,"filament.json")
    write_json(p_prn, printer)
    write_json(p_pro, process)
    write_json(p_fil, filament)
    return {"printer":p_prn, "process":p_pro, "filament":p_fil,
            "previews":{"printer":printer,"process":process,"filament":filament}}

def build_internal_profiles(work: str, material: str, layer: float, first_layer: float, line_w: float):
    printer = normalize_machine_types(MACHINE_FALLBACK.copy())
    process = build_process_json(layer, first_layer, line_w, infill=0.35)
    filament = build_filament_json(material)
    ensure_compat(process, printer)
    ensure_compat(filament, printer)
    p_prn=os.path.join(work,"printer.json"); p_pro=os.path.join(work,"process.json"); p_fil=os.path.join(work,"filament.json")
    write_json(p_prn, printer); write_json(p_pro, process); write_json(p_fil, filament)
    return {"printer":p_prn, "process":p_pro, "filament":p_fil,
            "previews":{"printer":printer,"process":process,"filament":filament}}

def attempt_slice(orca: str, datadir: str, files: Dict[str,str], input_path: str,
                  use_arrange_orient=True, with_checks=True):
    cmd = ["xvfb-run","-a",orca,"--debug","0","--datadir",datadir,
           "--load-settings", f"{files['printer']};{files['process']}",
           "--load-filaments", files["filament"]]
    if use_arrange_orient: cmd += ["--arrange","1","--orient","1"]
    if not with_checks: cmd += ["--no-check"]
    cmd += [input_path, "--slice","1",
            "--export-3mf", os.path.join(os.path.dirname(datadir),"out.3mf"),
            "--export-slicedata", os.path.join(os.path.dirname(datadir),"slicedata")]
    code, out, err = run(cmd, cwd=os.path.dirname(datadir), timeout=420)
    return code, out, err, cmd

@app.post("/slice")
async def slice_endpoint(
    file: UploadFile = File(...),
    material: str = Form("PLA"),
    infill: float = Form(0.35),
    layer_height: float = Form(0.20),
    first_layer_height: float = Form(0.30),
    use_repo_profiles: bool = Form(False),
    printer_name: Optional[str] = Form(None),
    process_name: Optional[str] = Form(None),
    filament_name: Optional[str] = Form(None),
):
    data = await file.read()
    if not data: raise HTTPException(status_code=400, detail="empty file")
    orca = find_orca_bin()
    if not orca: raise HTTPException(status_code=500, detail="orca-slicer not found")

    sha = sha256_bytes(data)
    work = tempfile.mkdtemp(prefix="fixedp_")
    cfg = os.path.join(work,"cfg"); os.makedirs(cfg, exist_ok=True)
    input_path = os.path.join(work,"input.stl")
    with open(input_path,"wb") as f: f.write(data)

    attempts=[]; final_code=1
    try:
        if use_repo_profiles:
            pr=_ensure_exists(_repo_profile_path("printer", printer_name or "X1C.json"))
            pc=_ensure_exists(_repo_profile_path("process", process_name or "0.20mm_standard.json"))
            fi=_ensure_exists(_repo_profile_path("filament", filament_name or f"{material.upper()}.json"))
            patched = patch_repo_profiles(pr, pc, fi, work)
        else:
            patched = build_internal_profiles(work, material, layer_height, first_layer_height, line_w=0.45)

        # Pass 1: normal
        code,out,err,cmd = attempt_slice(orca, cfg, patched, input_path, use_arrange_orient=True, with_checks=True)
        attempts.append({"try":"normal","code":code,"stdout_tail":tail(out),"stderr_tail":tail(err),"cmd":" ".join(cmd),
                         "printer_preview":patched["previews"]["printer"],"process_preview":patched["previews"]["process"],
                         "filament_preview":patched["previews"]["filament"]})
        final_code=code

        # Pass 2: no-check
        if code!=0:
            code,out,err,cmd = attempt_slice(orca, cfg, patched, input_path, use_arrange_orient=True, with_checks=False)
            attempts.append({"try":"no-check","code":code,"stdout_tail":tail(out),"stderr_tail":tail(err),"cmd":" ".join(cmd)})
            final_code=code

        # Pass 3: minimal (ohne arrange/orient)
        if final_code!=0:
            code,out,err,cmd = attempt_slice(orca, cfg, patched, input_path, use_arrange_orient=False, with_checks=False)
            attempts.append({"try":"minimal","code":code,"stdout_tail":tail(out),"stderr_tail":tail(err),"cmd":" ".join(cmd)})
            final_code=code

        meta={"ok": final_code==0, "code": final_code, "sha256": sha, "bytes": len(data), "attempts": attempts}
        if final_code!=0:
            return JSONResponse(status_code=500, content={"detail":{"message":"Slicing fehlgeschlagen.", **meta}})
        return {"ok": True, **meta}

    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><meta charset="utf-8"><title>Volume Slicer API</title>
<style>body{font-family:system-ui,Inter,Segoe UI,Roboto,Arial,sans-serif;max-width:980px;margin:40px auto;padding:0 16px}
section{border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0}button,input,select{padding:10px 14px;border-radius:8px;border:1px solid #ccc}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}pre{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:12px;white-space:pre-wrap}</style>
<h1>Volume Slicer API</h1>
<section><h3>Quick</h3>
<button onclick="fetch('/health').then(r=>r.text()).then(alert)">/health</button>
<button onclick="fetch('/slicer_env').then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">/slicer_env</button>
<a href="/docs" target="_blank"><button>Swagger</button></a></section>
<section><h3>Slice</h3>
<form id="f"><div class="row">
<input type="file" name="file" required />
<select name="material"><option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option></select>
<label><input type="checkbox" name="use_repo_profiles"/> Repo-Profile nutzen</label>
<input type="text" name="printer_name" placeholder="z.B. X1C.json"/>
<input type="text" name="process_name" placeholder="z.B. 0.20mm_standard.json"/>
<input type="text" name="filament_name" placeholder="z.B. PLA.json"/>
<button>Slice</button>
</div></form>
<pre id="out"></pre>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice', {method:'POST', body:fd});
  const t = await r.text();
  document.getElementById('out').textContent = t;
});
</script>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT","8000")), reload=False)
