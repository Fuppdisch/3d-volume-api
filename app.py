# ---------- app.py ----------
import os
import io
import re
import json
import math
import time
import hashlib
import zipfile
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Optional: für 3MF-XML
import xml.etree.ElementTree as ET

# Für STL-Analyse
import trimesh
import numpy as np

app = FastAPI(title="3D Print – Upload, Time & Cost API")

# --- CORS ---------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# --- ENV / Slicer Info --------------------------------------------------------
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

# --- Limits & Defaults --------------------------------------------------------
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
XVFB = shutil.which("xvfb-run") or "xvfb-run"

# Materialdichten (g/cm^3) – konservativ; kann später via Config überschrieben werden
MATERIAL_DENSITY_G_CM3 = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ASA": 1.07,
    "PC": 1.20,
}
FILAMENT_DIAMETER_MM = 1.75  # typisch

# --- Helpers ------------------------------------------------------------------
def sha256_of_bytes(buf: bytes) -> str:
    h = hashlib.sha256(); h.update(buf); return h.hexdigest()

def slicer_exists() -> bool:
    return (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK)) or \
           (shutil.which(os.path.basename(SLICER_BIN)) is not None)

def run(cmd: List[str], timeout: int = 900) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def np_to_list(x) -> List[float]:
    return list(map(float, x))

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# ---- STL Analyse -------------------------------------------------------------
def analyze_stl(data: bytes) -> Dict[str, Any]:
    mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(tuple(
            g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)
        ))
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("STL enthält keine gültige Geometrie.")

    mesh.remove_unreferenced_vertices()
    mesh.process(validate=True)

    tri_count = int(mesh.faces.shape[0])
    volume_mm3 = float(abs(mesh.volume)) if mesh.is_volume else None
    area_mm2 = float(mesh.area) if mesh.area is not None else None
    bounds = mesh.bounds
    size = bounds[1] - bounds[0]

    return {
        "mesh_is_watertight": bool(mesh.is_watertight),
        "triangles": tri_count,
        "volume_mm3": volume_mm3,
        "surface_area_mm2": area_mm2,
        "bbox_min_mm": np_to_list(bounds[0]),
        "bbox_max_mm": np_to_list(bounds[1]),
        "bbox_size_mm": np_to_list(size),
        "units_assumed": "mm",
    }

# ---- 3MF Analyse -------------------------------------------------------------
def analyze_3mf(data: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        namelist = zf.namelist()
        model_xml_name = None
        for cand in ("3D/3dmodel.model", "3d/3dmodel.model", "3D/Model.model"):
            if cand in namelist:
                model_xml_name = cand
                break

        res: Dict[str, Any] = {
            "zip_entries": len(namelist),
            "has_model_xml": bool(model_xml_name),
            "objects": [],
            "units": None,
            "triangles_total": 0,
            "objects_count": 0
        }
        if not model_xml_name:
            return res

        xml_bytes = zf.read(model_xml_name)
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return res

        ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        units = root.attrib.get("unit") or root.attrib.get("units")
        res["units"] = units or "mm"

        objects = root.findall(".//m:object", ns) if ns else root.findall(".//object")
        res["objects_count"] = len(objects)

        tri_total = 0; out_objects = []
        for obj in objects:
            oid = obj.attrib.get("id"); typ = obj.attrib.get("type")
            mesh_elem = obj.find("m:mesh", ns) if ns else obj.find("mesh")
            if mesh_elem is None:
                out_objects.append({"id": oid, "type": typ, "triangles": 0}); continue

            tris = mesh_elem.find("m:triangles", ns) if ns else mesh_elem.find("triangles")
            tri_count = 0
            if tris is not None:
                tri_count = len(tris.findall("m:triangle", ns) if ns else tris.findall("triangle"))
            tri_total += tri_count

            bbox = None
            verts_elem = mesh_elem.find("m:vertices", ns) if ns else mesh_elem.find("vertices")
            if verts_elem is not None:
                vs = (verts_elem.findall("m:vertex", ns) if ns else verts_elem.findall("vertex"))
                coords = []
                for v in vs:
                    try:
                        x = float(v.attrib.get("x", "0")); y = float(v.attrib.get("y", "0")); z = float(v.attrib.get("z", "0"))
                        coords.append((x, y, z))
                    except Exception:
                        pass
                if coords:
                    arr = np.array(coords, dtype=float)
                    vmin = arr.min(axis=0); vmax = arr.max(axis=0)
                    bbox = {"bbox_min": np_to_list(vmin), "bbox_max": np_to_list(vmax), "bbox_size": np_to_list(vmax - vmin)}

            out_objects.append({"id": oid, "type": typ, "triangles": tri_count, **({"bbox": bbox} if bbox else {})})

        res["triangles_total"] = tri_total
        res["objects"] = out_objects
        return res

# ---- Slicedata Parser --------------------------------------------------------
def parse_slicedata_folder(folder: Path) -> Dict[str, Any]:
    out = {"duration_s": None, "filament_mm": None, "filament_g": None, "files": []}
    for jf in sorted(folder.glob("*.json")):
        try:
            j = json.loads(jf.read_text()[:2_000_000])
            out["duration_s"] = out["duration_s"] or j.get("print_time_sec") or j.get("time_sec")
            out["filament_mm"] = out["filament_mm"] or j.get("filament_used_mm") or j.get("filament_mm")
            out["filament_g"]  = out["filament_g"]  or j.get("filament_used_g")  or j.get("filament_g")
            out["files"].append(jf.name)
        except Exception:
            pass
    return out

# ---- Filament mm → g ---------------------------------------------------------
def filament_mm_to_g(filament_mm: float, material: str = "PLA") -> Optional[float]:
    if filament_mm is None:
        return None
    mat = (material or "PLA").upper()
    rho = MATERIAL_DENSITY_G_CM3.get(mat, MATERIAL_DENSITY_G_CM3["PLA"])  # g/cm3
    d_mm = FILAMENT_DIAMETER_MM
    area_mm2 = math.pi * (d_mm / 2.0) ** 2
    vol_mm3 = filament_mm * area_mm2                      # mm3
    vol_cm3 = vol_mm3 / 1000.0                            # 1000 mm3 = 1 cm3
    grams = vol_cm3 * rho
    return grams

# --- Mini UI ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<meta charset="utf-8">
<title>3D Print – Upload, Zeit & Kosten</title>
<style>
  :root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
  body{font-family:system-ui,Segoe UI,Arial;margin:24px;line-height:1.45;color:var(--fg)}
  h1{margin:0 0 16px;font-size:22px}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px;background:#fff}
  button,input[type=submit]{padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#111827;color:#fff;cursor:pointer}
  button.secondary{background:#fff;color:#111827}
  label{font-weight:600;margin-right:8px}
  input[type=number],select{padding:8px 10px;border:1px solid var(--line);border-radius:10px}
  pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:360px;overflow:auto}
  small{color:var(--muted)}
</style>

<h1>3D Print – Upload, Zeit & Kosten</h1>
<div class="grid">
  <div class="card">
    <button class="secondary" onclick="openDocs()">Swagger (API-Doku)</button>
    <button onclick="hit('/health','#out')">Health</button>
    <button onclick="hit('/slicer_env','#out')">Slicer-Env</button>
  </div>

  <div class="card">
    <h3>Upload analysieren (/analyze_upload)</h3>
    <form onsubmit="return sendAnalyze(event)">
      <input type="file" name="file" accept=".stl,.3mf" required>
      <input type="submit" value="Analysieren">
      <div><small>bis 50 MB</small></div>
    </form>
  </div>

  <div class="card">
    <h3>Druckzeit schätzen (/estimate_time)</h3>
    <form onsubmit="return sendTime(event)">
      <input type="file" name="file" accept=".stl,.3mf" required>
      <select name="force_slice">
        <option value="auto" selected>auto (fallback mit --slice 0)</option>
        <option value="always">immer --slice 0</option>
      </select>
      <input type="submit" value="Zeit ermitteln">
      <div><small>Output: duration_s, filament_mm/g (wenn vorhanden)</small></div>
    </form>
  </div>

  <div class="card">
    <h3>Kosten schätzen (/estimate_cost)</h3>
    <form onsubmit="return sendCost(event)">
      <label>Dauer (s)</label><input type="number" name="duration_s" step="1" required><br><br>
      <label>Material</label>
      <select name="material">
        <option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option>
      </select>
      <label>filament_g</label><input type="number" name="filament_g" step="0.01">
      <label>oder filament_mm</label><input type="number" name="filament_mm" step="1">
      <br><br>
      <label>€/h Maschine</label><input type="number" name="machine_rate_eur_h" step="0.01" value="6.00">
      <label>€/kg Material</label><input type="number" name="material_eur_kg" step="0.01" value="22.00"><br><br>
      <label>kWh/h</label><input type="number" name="energy_kwh_per_h" step="0.01" value="0.10">
      <label>€/kWh</label><input type="number" name="energy_eur_per_kwh" step="0.01" value="0.35"><br><br>
      <label>Setup €</label><input type="number" name="setup_fee_eur" step="0.01" value="0.00">
      <label>Risiko %</label><input type="number" name="risk_pct" step="0.1" value="0">
      <br><br><input type="submit" value="Kosten berechnen">
    </form>
  </div>
</div>

<pre id="out">Output erscheint hier …</pre>

<script>
const base = location.origin;

async function hit(path, sel){
  const out = document.querySelector(sel || '#out');
  out.textContent = 'Lade ' + path + ' …';
  try{
    const r = await fetch(base + path);
    const isJson = (r.headers.get('content-type')||'').includes('json');
    out.textContent = isJson ? JSON.stringify(await r.json(), null, 2) : await r.text();
  }catch(e){ out.textContent = 'Fehler: ' + e; }
}

function formToJSON(form){
  const fd = new FormData(form); const obj = {};
  for (const [k,v] of fd.entries()){ obj[k]=v; }
  return obj;
}

async function sendAnalyze(e){
  e.preventDefault(); const fd = new FormData(e.target);
  const out = document.querySelector('#out'); out.textContent='Analysiere …';
  try{ const r = await fetch(base+'/analyze_upload',{method:'POST',body:fd});
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}

async function sendTime(e){
  e.preventDefault(); const fd = new FormData(e.target);
  const out = document.querySelector('#out'); out.textContent='Zeit schätzen …';
  try{ const r = await fetch(base+'/estimate_time',{method:'POST',body:fd});
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}

async function sendCost(e){
  e.preventDefault();
  const out = document.querySelector('#out'); out.textContent='Kosten berechnen …';
  const obj = formToJSON(e.target);
  // leere Strings entfernen
  Object.keys(obj).forEach(k=>{ if(obj[k]==='') delete obj[k]; });
  // numerisch
  ["duration_s","filament_g","filament_mm","machine_rate_eur_h","material_eur_kg","energy_kwh_per_h","energy_eur_per_kwh","setup_fee_eur","risk_pct"].forEach(k=>{
    if(obj[k]!==undefined) obj[k]=Number(obj[k]);
  });
  try{
    const r = await fetch(base+'/estimate_cost',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
    out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}

function openDocs(){ window.open(base + '/docs', '_blank'); }
</script>
"""

# --- Health / Slicer-Env (nur Info) ------------------------------------------
@app.get("/health", response_class=JSONResponse)
def health():
    return {
        "ok": True,
        "slicer_bin": SLICER_BIN,
        "slicer_present": slicer_exists(),
    }

@app.get("/slicer_env", response_class=JSONResponse)
def slicer_env():
    which = shutil.which(os.path.basename(SLICER_BIN)) or SLICER_BIN
    info = {"ok": True, "bin_exists": slicer_exists(), "which": which}
    try:
        code, out, err = run([which, "--help"], timeout=8)
        info["return_code"] = code
        info["help_snippet"] = (out or err or "")[:800]
    except Exception as e:
        info["return_code"] = None
        info["help_snippet"] = f"(help not available) {e}"
    return info

# --- Upload-Analyse -----------------------------------------------------------
@app.post("/analyze_upload", response_class=JSONResponse)
async def analyze_upload(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()
    if not (filename.endswith(".stl") or filename.endswith(".3mf")):
        raise HTTPException(400, "Nur STL- oder 3MF-Dateien werden akzeptiert.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Datei > {MAX_FILE_BYTES // (1024*1024)} MB.")

    sha = sha256_of_bytes(data)
    base_meta: Dict[str, Any] = {
        "ok": True,
        "filename": file.filename,
        "filesize_bytes": len(data),
        "sha256": sha,
        "filetype": "stl" if filename.endswith(".stl") else "3mf",
        "generated_at": int(time.time())
    }

    try:
        if filename.endswith(".stl"):
            stl_meta = analyze_stl(data)
            return {**base_meta, "stl": stl_meta}
        else:
            info = analyze_3mf(data)
            return {**base_meta, "three_mf": info}
    except Exception as e:
        raise HTTPException(500, f"Analyse fehlgeschlagen: {e}")

# --- Zeitabschätzung (Orca, nur Slicedata) -----------------------------------
@app.post("/estimate_time", response_class=JSONResponse)
async def estimate_time(
    file: UploadFile = File(...),
    force_slice: str = Form("auto"),  # "auto" | "always"
):
    """
    Minimaler Orca-Aufruf, um Slicedata (print_time_sec, filament_mm/g) zu extrahieren.
    - Kein Laden externer Profile
    - Erst ohne --slice; wenn keine Zeit → Fallback mit --slice 0
    """
    if not slicer_exists():
        raise HTTPException(500, "OrcaSlicer CLI nicht verfügbar.")

    filename = (file.filename or "").lower()
    if not (filename.endswith(".stl") or filename.endswith(".3mf")):
        raise HTTPException(400, "Nur STL- oder 3MF-Dateien werden akzeptiert.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Leere Datei.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Datei > {MAX_FILE_BYTES // (1024*1024)} MB.")

    work = Path(tempfile.mkdtemp(prefix="etime_"))
    try:
        is_3mf = filename.endswith(".3mf")
        inp = work / ("input.3mf" if is_3mf else "input.stl")
        inp.write_bytes(data)
        out_meta = work / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
        out_3mf  = work / "out.3mf"

        def base_cmd():
            return [SLICER_BIN, "--info", "--export-slicedata", str(out_meta), inp.as_posix()]

        logs: List[Dict[str, Any]] = []

        def try_run(cmd: List[str], tag: str):
            code, out, err = run([XVFB, "-a"] + cmd, timeout=900)
            logs.append({"tag": tag, "code": code, "stderr_tail": (err or out)[-300:]})
            return code

        # Versuch 1: ohne --slice (nur Projekt exportieren, falls nötig)
        tried_1 = False
        if force_slice != "always":
            tried_1 = True
            cmd1 = base_cmd() + ["--export-3mf", str(out_3mf)]
            c1 = try_run(cmd1, "no-slice")
            meta1 = parse_slicedata_folder(out_meta) if c1 == 0 else {}

            if meta1.get("duration_s"):
                return {
                    "ok": True,
                    "input_ext": ".3mf" if is_3mf else ".stl",
                    "duration_s": float(meta1["duration_s"]),
                    "filament_mm": meta1.get("filament_mm"),
                    "filament_g": meta1.get("filament_g"),
                    "logs": logs,
                    "notes": "Zeit aus Slicedata (ohne --slice)."
                }

        # Versuch 2: mit --slice 0
        cmd2 = base_cmd() + ["--slice", "0", "--export-3mf", str(out_3mf)]
        c2 = try_run(cmd2, "slice-0")
        if c2 != 0:
            raise HTTPException(500, detail=f"Orca-Run fehlgeschlagen (exit {c2}). Logs: {logs}")

        meta2 = parse_slicedata_folder(out_meta)
        if not meta2.get("duration_s"):
            raise HTTPException(500, detail=f"Keine Druckzeit in Slicedata gefunden. Logs: {logs}")

        return {
            "ok": True,
            "input_ext": ".3mf" if is_3mf else ".stl",
            "duration_s": float(meta2["duration_s"]),
            "filament_mm": meta2.get("filament_mm"),
            "filament_g": meta2.get("filament_g"),
            "logs": logs,
            "notes": "Zeit aus Slicedata (--slice 0)."
        }
    finally:
        # temporäre Dateien wegräumen
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass

# --- Kostenabschätzung --------------------------------------------------------
@app.post("/estimate_cost", response_class=JSONResponse)
async def estimate_cost(payload: Dict[str, Any]):
    """
    JSON Body Felder:
      duration_s (erforderlich)
      material (PLA/PETG/ASA/PC) [optional, default PLA]
      filament_g [optional] oder filament_mm [optional]  (wenn beides da, gewinnt filament_g)
      machine_rate_eur_h [default 6.0]
      material_eur_kg    [default 22.0]
      energy_kwh_per_h   [default 0.1]
      energy_eur_per_kwh [default 0.35]
      setup_fee_eur      [default 0.0]
      risk_pct           [default 0.0]
    """
    try:
        duration_s = float(payload.get("duration_s", None))
    except Exception:
        raise HTTPException(400, "duration_s fehlt oder ist ungültig.")
    if duration_s <= 0:
        raise HTTPException(400, "duration_s muss > 0 sein.")

    material = (payload.get("material") or "PLA").upper()
    machine_rate = float(payload.get("machine_rate_eur_h", 6.0))
    material_eur_kg = float(payload.get("material_eur_kg", 22.0))
    energy_kwh_per_h = float(payload.get("energy_kwh_per_h", 0.10))
    energy_eur_per_kwh = float(payload.get("energy_eur_per_kwh", 0.35))
    setup_fee = float(payload.get("setup_fee_eur", 0.0))
    risk_pct = clamp(float(payload.get("risk_pct", 0.0)), 0.0, 100.0)

    filament_g = payload.get("filament_g", None)
    filament_mm = payload.get("filament_mm", None)
    try:
        filament_g = None if filament_g is None else float(filament_g)
        filament_mm = None if filament_mm is None else float(filament_mm)
    except Exception:
        raise HTTPException(400, "filament_g/filament_mm müssen numerisch sein.")

    # ggf. von mm auf g umrechnen
    if filament_g is None and filament_mm is not None:
        filament_g = filament_mm_to_g(filament_mm, material=material)

    hours = duration_s / 3600.0
    machine_cost = hours * machine_rate
    energy_cost = hours * energy_kwh_per_h * energy_eur_per_kwh
    material_cost = ( (filament_g or 0.0) / 1000.0 ) * material_eur_kg

    subtotal = machine_cost + energy_cost + material_cost + setup_fee
    total = subtotal * (1.0 + risk_pct / 100.0)

    return {
        "ok": True,
        "inputs": {
            "duration_s": duration_s,
            "material": material,
            "filament_g": filament_g,
            "filament_mm": filament_mm,
            "machine_rate_eur_h": machine_rate,
            "material_eur_kg": material_eur_kg,
            "energy_kwh_per_h": energy_kwh_per_h,
            "energy_eur_per_kwh": energy_eur_per_kwh,
            "setup_fee_eur": setup_fee,
            "risk_pct": risk_pct
        },
        "breakdown": {
            "machine_cost_eur": round(machine_cost, 2),
            "energy_cost_eur": round(energy_cost, 2),
            "material_cost_eur": round(material_cost, 2),
            "setup_fee_eur": round(setup_fee, 2),
            "subtotal_eur": round(subtotal, 2),
            "risk_added_eur": round(total - subtotal, 2),
        },
        "total_eur": round(total, 2)
    }
