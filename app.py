# ---------- app.py ----------
import os
import io
import json
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

import xml.etree.ElementTree as ET
import trimesh
import numpy as np
import re

app = FastAPI(title="3D Print – Fixed Profiles Slicing API")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# --- Slicer / Env ---
SLICER_BIN = (
    os.getenv("SLICER_BIN")
    or os.getenv("ORCASLICER_BIN")
    or os.getenv("PRUSASLICER_BIN")
    or "/usr/local/bin/orca-slicer"
)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp")
os.environ.setdefault(
    "LD_LIBRARY_PATH",
    "/opt/orca/usr/lib:/opt/orca/lib:" + os.environ.get("LD_LIBRARY_PATH", "")
)
os.environ.setdefault(
    "PATH",
    "/opt/orca/usr/bin:/opt/orca/bin:" + os.environ.get("PATH", "")
)

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
XVFB = shutil.which("xvfb-run") or "xvfb-run"

# -----------------------------------------------------------------------------#
#                                     Utils                                    #
# -----------------------------------------------------------------------------#
def sha256_of_bytes(buf: bytes) -> str:
    h = hashlib.sha256()
    h.update(buf)
    return h.hexdigest()

def slicer_exists() -> bool:
    return (os.path.isfile(SLICER_BIN) and os.access(SLICER_BIN, os.X_OK)) or \
           (shutil.which(os.path.basename(SLICER_BIN)) is not None)

def run(cmd: List[str], timeout: int = 900) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""

def np_to_list(x) -> List[float]:
    return list(map(float, x))

def find_profiles() -> Dict[str, List[str]]:
    """Finde Profil-Dateien im Container (/app/profiles)."""
    base = Path("/app/profiles")
    res = {"printer": [], "process": [], "filament": []}
    for key, sub in [("printer", "printers"), ("process", "process"), ("filament", "filaments")]:
        d = base / sub
        if d.exists():
            res[key] = sorted(str(p) for p in d.glob("*.json"))
    return res

def must_pick(profile_paths: List[str], label: str, wanted_name: Optional[str]) -> str:
    """Wähle ein Profil. Wenn wanted_name übergeben wurde, muss es matchen."""
    if wanted_name:
        for p in profile_paths:
            n = Path(p).name
            if n == wanted_name or wanted_name in Path(p).stem:
                return p
        raise HTTPException(400, f"{label}-Profil '{wanted_name}' nicht gefunden.")
    if not profile_paths:
        raise HTTPException(500, f"Kein {label}-Profil vorhanden. Bitte /app/profiles/{label}s/*.json bereitstellen.")
    return profile_paths[0]

def pick_filament_for_material(filament_paths: List[str], material: str) -> Optional[str]:
    """Nimmt das Filament-Profil, dessen Dateiname den Materialstring enthält (PLA/PETG/ASA/PC)."""
    if not filament_paths:
        return None
    m = (material or "").strip().upper()
    for p in filament_paths:
        if m in Path(p).name.upper():
            return p
    return filament_paths[0]

def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))

def save_json(p: Path, obj: dict):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2))

def percent_from_frac(x: float) -> int:
    """0..1 → 0..100 (gerundet, begrenzt)."""
    try:
        f = float(x)
    except Exception:
        return None
    f = max(0.0, min(1.0, f))
    return int(round(f * 100))

def normalize_opt_name(val: Optional[str]) -> Optional[str]:
    """
    Interpretiert leere/Platzhalter-Werte (z.B. 'string', 'null') als None.
    Dadurch greifen Auto-Auswahlen für Profile.
    """
    if val is None:
        return None
    v = str(val).strip().strip('"').strip("'").lower()
    if v in ("", "string", "none", "null", "undefined"):
        return None
    return str(val).strip()

# -----------------------------------------------------------------------------#
#                 Printer-/Process-/Filament-Profile härten                    #
# -----------------------------------------------------------------------------#
def harden_printer_profile(src_path: str, workdir: Path) -> str:
    """
    Printer-Profil so normalisieren, dass Orca es sicher als 'machine' erkennt.
    - type='machine' + name setzen/sichern
    """
    try:
        prn = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Printer-Profil ungültig: {e}")

    if not isinstance(prn, dict):
        raise HTTPException(500, "Printer-Profil hat kein JSON-Objekt als Wurzel.")

    if "type" not in prn or (isinstance(prn.get("type"), str) and prn.get("type", "").strip() == ""):
        prn["type"] = "machine"
    if "name" not in prn or (isinstance(prn.get("name"), str) and prn.get("name", "").strip() == ""):
        prn["name"] = Path(src_path).stem

    out = workdir / "printer_hardened.json"
    save_json(out, prn)
    return str(out)

def harden_process_profile(
    src_path: str,
    workdir: Path,
    *,
    fill_density_pct: Optional[int] = None,
    printer_json: Optional[dict] = None
) -> str:
    """
    Process-Profil härten & mit Printer kompatibel machen:
    - type="process", name setzen/sichern.
    - Relative E ("1"), G92 E0 in layer_gcode; G92 E0 aus before_layer_gcode entfernen.
    - Negative Felder auf 0.
    - fill_density als **String** in %.
    - Maschinen-Bindungen entfernen / Nozzle an Drucker angleichen.
    """
    try:
        proc = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Process-Profil ungültig: {e}")

    if not isinstance(proc, dict):
        raise HTTPException(500, "Process-Profil hat kein JSON-Objekt als Wurzel.")

    settings = proc.get("settings")
    target = settings if isinstance(settings, dict) else proc

    # --- Drucker-Metadaten abgreifen
    prn = printer_json or {}
    nozzle_from_printer = None
    if isinstance(prn, dict):
        candidates = [
            prn.get("nozzle_diameter"),
            prn.get("nozzle"),
            prn.get("hotend_nozzle_diameter"),
        ]
        if isinstance(prn.get("settings"), dict):
            s = prn["settings"]
            candidates += [s.get("nozzle_diameter"), s.get("nozzle"), s.get("hotend_nozzle_diameter")]
        for v in candidates:
            try:
                if v is None:
                    continue
                nozzle_from_printer = float(v) if not isinstance(v, str) else float(str(v).replace(",", "."))
                break
            except Exception:
                pass
    if nozzle_from_printer is None:
        nozzle_from_printer = 0.4

    # Relative E = "1"
    key_rel = "use_relative_e_distances"
    if key_rel in target:
        target[key_rel] = "1"
    elif key_rel in proc and target is not proc:
        proc[key_rel] = "1"
    else:
        target[key_rel] = "1"

    # layer_gcode / before_layer_gcode
    def get_field(obj: dict, key: str) -> Optional[str]:
        v = obj.get(key)
        return v if isinstance(v, str) else None

    lg_src = "proc" if "layer_gcode" in proc else ("target" if "layer_gcode" in target else None)
    lg = get_field(proc, "layer_gcode") or get_field(target, "layer_gcode") or ""
    if "G92 E0" not in lg:
        lg = (lg.strip() + "\nG92 E0\n").strip()
    if lg_src == "proc":
        proc["layer_gcode"] = lg
    else:
        target["layer_gcode"] = lg

    blg_src = "proc" if "before_layer_gcode" in proc else ("target" if "before_layer_gcode" in target else None)
    blg = get_field(proc, "before_layer_gcode") or get_field(target, "before_layer_gcode") or ""
    if "G92 E0" in blg.upper():
        lines = [ln for ln in blg.splitlines() if "G92 E0" not in ln.upper()]
        blg_clean = "\n".join(lines).strip()
        if blg_src == "proc":
            proc["before_layer_gcode"] = blg_clean
        elif blg_src == "target":
            target["before_layer_gcode"] = blg_clean

    # Negative Felder korrigieren
    for k in ("tree_support_wall_count", "raft_first_layer_expansion"):
        if k in target:
            v = target[k]
            try:
                fv = float(v) if not isinstance(v, str) else float(v.replace(",", "."))
            except Exception:
                target[k] = "0"
            else:
                target[k] = "0" if fv < 0 else str(int(round(fv))) if float(fv).is_integer() else str(float(fv))
        elif k in proc:
            v = proc[k]
            try:
                fv = float(v) if not isinstance(v, str) else float(v.replace(",", "."))
            except Exception:
                proc[k] = "0"
            else:
                proc[k] = "0" if fv < 0 else str(int(round(fv))) if float(fv).is_integer() else str(float(fv))

    # Infill in % (als STRING)
    if fill_density_pct is not None:
        val_str = str(int(max(0, min(100, fill_density_pct))))
        if "fill_density" in target:
            target["fill_density"] = val_str
        elif "fill_density" in proc:
            proc["fill_density"] = val_str
        else:
            target["fill_density"] = val_str

    # Maschinen-Bindungen entfernen + Nozzle angleichen
    kill_keys = [
        "compatible_printers", "compatible_printers_condition",
        "machine_name", "machine_series", "machine_type", "machine_technology",
        "machine_profile", "machine_kit", "hotend_type",
        "printer_model", "printer_brand", "printer_series",
        "inherits_from",
    ]
    for k in kill_keys:
        if k in target:
            del target[k]
        if k in proc:
            del proc[k]

    # Nozzle setzen als String
    if "nozzle_diameter" in target:
        target["nozzle_diameter"] = str(nozzle_from_printer)
    elif "nozzle_diameter" in proc:
        proc["nozzle_diameter"] = str(nozzle_from_printer)
    else:
        target["nozzle_diameter"] = str(nozzle_from_printer)

    # type/name setzen
    if "type" not in proc or (isinstance(proc.get("type"), str) and proc.get("type", "").strip() == ""):
        proc["type"] = "process"
    if "name" not in proc or (isinstance(proc.get("name"), str) and proc.get("name", "").strip() == ""):
        proc["name"] = Path(src_path).stem + " (hardened)"

    out = workdir / "process_hardened.json"
    save_json(out, proc)
    return str(out)

def harden_filament_profile(src_path: str, workdir: Path) -> str:
    """
    Filament-Profil so normalisieren, dass Orca es sicher als 'filament' erkennt.
    - type='filament' + name setzen/sichern
    - Kompatibilitäts-/Maschinen-Bindungen entfernen
    - Problematische numerische Felder defensiv normalisieren
    """
    try:
        fil = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Filament-Profil ungültig: {e}")

    if not isinstance(fil, dict):
        raise HTTPException(500, "Filament-Profil hat kein JSON-Objekt als Wurzel.")

    settings = fil.get("settings")
    target = settings if isinstance(settings, dict) else fil

    # type/name sicherstellen
    if "type" not in fil or (isinstance(fil.get("type"), str) and fil.get("type", "").strip() == ""):
        fil["type"] = "filament"
    if "name" not in fil or (isinstance(fil.get("name"), str) and fil.get("name", "").strip() == ""):
        fil["name"] = Path(src_path).stem

    # Maschinen-/Kompatibilitäts-Bindungen entfernen
    kill_keys = [
        "compatible_printers", "compatible_printers_condition",
        "machine_name", "machine_series", "machine_type", "machine_technology",
        "machine_profile", "machine_kit", "hotend_type",
        "printer_model", "printer_brand", "printer_series",
        "inherits_from",
    ]
    for k in kill_keys:
        if k in target: del target[k]
        if k in fil:    del fil[k]

    # Defensiv: einige numerische Felder normalisieren
    for k in ("filament_density", "filament_diameter", "max_fan_speed", "min_fan_speed"):
        if k in target:
            try:
                v = target[k]
                fv = float(v) if not isinstance(v, str) else float(str(v).replace(",", "."))
                if not np.isfinite(fv) or fv < 0:
                    target[k] = "0"
                else:
                    target[k] = str(fv)
            except Exception:
                target[k] = "0"

    out = workdir / "filament_hardened.json"
    save_json(out, fil)
    return str(out)

# -----------------------------------------------------------------------------#
#                         3MF-Sanitisierung                                    #
# -----------------------------------------------------------------------------#
def sanitize_3mf_remove_configs(src_bytes: bytes) -> bytes:
    """
    Entfernt Config-/Metadata-Anteile aus einer 3MF-ZIP, damit NUR unsere festen Profile greifen.
    """
    src = io.BytesIO(src_bytes)
    with zipfile.ZipFile(src, "r") as zin:
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                name = info.filename
                lower = name.lower()

                if lower.startswith("config/") or "/config/" in lower:
                    continue
                if lower.startswith("metadata/") or "/metadata/" in lower:
                    continue
                if (lower.endswith(".json") or lower.endswith(".ini")) and \
                   any(tok in lower for tok in ("config", "setting", "profile")):
                    continue

                data = zin.read(name)

                if lower in ("3d/3dmodel.model", "3d/model.model", "3d/model"):
                    try:
                        s = data.decode("utf-8", errors="ignore")
                        s = re.sub(r"<\s*metadata\b[^>]*>.*?<\s*/\s*metadata\s*>", "", s, flags=re.I | re.S)
                        data = s.encode("utf-8")
                    except Exception:
                        pass

                zout.writestr(name, data)
        return out_buf.getvalue()

# -----------------------------------------------------------------------------#
#                               Analyse (STL/3MF)                              #
# -----------------------------------------------------------------------------#
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

def analyze_3mf(data: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        namelist = zf.namelist()
        model_xml_name = next((c for c in ("3D/3dmodel.model", "3d/3dmodel.model", "3D/Model.model") if c in namelist), None)

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

        tri_total = 0
        out_objects = []
        for obj in objects:
            oid = obj.attrib.get("id")
            typ = obj.attrib.get("type")
            mesh_elem = obj.find("m:mesh", ns) if ns else obj.find("mesh")
            if mesh_elem is None:
                out_objects.append({"id": oid, "type": typ, "triangles": 0})
                continue

            tris = mesh_elem.find("m:triangles", ns) if ns else mesh_elem.find("triangles")
            tri_count = len(tris.findall("m:triangle", ns) if ns else tris.findall("triangle")) if tris is not None else 0
            tri_total += tri_count

            bbox = None
            verts_elem = mesh_elem.find("m:vertices", ns) if ns else mesh_elem.find("vertices")
            if verts_elem is not None:
                vs = (verts_elem.findall("m:vertex", ns) if ns else verts_elem.findall("vertex"))
                coords = []
                for v in vs:
                    try:
                        x = float(v.attrib.get("x", "0"))
                        y = float(v.attrib.get("y", "0"))
                        z = float(v.attrib.get("z", "0"))
                        coords.append((x, y, z))
                    except Exception:
                        pass
                if coords:
                    arr = np.array(coords, dtype=float)
                    vmin = arr.min(axis=0)
                    vmax = arr.max(axis=0)
                    bbox = {
                        "bbox_min": np_to_list(vmin),
                        "bbox_max": np_to_list(vmax),
                        "bbox_size": np_to_list(vmax - vmin),
                    }

            out_objects.append(
                {"id": oid, "type": typ, "triangles": tri_count, **({"bbox": bbox} if bbox else {})}
            )

        res["triangles_total"] = tri_total
        res["objects"] = out_objects
        return res

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

# -----------------------------------------------------------------------------#
#                                UI / Health / Env                             #
# -----------------------------------------------------------------------------#
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<meta charset="utf-8">
<title>3D Print – Fixed Profiles Slicing</title>
<style>
  :root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
  body{font-family:system-ui,Segoe UI,Arial;margin:24px;line-height:1.45;color:var(--fg)}
  h1{margin:0 0 16px;font-size:22px}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px;background:#fff}
  button,input[type=submit]{padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:#111827;color:#fff;cursor:pointer}
  button.secondary{background:#fff;color:#111827}
  label{font-weight:600;margin-right:8px}
  input[type=number],select,input[type=text]{padding:8px 10px;border:1px solid var(--line);border-radius:10px}
  pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:360px;overflow:auto}
  small{color:var(--muted)}
</style>

<h1>3D Print – Fixed Profiles Slicing</h1>
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
    <h3>Druckzeit mit festen Profilen (/estimate_time)</h3>
    <form onsubmit="return sendTime(event)">
      <input type="file" name="file" accept=".stl,.3mf" required>
      <div style="margin:8px 0">
        <label>Material</label>
        <select name="material">
          <option>PLA</option><option>PETG</option><option>ASA</option><option>PC</option>
        </select>
        <label>Infill</label><input name="infill" type="number" step="0.01" value="0.35" style="width:120px">
      </div>
      <div style="margin:8px 0">
        <label>Printer-Profil</label><input name="printer_profile" placeholder="optional: Dateiname">
      </div>
      <div style="margin:8px 0">
        <label>Process-Profil</label><input name="process_profile" placeholder="optional: Dateiname">
      </div>
      <div style="margin:8px 0">
        <label>Filament-Profil</label><input name="filament_profile" placeholder="optional: Dateiname">
      </div>
      <input type="submit" value="Zeit ermitteln">
      <div><small>Es werden ausschließlich eure Profile geladen. Infill & Material kommen aus der Kundeneingabe.</small></div>
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
async function sendAnalyze(e){
  e.preventDefault();
  const fd = new FormData(e.target);
  const out = document.querySelector('#out'); out.textContent='Analysiere …';
  try{ const r = await fetch(base+'/analyze_upload',{method:'POST',body:fd});
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}
async function sendTime(e){
  e.preventDefault();
  const fd = new FormData(e.target);
  const out = document.querySelector('#out'); out.textContent='Slicen …';
  try{ const r = await fetch(base+'/estimate_time',{method:'POST',body:fd});
       out.textContent = JSON.stringify(await r.json(), null, 2);
  }catch(err){ out.textContent='Fehler: '+err; } return false;
}
function openDocs(){ window.open(base + '/docs', '_blank'); }
</script>
"""

# ----------------------------- Health / Env -----------------------------------
@app.get("/health", response_class=JSONResponse)
def health():
    return {
        "ok": True,
        "slicer_bin": SLICER_BIN,
        "slicer_present": slicer_exists(),
        "profiles": find_profiles()
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

# ----------------------------- Upload Analyse ---------------------------------
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

    base_meta: Dict[str, Any] = {
        "ok": True,
        "filename": file.filename,
        "filesize_bytes": len(data),
        "sha256": sha256_of_bytes(data),
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

# ----------------------------- Slicing/Timing --------------------------------
@app.post("/estimate_time", response_class=JSONResponse)
async def estimate_time(
    file: UploadFile = File(...),
    printer_profile: str = Form(None),
    process_profile: str = Form(None),
    filament_profile: str = Form(None),
    material: str = Form("PLA"),  # PLA|PETG|ASA|PC
    infill: float = Form(0.35),   # 0..1
    debug: int = Form(0),         # 1 = Debug-Infos in Response
):
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

    # Swagger/Platzhalter robust ignorieren
    printer_profile  = normalize_opt_name(printer_profile)
    process_profile  = normalize_opt_name(process_profile)
    filament_profile = normalize_opt_name(filament_profile)

    prof = find_profiles()
    pick_printer  = must_pick(prof["printer"],  "printer",  printer_profile)
    pick_process0 = must_pick(prof["process"],  "process",  process_profile)

    if filament_profile:
        pick_filament = must_pick(prof["filament"], "filament", filament_profile)
    else:
        auto_fil = pick_filament_for_material(prof["filament"], material)
        if not auto_fil:
            raise HTTPException(500, "Kein Filament-Profil vorhanden. Bitte /app/profiles/filaments/*.json bereitstellen.")
        pick_filament = auto_fil

    infill_pct = percent_from_frac(infill)
    if infill_pct is None:
        raise HTTPException(400, "Ungültiger Infill-Wert. Erwartet 0..1, z. B. 0.35 für 35%.")

    work = Path(tempfile.mkdtemp(prefix="fixedp_"))
    try:
        is_3mf = filename.endswith(".3mf")
        inp = work / ("input.3mf" if is_3mf else "input.stl")

        if is_3mf:
            data_sane = sanitize_3mf_remove_configs(data)
            inp.write_bytes(data_sane)
        else:
            inp.write_bytes(data)

        out_meta = work / "slicedata"; out_meta.mkdir(parents=True, exist_ok=True)
        out_3mf  = work / "out.3mf"
        datadir  = work / "cfg"; datadir.mkdir(parents=True, exist_ok=True)

        # Profiles härten und kompatibel machen
        hardened_printer_json = load_json(Path(pick_printer))
        hardened_printer  = harden_printer_profile(pick_printer, work)
        hardened_process  = harden_process_profile(
            pick_process0, work,
            fill_density_pct=infill_pct,
            printer_json=hardened_printer_json
        )
        hardened_filament = harden_filament_profile(pick_filament, work)

        # Reihenfolge: PRINTER → PROCESS
        settings_chain = [hardened_printer, hardened_process]
        filament_chain = [hardened_filament]

        base_min = [
            SLICER_BIN,
            "--datadir", str(datadir),
            "--load-settings", ";".join(settings_chain),
            "--load-filaments", ";".join(filament_chain),
            "--export-slicedata", str(out_meta),
            inp.as_posix(),
        ]
        cmd_min = base_min + ["--slice", "0", "--export-3mf", str(out_3mf)]
        full_min = [XVFB, "-a"] + cmd_min

        code, out, err = run(full_min, timeout=900)
        tail = (err or out)[-2000:]
        last_cmd = " ".join(full_min)

        if code != 0 and "No such file: 1" in tail:
            base_ultra = [
                SLICER_BIN,
                "--datadir", str(datadir),
                "--load-settings", ";".join(settings_chain),
                "--load-filaments", ";".join(filament_chain),
                inp.as_posix(),
            ]
            cmd_ultra = base_ultra + ["--slice", "0", "--export-3mf", str(out_3mf), "--export-slicedata", str(out_meta)]
            full_ultra = [XVFB, "-a"] + cmd_ultra
            code2, out2, err2 = run(full_ultra, timeout=900)
            tail2 = (err2 or out2)[-2000:]
            code, out, err, tail = code2, out2, err2, tail2
            last_cmd = " ".join(full_ultra)

        if code != 0:
            if debug:
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": f"Slicing fehlgeschlagen (exit {code})",
                        "cmd": last_cmd,
                        "stderr_tail": tail[-2000:],
                        "stdout_tail": (out or "")[-1000:],
                        "profiles_used": {
                            "printer": Path(pick_printer).name,
                            "process": Path(pick_process0).name,
                            "filament": Path(pick_filament).name
                        }
                    }
                )
            raise HTTPException(500, detail=f"Slicing fehlgeschlagen (exit {code}): {tail[-800:]}")

        meta = parse_slicedata_folder(out_meta)
        if not meta.get("duration_s"):
            if debug:
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": "Keine Druckzeit in Slicedata gefunden.",
                        "cmd": last_cmd,
                        "stderr_tail": tail[-2000:],
                        "stdout_tail": (out or "")[-1000:],
                        "slicedata_files": meta.get("files"),
                    }
                )
            raise HTTPException(500, detail=f"Keine Druckzeit in Slicedata gefunden. Logs: {tail[-800:]}")

        resp = {
            "ok": True,
            "input_ext": ".3mf" if is_3mf else ".stl",
            "profiles_used": {
                "printer": Path(pick_printer).name,
                "process": Path(pick_process0).name,
                "filament": Path(pick_filament).name
            },
            "material": material.upper(),
            "infill_pct": infill_pct,
            "duration_s": float(meta["duration_s"]),
            "filament_mm": meta.get("filament_mm"),
            "filament_g": meta.get("filament_g"),
            "notes": "Gesliced mit festen Profilen (--slice 0). 3MF-Configs entfernt. Reihenfolge: Printer→Process. Fallback aktiv."
        }
        if debug:
            resp["cmd"] = last_cmd
            resp["slicedata_files"] = meta.get("files")
        return resp

    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass
