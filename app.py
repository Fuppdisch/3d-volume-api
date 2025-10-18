# app.py
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ----------------------------
# Konfiguration
# ----------------------------
ORCA_BIN = os.environ.get("ORCA_BIN", "/opt/orca/bin/orca-slicer")
REPO_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))  # erwartet printer/, process/, filament/
MAX_STD_TAIL = 1200  # Zeichen

app = FastAPI(title="OrcaSlicer API", version="1.0.0")


# ----------------------------
# Utilities
# ----------------------------

def _tail(s: str, n: int = MAX_STD_TAIL) -> str:
    s = s or ""
    return s[-n:]


def _ensure_float(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        # erlaubt "300" oder "300.0"
        try:
            return float(v.strip())
        except Exception:
            pass
    raise ValueError(f"Expected number, got {type(v)}: {v!r}")


def _ensure_int(v: Any) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v.strip()))
        except Exception:
            pass
    raise ValueError(f"Expected integer, got {type(v)}: {v!r}")


def _ensure_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    return str(v)


def _ensure_float_list(v: Any) -> List[float]:
    if isinstance(v, (list, tuple)):
        return [ _ensure_float(x) for x in v ]
    raise ValueError(f"Expected list of floats, got {type(v)}")


def _ensure_string_list(v: Any) -> List[str]:
    if isinstance(v, (list, tuple)):
        return [ _ensure_str(x) for x in v ]
    raise ValueError(f"Expected list of strings, got {type(v)}")


def _coerce_bed_shape(val: Any) -> List[List[float]]:
    """
    akzeptiert:
      ["0x0","400x0","400x400","0x400"]  -> [[0,0],[400,0],[400,400],[0,400]]
      [[0,0],[400,0],[400,400],[0,400]] -> unverändert
    """
    if isinstance(val, list):
        if all(isinstance(p, list) and len(p) == 2 for p in val):
            return [[_ensure_float(p[0]), _ensure_float(p[1])] for p in val]
        if all(isinstance(p, str) and "x" in p for p in val):
            pairs = []
            for s in val:
                a, b = s.split("x", 1)
                pairs.append([_ensure_float(a), _ensure_float(b)])
            return pairs
    raise ValueError("bed_shape must be list of [x,y] pairs or list of 'XxY' strings")


def _inject_compat(pr: Dict[str, Any], printer_name: str) -> None:
    """Sorgt dafür, dass der exakte Druckername in compatible_printers enthalten ist."""
    key = "compatible_printers"
    vals: List[str] = []
    if key in pr:
        try:
            vals = _ensure_string_list(pr[key])
        except Exception:
            vals = []
    if printer_name not in vals and "*" not in vals:
        vals.append(printer_name)
    pr[key] = vals
    # leeres Bedingungsfeld erlaubt
    pr.setdefault("compatible_printers_condition", "")


def _coerce_machine_profile(js: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(js)
    out["type"] = "machine"
    out["printer_technology"] = _ensure_str(out.get("printer_technology", "FFF"))
    out["gcode_flavor"] = _ensure_str(out.get("gcode_flavor", "marlin"))
    out["name"] = _ensure_str(out.get("name", "Generic 400x400 0.4 nozzle"))
    # häufige Felder -> sichere Typen
    if "bed_shape" in out:
        out["bed_shape"] = _coerce_bed_shape(out["bed_shape"])
    else:
        out["bed_shape"] = [[0.0,0.0],[400.0,0.0],[400.0,400.0],[0.0,400.0]]
    out["max_print_height"] = _ensure_float(out.get("max_print_height", 300))
    if "min_layer_height" in out:
        out["min_layer_height"] = _ensure_float(out["min_layer_height"])
    if "max_layer_height" in out:
        out["max_layer_height"] = _ensure_float(out["max_layer_height"])
    out["extruders"] = _ensure_int(out.get("extruders", 1))
    # nozzle_diameter als Float-Liste
    nd = out.get("nozzle_diameter", [0.4])
    if isinstance(nd, list):
        out["nozzle_diameter"] = _ensure_float_list(nd)
    else:
        out["nozzle_diameter"] = [_ensure_float(nd)]
    # optionale Meta
    out.setdefault("printer_model", _ensure_str(out.get("printer_model", "Generic 400x400")))
    out.setdefault("printer_variant", _ensure_str(out.get("printer_variant", "0.4")))
    return out


def _coerce_process_profile(js: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(js)
    out["type"] = "process"
    # nichts hart validieren, nur offensichtliche Zahlenfelder glätten, falls vorhanden
    for k in ("layer_height","first_layer_height","line_width","perimeter_extrusion_width",
              "external_perimeter_extrusion_width","infill_extrusion_width"):
        if k in out:
            try:
                out[k] = str(_ensure_float(out[k]))
            except Exception:
                pass
    # Geschwindigkeiten/… als Strings lassen (Orca akzeptiert Strings für ini/json Felder häufig)
    return out


def _coerce_filament_profile(js: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(js)
    out["type"] = "filament"
    return out


def _read_json_file(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_file(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _run_orca(args: List[str]) -> Tuple[int, str, str]:
    cmd = ["xvfb-run", "-a", ORCA_BIN] + args
    proc = subprocess.run(
        cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return proc.returncode, proc.stdout, proc.stderr


def _build_cli(
    datadir: Path,
    printer_json: Path,
    process_json: Path,
    filament_json: Path,
    stl_path: Path,
    do_arrange: bool = True,
    do_orient: bool = True,
    plate: int = 1,
    export_3mf: Optional[Path] = None,
    export_slicedata: Optional[Path] = None,
) -> List[str]:
    args: List[str] = [
        "--debug", "0",
        "--datadir", str(datadir),
        "--load-settings", f"{printer_json};{process_json}",
        "--load-filaments", str(filament_json),
    ]
    if do_arrange:
        args += ["--arrange", "1"]
    if do_orient:
        args += ["--orient", "1"]

    args += [str(stl_path), "--slice", str(plate)]

    if export_3mf:
        args += ["--export-3mf", str(export_3mf)]
    if export_slicedata:
        args += ["--export-slicedata", str(export_slicedata)]
    return args


# ----------------------------
# Modelle für Requests
# ----------------------------

class SliceOptions(BaseModel):
    material: Optional[str] = "PLA"         # Filament-Dateiname ohne .json unter /app/profiles/filament
    infill: Optional[str] = "35%"           # wird in process injiziert als sparse_infill_density, falls gesetzt
    printer_name: Optional[str] = None      # überschreibt den Namen im Printer-Profil (für kompatible_printers)
    arrange: Optional[bool] = True
    orient: Optional[bool] = True


# ----------------------------
# Endpunkte
# ----------------------------

@app.get("/health")
def health():
    bin_exists = Path(ORCA_BIN).exists()
    which = shutil.which(ORCA_BIN) or ""
    return {
        "ok": True,
        "orca_bin": ORCA_BIN,
        "bin_exists": bin_exists,
        "which": which,
        "repo_present": REPO_DIR.exists(),
    }


@app.post("/import_bundle")
async def import_bundle(zip_file: UploadFile = File(...)):
    """
    Erwartet ein ZIP mit:
      bundle_structure.json
      printer/*.json
      process/*.json
      filament/*.json

    Importiert NICHT dauerhaft in /app/profiles,
    sondern prüft Struktur & gibt die gelesenen Inhalte zurück.
    (Dauerhafte Installation wäre optional.)
    """
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        zpath = tdir / "bundle.zip"
        with zpath.open("wb") as f:
            f.write(await zip_file.read())
        shutil.unpack_archive(str(zpath), str(tdir))

        bs = tdir / "bundle_structure.json"
        if not bs.exists():
            return JSONResponse(status_code=400, content={"ok": False, "error": "bundle_structure.json fehlt"})

        meta = _read_json_file(bs)

        # Dateien laden
        printer_files = [tdir / p for p in meta.get("printer_config", [])]
        process_files = [tdir / p for p in meta.get("process_config", [])]
        filament_files = [tdir / p for p in meta.get("filament_config", [])]

        def load_list(paths: List[Path]) -> List[Dict[str, Any]]:
            out = []
            for p in paths:
                if not p.exists():
                    raise FileNotFoundError(f"Bundle referenziert fehlende Datei: {p}")
                out.append(_read_json_file(p))
            return out

        try:
            printers = load_list(printer_files)
            processes = load_list(process_files)
            filaments = load_list(filament_files)
        except Exception as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

        return {
            "ok": True,
            "bundle_meta": meta,
            "counts": {
                "printers": len(printers),
                "processes": len(processes),
                "filaments": len(filaments),
            },
            "previews": {
                "printer_first": printers[0] if printers else None,
                "process_first": processes[0] if processes else None,
                "filament_first": filaments[0] if filaments else None,
            }
        }


@app.post("/slice")
async def slice_endpoint(
    model: UploadFile = File(...),
    options_json: Optional[str] = Form(None)
):
    """
    Sliced eine STL mit Repo-Profilen.
    - ergänzt kompatible_printers automatisch
    - coerct Typen in Printer/Process/Filament
    - wendet optional infill (sparse_infill_density) an
    """
    try:
        opts = SliceOptions.model_validate_json(options_json) if options_json else SliceOptions()
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"options_json invalid: {e}"})

    # Repo-Dateien
    printer_repo = REPO_DIR / "printers" / "X1C.json"  # du hast diesen Namen im Repo
    process_repo = REPO_DIR / "process" / "0.20mm_standard.json"
    filament_repo = REPO_DIR / "filaments" / f"{opts.material}.json"

    if not printer_repo.exists() or not process_repo.exists() or not filament_repo.exists():
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "Repo-Profile fehlen",
                "paths": {
                    "printer": str(printer_repo),
                    "process": str(process_repo),
                    "filament": str(filament_repo),
                },
            },
        )

    # Arbeitsverzeichnis
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        datadir = tdir / "cfg"
        datadir.mkdir(parents=True, exist_ok=True)

        stl_path = tdir / "input.stl"
        with stl_path.open("wb") as f:
            f.write(await model.read())

        # Profile laden & härten
        raw_printer = _read_json_file(printer_repo)
        raw_process = _read_json_file(process_repo)
        raw_filament = _read_json_file(filament_repo)

        # Coercion
        machine = _coerce_machine_profile(raw_printer)
        process = _coerce_process_profile(raw_process)
        filament = _coerce_filament_profile(raw_filament)

        # Druckername
        printer_name = opts.printer_name or machine.get("name") or "Generic 400x400 0.4 nozzle"
        machine["name"] = printer_name

        # kompatible_printers injizieren
        _inject_compat(process, printer_name)
        _inject_compat(filament, printer_name)

        # optional Infill überschreiben
        if opts.infill:
            process["sparse_infill_density"] = _ensure_str(opts.infill)

        # Dateien schreiben
        p_printer = tdir / "printer.json"
        p_process = tdir / "process.json"
        p_filament = tdir / "filament.json"
        _write_json_file(p_printer, machine)
        _write_json_file(p_process, process)
        _write_json_file(p_filament, filament)

        out_3mf = tdir / "out.3mf"
        slice_dir = tdir / "slicedata"

        args = _build_cli(
            datadir=datadir,
            printer_json=p_printer,
            process_json=p_process,
            filament_json=p_filament,
            stl_path=stl_path,
            do_arrange=bool(opts.arrange),
            do_orient=bool(opts.orient),
            plate=1,
            export_3mf=out_3mf,
            export_slicedata=slice_dir,
        )

        code, stdout, stderr = _run_orca(args)

        result = {
            "ok": code == 0,
            "code": code,
            "cmd": " ".join(["xvfb-run", "-a", ORCA_BIN] + args),
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
            "profiles_used": {
                "printer_preview": machine,
                "process_preview": process,
                "filament_preview": filament,
            }
        }

        if code != 0:
            # häufige Fehlerhinweise ergänzen
            hints: List[str] = []
            if "process not compatible with printer" in stdout or "process not compatible with printer" in stderr:
                hints.append("Process/Filament enthält den Printer-Namen evtl. nicht in compatible_printers.")
                hints.append("Typen prüfen: bed_shape (Float-Paare), max_print_height (Zahl), extruders (int), nozzle_diameter (Float-Liste).")
            if "its_convex_hull" in stdout or "its_convex_hull" in stderr:
                hints.append("bed_shape muss numerisch und konvex sein (z. B. [[0,0],[400,0],[400,400],[0,400]]).")
            if "Invalid option --load" in stdout or "Invalid option --load" in stderr:
                hints.append("Benutze --load-settings und --load-filaments, nicht --load.")

            if hints:
                result["hints"] = hints

            return JSONResponse(status_code=500, content=result)

        # Erfolg
        return result


@app.post("/selftest")
def selftest():
    """Prüft Orca-Binary & Repo-Struktur, ohne zu slicen."""
    exists = Path(ORCA_BIN).exists()
    which = shutil.which(ORCA_BIN) or ""
    present = {
        "printer": (REPO_DIR / "printers" / "X1C.json").exists(),
        "process": (REPO_DIR / "process" / "0.20mm_standard.json").exists(),
        "filament_PLA": (REPO_DIR / "filaments" / "PLA.json").exists(),
        "bundle_structure": (REPO_DIR / "bundle_structure.json").exists(),
    }
    return {
        "ok": exists and all(present.values()),
        "orca_bin": ORCA_BIN,
        "bin_exists": exists,
        "which": which,
        "repo_checks": present,
    }


# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    # Hinweis: Kein Inline-JS, keine f-Strings mit '{' in Literalen -> verhindert SyntaxError bei Deploy
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
