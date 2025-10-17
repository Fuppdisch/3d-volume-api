# ---- Hilfen: alles als Strings schreiben ------------------------------------
def _to_str(v) -> str:
    # True/False als "1"/"0" (so sind viele Orca-Felder exportiert)
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)

def _to_str_list(x, default_first: str) -> list[str]:
    if isinstance(x, list):
        return [ _to_str(e) for e in x ] if x else [ default_first ]
    if x is None:
        return [ default_first ]
    return [ _to_str(x) ]

# ---- Printer härten: nozzle_diameter etc. als STRING-Liste -------------------
def harden_printer_profile(src_path: str, workdir: Path) -> tuple[str, dict]:
    try:
        prn = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Printer-Profil ungültig: {e}")

    # nozzle_diameter muss eine Liste von Strings sein, z. B. ["0.4"]
    prn["nozzle_diameter"] = _to_str_list(prn.get("nozzle_diameter", "0.4"), "0.4")

    # Optional robust: Achsen-/Geschwindigkeitsfelder ebenfalls als Strings (nur wenn vorhanden)
    for key in [
        "min_layer_height", "max_layer_height",
        "max_print_height", "bed_shape",  # bed_shape bleibt i. d. R. Array von Koordinaten → nicht erzwingen
    ]:
        if key in prn and not isinstance(prn[key], list):
            prn[key] = _to_str(prn[key])

    out = workdir / "printer_hardened.json"
    save_json(out, prn)
    return str(out), prn

# ---- Process härten: nur Strings, invalide Felder neutralisieren -------------
def harden_process_profile(
    src_path: str, workdir: Path,
    *, fill_density_pct: int | None = None,
    printer_json: dict | None = None
) -> str:
    try:
        proc = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Process-Profil ungültig: {e}")

    # Relative Extrusion? Wir lassen E **konsequent** absolut (keine G92-Konflikte):
    # Wenn du relative E brauchst, setze "1" und lass KEIN G92 in Layer-GCode.
    proc["use_relative_e_distances"] = _to_str(proc.get("use_relative_e_distances", "0"))

    # G92 aus Layer-GCodes entfernen, um Konflikte zu vermeiden
    for k in ("before_layer_gcode", "layer_gcode", "before_layer_change_gcode", "layer_change_gcode"):
        if k in proc and isinstance(proc[k], str):
            proc[k] = proc[k].replace("G92 E0", "").strip()

    # Negative Platzhalter neutralisieren → "0" (String!)
    for k in ("tree_support_wall_count", "raft_first_layer_expansion"):
        v = proc.get(k, None)
        try:
            if float(str(v).replace(",", ".")) < 0:
                proc[k] = "0"
        except Exception:
            # wenn kein numerischer Wert: auf "0" setzen
            proc[k] = "0"

    # Infill stets als String 0..100
    if fill_density_pct is not None:
        proc["fill_density"] = _to_str(int(max(0, min(100, fill_density_pct))))
    else:
        # vorhandenen Wert sicher in String casten
        if "fill_density" in proc:
            try:
                fd = float(str(proc["fill_density"]).replace(",", "."))
                proc["fill_density"] = _to_str(int(max(0, min(100, fd))))
            except Exception:
                proc["fill_density"] = "20"  # fallback

    out = workdir / "process_hardened.json"
    save_json(out, proc)
    return str(out)

# ---- Filament härten: unverändert, aber sicherstellen, dass Strings genutzt werden
def harden_filament_profile(src_path: str, workdir: Path, *, material: str) -> str:
    try:
        fila = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Filament-Profil ungültig: {e}")

    # Typisch sind Strings in Presets; wir ändern hier nichts, nur schreiben es zurück.
    out = workdir / "filament_hardened.json"
    save_json(out, fila)
    return str(out)
