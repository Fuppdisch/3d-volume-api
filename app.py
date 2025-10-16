def harden_process_profile(
    src_path: str,
    workdir: Path,
    *,
    fill_density_pct: Optional[int] = None,
    printer_json: Optional[dict] = None  # <— NEU
) -> str:
    """
    Process-Profil härten & mit Printer kompatibel machen:
    - type="process", name setzen
    - Relative E ("1"), G92 E0 in layer_gcode; G92 E0 aus before_layer_gcode entfernen
    - Negative Felder auf 0
    - fill_density als STRING in %
    - Prozess von Maschinen-Bindungen befreien (compatible_printers, machine_* …)
      bzw. (wo sinnvoll) an den Drucker anpassen (Nozzle etc.)
    """
    try:
        proc = load_json(Path(src_path))
    except Exception as e:
        raise HTTPException(500, f"Process-Profil ungültig: {e}")

    # Manche Exporte packen alles unter "settings", andere top-level.
    settings = proc.get("settings")
    target = settings if isinstance(settings, dict) else proc

    # --- 0) Drucker-Metadaten extrahieren (so gut es geht)
    prn = printer_json or {}
    prn_name = (prn.get("name") or prn.get("printer_name") or "").strip()
    # Nozzle aus Printer abgreifen, fallback 0.4
    nozzle_from_printer = None
    for key in ("nozzle_diameter", "nozzle", "hotend_nozzle_diameter"):
        v = prn.get(key) if key in prn else prn.get("settings", {}).get(key) if isinstance(prn.get("settings"), dict) else None
        try:
            if v is not None:
                nozzle_from_printer = float(v) if not isinstance(v, str) else float(v.replace(",", "."))
                break
        except Exception:
            pass
    if nozzle_from_printer is None:
        nozzle_from_printer = 0.4

    # --- 1) Relative E (als "1")
    key_rel = "use_relative_e_distances"
    if key_rel in target:
        target[key_rel] = "1"
    elif key_rel in proc and target is not proc:
        proc[key_rel] = "1"
    else:
        target[key_rel] = "1"

    # --- 2) layer_gcode: G92 E0 hinzufügen; in before_layer_gcode entfernen
    def get_field(obj: dict, key: str) -> Optional[str]:
        v = obj.get(key);  return v if isinstance(v, str) else None

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

    # --- 3) Negative Felder korrigieren
    for k in ("tree_support_wall_count", "raft_first_layer_expansion"):
        if k in target:
            v = target[k]
            try:
                fv = float(v) if isinstance(v, str) else float(v)
            except Exception:
                target[k] = "0"
            else:
                target[k] = "0" if fv < 0 else str(int(round(fv))) if float(fv).is_integer() else str(float(fv))
        elif k in proc:
            v = proc[k]
            try:
                fv = float(v) if isinstance(v, str) else float(v)
            except Exception:
                proc[k] = "0"
            else:
                proc[k] = "0" if fv < 0 else str(int(round(fv))) if float(fv).is_integer() else str(float(fv))

    # --- 4) Infill in % (als STRING)
    if fill_density_pct is not None:
        val_str = str(int(max(0, min(100, fill_density_pct))))
        if "fill_density" in target:
            target["fill_density"] = val_str
        elif "fill_density" in proc:
            proc["fill_density"] = val_str
        else:
            target["fill_density"] = val_str

    # --- 5) Maschinen-Bindungen entfernen / angleichen
    # Felder, die häufig Inkompatibilitäten verursachen:
    kill_keys = [
        "compatible_printers", "compatible_printers_condition",
        "machine_name", "machine_series", "machine_type", "machine_technology",
        "machine_profile", "machine_kit", "hotend_type",
        "printer_model", "printer_brand", "printer_series",
        "inherits_from",  # manchmal verweist das Process auf ein anderes Prozessprofil
    ]
    for k in kill_keys:
        if k in target: del target[k]
        if k in proc:   del proc[k]

    # Nozzle in Process mit Printer angleichen (als Zahl/String akzeptiert, hier String)
    if "nozzle_diameter" in target:
        try:
            target["nozzle_diameter"] = str(nozzle_from_printer)
        except Exception:
            pass
    elif "nozzle_diameter" in proc:
        try:
            proc["nozzle_diameter"] = str(nozzle_from_printer)
        except Exception:
            pass
    else:
        target["nozzle_diameter"] = str(nozzle_from_printer)

    # --- 6) type/name setzen
    if "type" not in proc or (isinstance(proc.get("type"), str) and proc.get("type", "").strip() == ""):
        proc["type"] = "process"
    if "name" not in proc or (isinstance(proc.get("name"), str) and proc.get("name", "").strip() == ""):
        base = Path(src_path).stem
        proc["name"] = f"{base} (hardened)"

    out = workdir / "process_hardened.json"
    save_json(out, proc)
    return str(out)
