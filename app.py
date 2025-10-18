# app.py
import os
import io
import json
import math
import tempfile
import shutil
import hashlib
import subprocess
from typing import List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========== Konfiguration ==========
API_TITLE = "Online 3D-Druck Kalkulator + Slicer"
ORCA_BIN_CANDIDATES = ["/opt/orca/bin/orca-slicer", "/usr/local/bin/orca-slicer"]
XVFB = os.environ.get("XVFB_BIN", "/usr/bin/xvfb-run")

PROFILES_BASE = "/app/profiles"
PRINTERS_DIR = os.path.join(PROFILES_BASE, "printers")
PROCESS_DIR = os.path.join(PROFILES_BASE, "process")
FILAMENT_DIR = os.path.join(PROFILES_BASE, "filaments")

ALLOWED_MATERIALS = {
    "PLA": 1.25,
    "PETG": 1.26,
    "ASA": 1.08,
    "PC": 1.20,
}

# ========== FastAPI ==========
app = FastAPI(title=API_TITLE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # später auf deine Domain einschränken
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== Hilfsfunktionen Profile ==========
def _first_file_or_named(dir_path: str, name_opt: Optional[str], exts=(".json", ".ini")) -> str:
    """
    Liefert eine Datei aus dem Ordner, entweder exakt benannt (name_opt)
    oder den ersten Treffer nach Alphabet.
    """
    if name_opt:
        cand = os.path.join(dir_path, name_opt)
        if os.path.isfile(cand):
            return cand
        # tolerant: ohne Pfadbestandteile nur Name matchen
        for f in sorted(os.listdir(dir_path)):
            if f.lower() == name_opt.lower():
                return os.path.join(dir_path, f)
    # erster Treffer
    for f in sorted(os.listdir(dir_path)):
        if f.lower().endswith(exts):
            return os.path.join(dir_path, f)
    raise FileNotFoundError(f"Keine Profil-Datei in {dir_path} gefunden.")

def _to_float(x):
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except:
            pass
    return x

def _parse_bed_shape(val) -> List[List[float]]:
    """
    akzeptiert:
      - [[0,0],[400,0],[400,400],[0,400]]
      - ["0x0","400x0","400x400","0x400"]
    """
    if isinstance(val, list) and len(val) > 0:
        if isinstance(val[0], list):
            # bereits zahlen
            return [[_to_float(a), _to_float(b)] for a, b in val]
        if isinstance(val[0], str):
            out = []
            for s in val:
                if "x" in s:
                    a, b = s.split("x", 1)
                    out.append([float(a), float(b)])
            if out:
                return out
    # fallback: Standard 220x220
    return [[0.0, 0.0], [220.0, 0.0], [220.0, 220.0], [0.0, 220.0]]

def _force_float_list(arr) -> List[float]:
    out = []
    if isinstance(arr, list):
        for v in arr:
            out.append(_to_float(v))
    else:
        out = [_to_float(arr)]
    return out

def _harden_machine_json(j: dict) -> dict:
    # Minimales erwartetes Schema für Orca
    out = {}
    out["type"] = "machine"
    out["version"] = "1"
    out["from"] = j.get("from", "user")
    out["name"] = j.get("name") or j.get("printer_name") or "Custom FFF 0.4 nozzle"
    out["printer_technology"] = j.get("printer_technology", "FFF")
    out["gcode_flavor"] = j.get("gcode_flavor", "marlin")

    # Geometrie
    bed = j.get("bed_shape") or j.get("printable_area")
    out["bed_shape"] = _parse_bed_shape(bed)
    out["max_print_height"] = _to_float(j.get("max_print_height") or j.get("printable_height") or 200.0)

    # Schichtgrenzen
    out["min_layer_height"] = _to_float(j.get("min_layer_height", 0.06))
    out["max_layer_height"] = _to_float(j.get("max_layer_height", 0.3))

    # Extruder / Düse
    extruders = j.get("extruders", 1)
    try:
        extruders = int(extruders)
    except:
        extruders = 1
    out["extruders"] = extruders

    nd = j.get("nozzle_diameter", [0.4])
    nd = _force_float_list(nd)
    out["nozzle_diameter"] = nd

    # Optional: Modell/Variante übernehmen, aber rein informativ
    if "printer_model" in j:
        out["printer_model"] = j["printer_model"]
    if "printer_variant" in j:
        out["printer_variant"] = j["printer_variant"]

    return out

def _harden_process_json(j: dict, machine_name: str) -> dict:
    out = {}
    out["type"] = "process"
    out["version"] = "1"
    out["from"] = j.get("from", "user")
    out["name"] = j.get("name", "0.20mm Standard")
    # layer heights
    out["layer_height"] = str(j.get("layer_height", j.get("initial_layer_height", "0.2")))
    out["first_layer_height"] = str(j.get("first_layer_height", j.get("initial_layer_height", "0.3")))
    # extrusion widths / speeds
    if "line_width" in j:
        out["line_width"] = str(j["line_width"])
    for k_src, k_dst in [
        ("perimeter_extrusion_width", "perimeter_extrusion_width"),
        ("external_perimeter_extrusion_width", "external_perimeter_extrusion_width"),
        ("infill_extrusion_width", "infill_extrusion_width"),
    ]:
        if k_src in j:
            out[k_dst] = str(j[k_src])

    # Dichten
    # akzeptiere "35%" oder 0.35 / "0.35"
    s_inf = j.get("sparse_infill_density") or j.get("fill_density") or "25%"
    if isinstance(s_inf, (int, float)):
        val = f"{int(round(float(s_inf) * 100))}%"
    else:
        s = str(s_inf).strip()
        if s.endswith("%"):
            val = s
        else:
            try:
                f = float(s)
                if f <= 1.0:
                    val = f"{int(round(f*100))}%"
                else:
                    val = f"{int(round(f))}%"
            except:
                val = "25%"
    out["sparse_infill_density"] = val

    # Standard Perimeter / Solid
    out["perimeters"] = str(j.get("perimeters", "2"))
    out["top_solid_layers"] = str(j.get("top_solid_layers", j.get("solid_layers", "3")))
    out["bottom_solid_layers"] = str(j.get("bottom_solid_layers", j.get("solid_layers", "3")))

    # Geschwindigkeiten (optional)
    for k in ["outer_wall_speed", "inner_wall_speed", "travel_speed",
              "perimeter_speed", "external_perimeter_speed", "infill_speed"]:
        if k in j:
            out[k] = str(j[k])

    # G-Code Felder (leer lassen, um Kompatibilität nicht zu stören)
    out["before_layer_gcode"] = j.get("before_layer_gcode", "")
    out["layer_gcode"] = j.get("layer_gcode", "")
    out["toolchange_gcode"] = j.get("toolchange_gcode", "")
    out["printing_by_object_gcode"] = j.get("printing_by_object_gcode", "")

    # Kompatibilität breit öffnen
    out["compatible_printers"] = ["*", machine_name]
    out["compatible_printers_condition"] = ""

    return out

def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _pick_filament(material: str) -> str:
    # bevorzugt exakt passende Datei im filaments/
    target = material.upper()
    files = sorted(os.listdir(FILAMENT_DIR))
    for f in files:
        base = os.path.splitext(f)[0].upper()
        if target in base:
            return os.path.join(FILAMENT_DIR, f)
    # fallback: PLA.json wenn vorhanden
    pla = os.path.join(FILAMENT_DIR, "PLA.json")
    if os.path.isfile(pla):
        return pla
    # sonst erste
    return _first_file_or_named(FILAMENT_DIR, None)

def _which_orca() -> Optional[str]:
    for p in ORCA_BIN_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

def _tail(s: str, n: int = 800) -> str:
    if s is None:
        return ""
    s = s.strip()
    return s[-n:]

# ========== STL-Analyse ==========
try:
    import trimesh
    import numpy as np
except Exception:
    trimesh = None
    np = None

def _safe_load_mesh(data: bytes, unit: str) -> Tuple[float, float, dict]:
    if trimesh is None:
        raise HTTPException(500, detail="trimesh nicht installiert")
    m = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    # Reparatur
    try:
        m.remove_duplicate_faces()
    except:
        pass
    try:
        m.remove_degenerate_faces()
    except:
        pass
    try:
        m.fill_holes()  # falls vorhanden; wenn nicht, ignorieren
    except:
        pass
    if not m.is_watertight:
        # Fallback: Voxel
        v = m.voxelized(pitch=max(m.scale / 100, 0.5))
        m = v.as_boxes()

    # Unit-Scale
    u = unit.lower()
    scale = 1.0
    if u == "cm":
        scale = 10.0
    elif u == "m":
        scale = 1000.0
    # volume in mm^3
    vol_mm3 = float(m.volume) * (scale ** 3)
    vol_cm3 = vol_mm3 / 1000.0

    info = {
        "mesh_is_watertight": bool(m.is_watertight),
        "triangles": int(len(m.faces)) if hasattr(m, "faces") else None,
        "bbox_size_mm": list((np.array(m.bounding_box.extents) * scale).tolist()) if np is not None else None
    }
    return vol_mm3, vol_cm3, info

# ========== Models ==========
class WeightRequest(BaseModel):
    volume_mm3: float
    material: str
    infill: float

# ========== Routes ==========
@app.get("/", response_class=HTMLResponse)
def index():
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{API_TITLE}</title>
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;max-width:900px;margin:40px auto;padding:0 16px}}
  h1{{font-size:22px;margin:0 0 8px}}
  section{{border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0}}
  button,input[type=file]{{padding:10px 14px;border-radius:8px;border:1px solid #ccc}}
  .row{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
  code,pre{{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:8px;display:block;white-space:pre-wrap}}
</style>
</head>
<body>
<h1>{API_TITLE}</h1>

<section>
  <h3>Quick Checks</h3>
  <div class="row">
    <button onclick="fetch('/health').then(r=>r.text()).then(alert)">/health</button>
    <button onclick="fetch('/slicer_env').then(r=>r.json()).then(x=>alert(JSON.stringify(x,null,2)))">/slicer_env</button>
    <a href="/docs" target="_blank"><button>Swagger</button></a>
  </div>
</section>

<section>
  <h3>Slice Test</h3>
  <form id="sliceForm">
    <div class="row">
      <input type="file" name="file" required />
      <select name="material">
        <option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option>
      </select>
      <input type="number" name="infill" step="0.01" min="0" max="1" value="0.35" />
      <input type="text" name="printer_name" placeholder="optional: X1C.json" />
      <input type="text" name="process_name" placeholder="optional: 0.20mm_standard.json" />
      <input type="text" name="filament_name" placeholder="optional: PLA.json" />
    </div>
    <div class="row" style="margin-top:8px">
      <button>Slice</button>
    </div>
  </form>
  <pre id="out"></pre>
</section>

<script>
document.getElementById('sliceForm').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/slice', {{ method:'POST', body: fd }});
  const t = await r.text();
  document.getElementById('out').textContent = t;
});
</script>
</body>
</html>
"""

@app.get("/health")
def health():
    return PlainTextResponse("ok")

@app.get("/slicer_env")
def slicer_env():
    which = _which_orca()
    help_snip = ""
    rc = None
    if which:
        try:
            cp = subprocess.run([which, "--help"], capture_output=True, text=True, timeout=10)
            help_snip = (cp.stdout or cp.stderr or "")[:1200]
            rc = cp.returncode
        except Exception as e:
            help_snip = f"help failed: {e}"
    profiles = {
        "printer": [os.path.join(PRINTERS_DIR, f) for f in sorted(os.listdir(PRINTERS_DIR)) if f.lower().endswith(".json")],
        "process": [os.path.join(PROCESS_DIR, f) for f in sorted(os.listdir(PROCESS_DIR)) if f.lower().endswith(".json")],
        "filament": [os.path.join(FILAMENT_DIR, f) for f in sorted(os.listdir(FILAMENT_DIR)) if f.lower().endswith(".json")],
    }
    return JSONResponse({
        "ok": True,
        "slicer_bin": which,
        "slicer_present": bool(which),
        "help_snippet": help_snip,
        "profiles": profiles
    })

@app.post("/analyze")
async def analyze(file: UploadFile = File(...), unit: str = Form("mm")):
    data = await file.read()
    vol_mm3, vol_cm3, info = _safe_load_mesh(data, unit)
    model_id = hashlib.sha256(data).hexdigest()[:16]
    return {
        "model_id": model_id,
        "volume_mm3": round(vol_mm3, 3),
        "volume_cm3": round(vol_cm3, 3),
        "stl": info
    }

@app.post("/weight_direct")
def weight_direct(req: WeightRequest):
    mat = req.material.upper()
    rho = ALLOWED_MATERIALS.get(mat)
    if rho is None:
        raise HTTPException(400, detail=f"Unbekanntes Material '{req.material}'. Erlaubt: {list(ALLOWED_MATERIALS.keys())}")
    vol_cm3 = req.volume_mm3 / 1000.0
    # Infill linear berücksichtigen (einfaches Modell: Hülle ~ vernachlässigt hier)
    mass_g = vol_cm3 * rho * float(req.infill)
    return {"weight_g": round(mass_g, 3)}

@app.post("/slice")
async def slice_route(
    file: UploadFile = File(...),
    material: str = Form("PLA"),
    infill: float = Form(0.35),
    arrange: int = Form(1),
    orient: int = Form(1),
    debug: int = Form(0),
    printer_name: Optional[str] = Form(None),
    process_name: Optional[str] = Form(None),
    filament_name: Optional[str] = Form(None),
):
    """
    Nimmt STL & feste Profile, normalisiert Profile (Typen!), führt Orca-Slicer aus.
    """
    orca = _which_orca()
    if not orca:
        raise HTTPException(500, detail="orca-slicer nicht gefunden (erwartet /opt/orca/bin/orca-slicer)")

    try:
        # Eingangsdatei in Temp
        tmp = tempfile.mkdtemp(prefix="fixedp_")
        input_path = os.path.join(tmp, "input_model.stl")
        with open(input_path, "wb") as f:
            f.write(await file.read())

        # Profile laden
        printer_path = _first_file_or_named(PRINTERS_DIR, printer_name)
        process_path = _first_file_or_named(PROCESS_DIR, process_name)
        if filament_name:
            filament_path = _first_file_or_named(FILAMENT_DIR, filament_name)
        else:
            filament_path = _pick_filament(material)

        # JSON einlesen
        prn_raw = _read_json(printer_path)
        proc_raw = _read_json(process_path)

        # härten
        prn = _harden_machine_json(prn_raw)
        proc = _harden_process_json(proc_raw, prn["name"])

        # Infill überschreiben (z. B. 0.35 → "35%")
        inf_pct = f"{int(round(float(infill) * 100))}%"
        proc["sparse_infill_density"] = inf_pct

        # persistieren
        prn_json = os.path.join(tmp, "printer.json")
        proc_json = os.path.join(tmp, "process.json")
        with open(prn_json, "w", encoding="utf-8") as f:
            json.dump(prn, f, ensure_ascii=False)
        with open(proc_json, "w", encoding="utf-8") as f:
            json.dump(proc, f, ensure_ascii=False)

        # Ausgabepfade
        out_dir = os.path.join(tmp, "slicedata")
        os.makedirs(out_dir, exist_ok=True)
        out_3mf = os.path.join(tmp, "out.3mf")

        # CLI bauen
        cmd = [
            XVFB, "-a",
            orca, "--debug", str(debug),
            "--datadir", os.path.join(tmp, "cfg"),
            "--load-settings", prn_json,
            "--load-settings", proc_json,
            "--load-filaments", filament_path,
            "--arrange", str(arrange),
            "--orient", str(orient),
            input_path,
            "--slice", "1",
            "--export-3mf", out_3mf,
            "--export-slicedata", out_dir,
        ]

        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        if cp.returncode != 0:
            # typische -17 = process not compatible with printer
            detail = {
                "message": "Slicing fehlgeschlagen.",
                "cmd": " ".join(cmd),
                "code": cp.returncode,
                "stdout_tail": _tail(cp.stdout),
                "stderr_tail": _tail(cp.stderr),
                "printer_hardened_json": prn,
                "process_hardened_json": proc,
                "filament_used": filament_path
            }
            raise HTTPException(status_code=500, detail=detail)

        # Erfolg → Minimale Rückgabe
        result = {
            "ok": True,
            "out_3mf_exists": os.path.isfile(out_3mf),
            "slicedata_dir": out_dir,
            "cmd": " ".join(cmd),
            "printer_used": os.path.basename(printer_path),
            "process_used": os.path.basename(process_path),
            "filament_used": os.path.basename(filament_path),
        }
        return JSONResponse(result)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail={"message": f"Slice-Exception: {e}"})
    finally:
        # Temp-Ordner bewusst nicht löschen → erleichtert Debug in Render (stdout ansehbar)
        pass
