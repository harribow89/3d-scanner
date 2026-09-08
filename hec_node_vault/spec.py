"""HEC Node Vault — parametric assembly specification (HEC-PC-002 Rev A).

Pure Python, no bpy: this module works out every part's size, position and
explode vector in millimetres, so the layout can be checked, printed as a
manifest and unit-tested without Blender. `build_blender.py` is a thin layer
that turns this spec into objects.

Coordinates: X = width (right +), Y = depth (rear +), Z = up. Origin sits on
the floor at the centre of the footprint. Every dimension is millimetres, and
`pos` is the centre of the part's bounding box.

Dimensions marked ASSUMPTION in the notes come from the design brief rather
than measured hardware — check them against the real tray, boards and glass
before anything is cut or printed.
"""

# --- parameters ---------------------------------------------------------------

PARAMS = {
    # Envelope (brief: 400 x 400 x 840 W x D x H)
    "width": 400.0,
    "depth": 400.0,
    "height": 840.0,

    # Vertical stack-up: casters + plinth + glazed section + top cap = height
    "caster_height": 75.0,
    "plinth_height": 150.0,
    "top_cap_height": 75.0,

    # Nodes
    "node_count": 5,
    "tier_pitch": 120.0,
    "board": "mATX",          # mATX | ATX
    "board_thickness": 1.6,
    "standoff_height": 9.0,

    # Cable-tray spine (ladder tray stood vertically, rear of the case)
    "tray_width": 150.0,
    "tray_depth": 60.0,
    "tray_rung_thickness": 20.0,
    "tray_setback": 20.0,     # gap between tray face and rear glass

    # Power supplies
    "psu_form": "SFX",        # SFX | ATX — see validate(); five ATX will not fit
    "psu_clearance": 15.0,

    # GPUs
    "gpu_length": 300.0,
    "gpu_height": 120.0,
    "gpu_thickness": 40.0,
    "gpu_riser_height": 20.0,   # how far the card sits above the board
    "cpu_cooler_height": 79.0,  # tower cooler, matching the detail model

    # Cooling
    "fan_size": 120.0,
    "exhaust_fans": 3,
    "intake_fans": 2,

    # Glazing
    "glass_thickness": 6.0,
    "glass_material": "tinted tempered glass",

    # Structure
    "post_size": 25.0,        # corner post section
    "printer_bed": 256.0,     # longest printable edge (Bambu P1S / X1C class)

    # Estimating
    "filament_density": 1.27,     # g/cm3, PETG
    "filament_fill_factor": 0.34,  # walls + gyroid infill as a fraction of solid
    "filament_cost_per_kg": 30.0,  # AUD
}

# Parameter sets. "brief" is the design brief as written; "resolved" is the same
# machine with the fit and thermal blockers designed out — see README.md.
PRESETS = {
    "brief": {},
    "resolved": {
        "width": 500.0,
        "depth": 500.0,
        "height": 1145.0,
        "node_count": 4,
        "tier_pitch": 180.0,
        "plinth_height": 200.0,
        "top_cap_height": 90.0,
        "psu_form": "SFX",
        "fan_size": 140.0,
        "exhaust_fans": 6,
        "intake_fans": 4,
    },
    # Same fixes but keeping all five nodes: taller again.
    "resolved-5": {
        "width": 500.0,
        "depth": 500.0,
        "height": 1325.0,
        "node_count": 5,
        "tier_pitch": 180.0,
        "plinth_height": 200.0,
        "top_cap_height": 90.0,
        "psu_form": "SFX",
        "fan_size": 140.0,
        "exhaust_fans": 6,
        "intake_fans": 4,
    },
}


BOARD_SIZES = {          # width (X) x depth (Y)
    "mATX": (244.0, 244.0),
    "ATX": (305.0, 244.0),
}

PSU_SIZES = {            # width (X) x depth (Y) x height (Z)
    "SFX": (125.0, 100.0, 63.5),
    "ATX": (150.0, 140.0, 86.0),
}

# Estimated masses (kg) for the weight check — nominal catalogue figures.
MASSES = {
    "glass_density": 2500.0,   # kg/m3
    "board": 1.0,
    "gpu": 1.6,
    "psu_sfx": 1.2,
    "psu_atx": 2.0,
    "tray_per_m": 3.2,
    "frame": 12.0,
    "display": 3.5,
}

# Power draw per node (W) for the electrical check.
POWER = {"cpu": 125.0, "gpu": 250.0, "board_and_drives": 60.0, "psu_efficiency": 0.90}


def fan_grid(count, fan, width, depth, y_centre=0.0, margin=40.0, gap=8.0):
    """
    Positions for `count` fans packed into the available face.

    A row of six 140 mm fans is 888 mm wide and the case is 500 — so they wrap
    into rows, centred, the way they would actually be laid out on the plate.
    """
    pitch = fan + gap
    cols = max(1, int((width - margin) // pitch))
    rows = max(1, -(-count // cols))
    # `y_centre` is the front row; further rows sit behind it, so a wrapped row
    # never pushes out through the face the fans are mounted on.
    positions = []
    placed = 0
    for r in range(rows):
        n = min(cols, count - placed)
        for c in range(n):
            positions.append((-((n - 1) * pitch) / 2.0 + c * pitch,
                              y_centre + r * pitch))
            placed += 1
    return positions


def psu_layout(p):
    """
    How the PSUs pack into the plinth, trying both orientations.

    Rotating a unit 90 degrees costs nothing and often decides whether the row
    fits, so pick the orientation that holds the most.
    """
    psu_w, psu_d, psu_h = PSU_SIZES[p["psu_form"]]
    clear = p["psu_clearance"]
    best = None
    for rotated, (fw, fd) in ((False, (psu_w, psu_d)), (True, (psu_d, psu_w))):
        per_row = int((p["width"] - 40.0) // (fw + clear))
        rows = int((p["depth"] - 60.0) // (fd + clear))
        capacity = max(0, per_row) * max(0, rows)
        candidate = {"rotated": rotated, "footprint": (fw, fd), "height": psu_h,
                     "per_row": max(0, per_row), "rows": max(0, rows),
                     "capacity": capacity}
        if best is None or capacity > best["capacity"]:
            best = candidate
    return best


def _p(params=None):
    merged = dict(PARAMS)
    if params:
        merged.update(params)
    return merged


def levels(params=None):
    """Key Z heights of the stack-up, in mm from the floor."""
    p = _p(params)
    base = p["caster_height"]
    plinth_top = base + p["plinth_height"]
    cap_bottom = p["height"] - p["top_cap_height"]
    return {
        "floor": 0.0,
        "chassis_base": base,
        "plinth_top": plinth_top,
        "glazed_bottom": plinth_top,
        "glazed_top": cap_bottom,
        "cap_bottom": cap_bottom,
        "top": p["height"],
        "glazed_height": cap_bottom - plinth_top,
    }


def tier_heights(params=None):
    """Z of each node shelf's top face, evenly placed in the glazed section."""
    p = _p(params)
    lv = levels(p)
    n = int(p["node_count"])
    span = (n - 1) * p["tier_pitch"]
    # Centre the stack in the glazed volume, leaving room for the GPU above the
    # top board and the intake plenum below the bottom one.
    first = lv["glazed_bottom"] + (lv["glazed_height"] - span) / 2.0 - 40.0
    return [round(first + i * p["tier_pitch"], 1) for i in range(n)]


# --- part construction --------------------------------------------------------

def _part(no, key, name, group, kind, size, pos, **kw):
    part = {
        "no": no, "key": key, "name": name, "group": group, "kind": kind,
        "size": [round(v, 2) for v in size], "pos": [round(v, 2) for v in pos],
        "qty": kw.get("qty", 1),
        "printed": kw.get("printed", False),
        "material": kw.get("material", "steel"),
        "explode_dir": kw.get("explode_dir", [0.0, 0.0, 0.0]),
        "explode_dist": kw.get("explode_dist", 0.0),
        "note": kw.get("note", ""),
    }
    for extra in ("print_profile", "radius", "axis", "draft", "family"):
        if extra in kw:
            part[extra] = kw[extra]
    return part


def build_spec(params=None) -> dict:
    """The full assembly: every part with size, position and explode vector."""
    p = _p(params)
    lv = levels(p)
    tiers = tier_heights(p)
    W, D = p["width"], p["depth"]
    hx, hy = W / 2.0, D / 2.0
    gt = p["glass_thickness"]
    parts = []

    board_w, board_d = BOARD_SIZES[p["board"]]
    psu_w, psu_d, psu_h = PSU_SIZES[p["psu_form"]]

    tray_y = hy - p["tray_setback"] - p["tray_depth"] / 2.0
    board_y = tray_y - p["tray_depth"] / 2.0 - board_d / 2.0 - 10.0

    # 1 — Glass enclosure: four sides plus a top, exploding outward individually.
    glaze_z = lv["glazed_bottom"] + lv["glazed_height"] / 2.0
    for label, size, pos, direction in (
        ("Front", (W, gt, lv["glazed_height"]), (0, -hy + gt / 2, glaze_z), (0, -1, 0)),
        ("Rear", (W, gt, lv["glazed_height"]), (0, hy - gt / 2, glaze_z), (0, 1, 0)),
        ("Left", (gt, D, lv["glazed_height"]), (-hx + gt / 2, 0, glaze_z), (-1, 0, 0)),
        ("Right", (gt, D, lv["glazed_height"]), (hx - gt / 2, 0, glaze_z), (1, 0, 0)),
        ("Top", (W, D, gt), (0, 0, lv["glazed_top"] - gt / 2), (0, 0, 1)),
    ):
        parts.append(_part(1, f"01_Glass_{label}", f"Glass panel — {label.lower()}",
                           "Glass", "box", size, pos, material="glass",
                           explode_dir=direction, explode_dist=260.0,
                           note=f"{gt} mm {p['glass_material']}; photos 9 & 11."))

    # 2 — Base plinth: PSU bay and intake plenum.
    parts.append(_part(2, "02_Plinth_Shell", "Base plinth", "Frame", "box",
                       (W, D, p["plinth_height"]),
                       (0, 0, lv["chassis_base"] + p["plinth_height"] / 2),
                       material="dark", explode_dir=(0, 0, -1), explode_dist=200.0,
                       note="Holds the PSUs and ducts intake air; photos 6 & 10."))

    # 3 — Cable-tray spine plus one rung per node.
    tray_h = lv["glazed_top"] - lv["plinth_top"]
    parts.append(_part(3, "03_Tray_Spine", "Cable-tray spine", "Frame", "box",
                       (p["tray_width"], p["tray_depth"], tray_h),
                       (0, tray_y, lv["plinth_top"] + tray_h / 2),
                       material="tray", explode_dir=(0, 1, 0), explode_dist=180.0,
                       note="150 mm ladder tray stood vertically; photos 3 & 7."))
    for i, z in enumerate(tiers, start=1):
        parts.append(_part(3, f"03_Tray_Rung_{i}", f"Tray rung {i}", "Frame", "box",
                           (p["tray_width"], p["tray_depth"], p["tray_rung_thickness"]),
                           (0, tray_y, z - p["tray_rung_thickness"] / 2),
                           material="tray", explode_dir=(0, 1, 0), explode_dist=180.0))

    # 4/5 — One motherboard and GPU per tier.
    for i, z in enumerate(tiers, start=1):
        parts.append(_part(4, f"04_Motherboard_{i}", f"Motherboard {i} ({p['board']})",
                           "Electronics", "box",
                           (board_w, board_d, p["board_thickness"]),
                           (0, board_y, z + p["standoff_height"] + p["board_thickness"] / 2),
                           material="pcb", explode_dir=(0, -1, 0),
                           explode_dist=120.0 + i * 18.0,
                           note="Z790-class board, one per rung; photo 20."))
        parts.append(_part(5, f"05_GPU_{i}", f"Graphics card {i}", "Electronics", "box",
                           (p["gpu_length"], p["gpu_thickness"], p["gpu_height"]),
                           (0, board_y - board_d / 2 + p["gpu_thickness"] / 2 + 30.0,
                            z + p["standoff_height"] + 20.0 + p["gpu_height"] / 2),
                           material="black", explode_dir=(0, -1, 0.35),
                           explode_dist=200.0 + i * 18.0,
                           note="Mounted on a riser above the board, cradled at the far end."))

    # 6 — PSUs racked in the plinth.
    layout = psu_layout(p)
    fw, fd = layout["footprint"]
    psu_z = lv["chassis_base"] + 20.0 + psu_h / 2.0
    per_row = max(1, layout["per_row"])
    for i in range(int(p["node_count"])):
        col, row = i % per_row, i // per_row
        x = -((per_row - 1) * (fw + p["psu_clearance"])) / 2.0 + col * (fw + p["psu_clearance"])
        y = -hy + 40.0 + fd / 2.0 + row * (fd + p["psu_clearance"])
        parts.append(_part(6, f"06_PSU_{i + 1}", f"Power supply {i + 1} ({p['psu_form']}"
                           f"{', rotated' if layout['rotated'] else ''})",
                           "Electronics", "box", (fw, fd, psu_h), (x, y, psu_z),
                           material="black", explode_dir=(0, -1, -0.4), explode_dist=240.0,
                           note=f"{p['psu_form']} unit racked in the plinth."))

    # 7/8 — Top cap: exhaust fans behind a perforated grille.
    cap_z = lv["cap_bottom"] + p["top_cap_height"] / 2.0
    fan = p["fan_size"]
    exhaust_grid = fan_grid(int(p["exhaust_fans"]), fan, W, D)
    for i, (fx, fy) in enumerate(exhaust_grid):
        parts.append(_part(7, f"07_Exhaust_Fan_{i + 1}", f"Exhaust fan {i + 1}",
                           "Cooling", "cyl", (fan, fan, 25.0),
                           (fx, fy, lv["cap_bottom"] + 20.0),
                           material="fan", axis="z", radius=fan / 2.0,
                           explode_dir=(0, 0, 1), explode_dist=200.0,
                           note="Pulls the chimney; see the airflow warning."))
    parts.append(_part(8, "08_Top_Vent_Grille", "Top vent grille", "Cooling", "box",
                       (W - 20.0, D - 20.0, 8.0), (0, 0, p["height"] - 12.0),
                       material="alu", explode_dir=(0, 0, 1), explode_dist=300.0,
                       note="Perforated cap; photos 1 & 2."))
    parts.append(_part(8, "08_Top_Cap_Shell", "Top cap shell", "Frame", "box",
                       (W, D, p["top_cap_height"]), (0, 0, cap_z),
                       material="dark", explode_dir=(0, 0, 1), explode_dist=250.0))

    # 9 — Intake fans in the plinth.
    intake_grid = fan_grid(int(p["intake_fans"]), fan, W, D,
                           y_centre=-hy + fan / 2 + 40.0)
    for i, (fx, fy) in enumerate(intake_grid):
        parts.append(_part(9, f"09_Intake_Fan_{i + 1}", f"Intake fan {i + 1}",
                           "Cooling", "cyl", (fan, fan, 25.0),
                           (fx, fy, lv["chassis_base"] + p["plinth_height"] - 25.0),
                           material="fan", axis="z", radius=fan / 2.0,
                           explode_dir=(0, -0.4, -1), explode_dist=220.0))

    # 10 — Corner RGB, run inside printed diffusers.
    post = p["post_size"]
    for sx in (-1, 1):
        for sy in (-1, 1):
            label = f"{'R' if sx > 0 else 'L'}{'B' if sy > 0 else 'F'}"
            parts.append(_part(10, f"10_LED_Strip_{label}", f"Edge lighting — {label}",
                               "Lighting", "box",
                               (10.0, 10.0, lv["glazed_height"] - 20.0),
                               (sx * (hx - post / 2), sy * (hy - post / 2), glaze_z),
                               material="led", explode_dir=(sx, sy, 0), explode_dist=200.0,
                               note="Planned addition — not in the photo set."))

    # 11/12 — Telemetry display on a VESA arm off the top front.
    parts.append(_part(11, "11_Telemetry_Display", "Telemetry display", "Electronics", "box",
                       (520.0, 45.0, 320.0), (0, -hy - 190.0, lv["glazed_top"] - 40.0),
                       material="black", explode_dir=(0, -1, 0.2), explode_dist=260.0,
                       note="Per-node health dashboard; photo 22 (LG monitor)."))
    parts.append(_part(12, "12_Monitor_Arm", "Monitor arm", "Frame", "box",
                       (60.0, 260.0, 40.0), (0, -hy - 60.0, lv["glazed_top"] + 10.0),
                       material="alu", explode_dir=(0, -1, 0.5), explode_dist=200.0,
                       note="VESA arm cantilevered off the top rail; photos 5 & 8."))

    # 13 — Cable looms dressed down the tray.
    parts.append(_part(13, "13_Cable_Loom", "Cable looms", "Electronics", "box",
                       (60.0, 36.0, lv["glazed_top"] - lv["plinth_top"] - 40.0),
                       (0, hy - 26.0,
                        lv["plinth_top"] + (lv["glazed_top"] - lv["plinth_top"]) / 2),
                       material="black", explode_dir=(0, 1, 0), explode_dist=140.0,
                       note="Dressed down the rear face of the tray."))

    # 14 — Lockable casters.
    for sx in (-1, 1):
        for sy in (-1, 1):
            label = f"{'R' if sx > 0 else 'L'}{'B' if sy > 0 else 'F'}"
            parts.append(_part(14, f"14_Caster_{label}", f"Caster {label}", "Frame", "cyl",
                               (75.0, 75.0, p["caster_height"]),
                               (sx * (hx - 60.0), sy * (hy - 60.0), p["caster_height"] / 2),
                               material="black", axis="z", radius=37.5,
                               explode_dir=(0, 0, -1), explode_dist=180.0,
                               note="75 mm lockable casters; photo 14."))

    parts.extend(printed_parts(p, lv, tiers, board_y, tray_y))
    result = {
        "params": p,
        "levels": lv,
        "tiers": tiers,
        "parts": parts,
        "warnings": validate(p),
        "totals": totals(p, parts),
    }
    for problem in check_envelope(result):
        result["warnings"].append({"severity": "major", "topic": "fit", "text": problem})
    return result


# --- 3D-printed parts ---------------------------------------------------------

def _profile(material, layer, walls, infill, supports, purpose, draft=False):
    return {"material": material, "layer_mm": layer, "walls": walls,
            "infill_pct": infill, "supports": supports, "purpose": purpose,
            "draft": draft}


def printed_parts(p, lv, tiers, board_y, tray_y) -> list:
    """
    Every part that gets printed, positioned in the assembly.

    Geometry here is parametric massing with correct bounding sizes and mounting
    positions. Parts marked draft=True in their profile need dimensioned CAD
    against the real hardware before you print them for fit; the rest
    (comb, pad, shroud, diffuser, VESA plate) are simple enough to print as
    generated once the hole positions are confirmed.
    """
    W, D = p["width"], p["depth"]
    hx, hy = W / 2.0, D / 2.0
    out = []
    board_w, board_d = BOARD_SIZES[p["board"]]
    psu_w, psu_d, psu_h = PSU_SIZES[p["psu_form"]]

    # P01 — node sled rails, two per tier, carrying the board on standoffs.
    # Split into rails rather than one shelf so each fits the print bed.
    rail_len = min(board_d + 20.0, p["printer_bed"] - 8.0)
    for i, z in enumerate(tiers, start=1):
        for side, sx in (("L", -1), ("R", 1)):
            out.append(_part(101, f"P01_Node_Rail_{i}{side}", f"Node sled rail {i}{side}",
                             "Printed", "box", (40.0, rail_len, 25.0),
                             (sx * (board_w / 2 - 20.0), board_y, z - 12.5),
                             printed=True, material="printed", family="Node sled rail",
                             explode_dir=(sx * 0.6, -1, 0), explode_dist=150.0,
                             qty=1,
                             print_profile=_profile("PETG", 0.24, 4, 40, False,
                                                    "Carries a node on M3 standoffs; "
                                                    "clamps to the tray rung."),
                             note=f"{rail_len:.0f} mm long — sized to fit a "
                                  f"{p['printer_bed']:.0f} mm bed."))

    # P02 — tray rung clamps, four per tier.
    for i, z in enumerate(tiers, start=1):
        for j, (sx, sy) in enumerate(((-1, -1), (1, -1), (-1, 1), (1, 1)), start=1):
            out.append(_part(102, f"P02_Rung_Clamp_{i}_{j}", f"Tray rung clamp {i}.{j}",
                             "Printed", "box", (55.0, 45.0, 30.0),
                             (sx * (p["tray_width"] / 2 - 27.5),
                              tray_y + sy * (p["tray_depth"] / 2 - 22.5), z + 5.0),
                             printed=True, material="printed", family="Tray rung clamp",
                             explode_dir=(sx * 0.5, sy, 0.4), explode_dist=130.0,
                             print_profile=_profile("PETG", 0.2, 5, 50, False,
                                                    "Clamps the sled rail to the ladder "
                                                    "rung; M6 bolt through.", draft=True)))

    # P03 — GPU cradle / anti-sag brace, one per node.
    for i, z in enumerate(tiers, start=1):
        out.append(_part(103, f"P03_GPU_Cradle_{i}", f"GPU cradle {i}", "Printed", "box",
                         (60.0, 50.0, 90.0),
                         (p["gpu_length"] / 2 - 40.0, board_y - board_d / 2 + 45.0,
                          z + p["standoff_height"] + 45.0),
                         printed=True, material="printed", family="GPU cradle",
                         explode_dir=(1, -0.5, 0), explode_dist=170.0,
                         print_profile=_profile("PETG", 0.24, 4, 35, True,
                                                "Takes the far end of the card so the "
                                                "slot carries no cantilever load.",
                                                draft=True)))

    # P04 — PSU cradles in the plinth.
    layout = psu_layout(p)
    fw, fd = layout["footprint"]
    per_row = max(1, layout["per_row"])
    for i in range(int(p["node_count"])):
        col, row = i % per_row, i // per_row
        x = -((per_row - 1) * (fw + p["psu_clearance"])) / 2.0 + col * (fw + p["psu_clearance"])
        y = -hy + 40.0 + fd / 2.0 + row * (fd + p["psu_clearance"])
        out.append(_part(104, f"P04_PSU_Cradle_{i + 1}", f"PSU cradle {i + 1}",
                         "Printed", "box", (fw + 12.0, fd + 12.0, 22.0),
                         (x, y, p["caster_height"] + 20.0 - 11.0),
                         printed=True, material="printed", family="PSU cradle",
                         explode_dir=(0, -1, -0.6), explode_dist=200.0,
                         print_profile=_profile("ASA", 0.24, 4, 30, False,
                                                "Locates and isolates the PSU; ASA "
                                                "because it sits in the hot air path.",
                                                draft=True)))

    # P05 — fan shrouds, one per fan, ducting the fan to its grille.
    fan = p["fan_size"]
    for i, (fx, fy) in enumerate(fan_grid(int(p["exhaust_fans"]), fan, W, D)):
        out.append(_part(105, f"P05_Fan_Shroud_Ex_{i + 1}", f"Exhaust fan shroud {i + 1}",
                         "Printed", "box", (fan + 10.0, fan + 10.0, 28.0),
                         (fx, fy, lv["cap_bottom"] + 45.0),
                         printed=True, material="printed", family="Fan shroud",
                         explode_dir=(0, 0, 1), explode_dist=240.0,
                         print_profile=_profile("PETG", 0.28, 3, 20, False,
                                                "Seals fan to grille so air is not "
                                                "recirculated around the frame.")))
    intake_grid = fan_grid(int(p["intake_fans"]), fan, W, D,
                           y_centre=-hy + fan / 2 + 40.0)
    for i, (fx, fy) in enumerate(intake_grid):
        out.append(_part(105, f"P05_Fan_Shroud_In_{i + 1}", f"Intake fan shroud {i + 1}",
                         "Printed", "box", (fan + 10.0, fan + 10.0, 28.0),
                         (fx, fy, lv["chassis_base"] + p["plinth_height"] - 55.0),
                         printed=True, material="printed", family="Fan shroud",
                         explode_dir=(0, -0.5, -1), explode_dist=240.0,
                         print_profile=_profile("PETG", 0.28, 3, 20, False,
                                                "Ducts intake air and carries the dust "
                                                "filter frame.")))

    # P06 — corner LED diffusers, segmented to fit the bed.
    seg_len = min(p["printer_bed"] - 20.0, 240.0)
    # Ceiling, not round: rounding down makes each segment LONGER than the
    # target, which is how the brief preset ended up with 260 mm parts for a
    # 256 mm bed.
    segs = max(1, int(-(-(lv["glazed_height"] - 20.0) // seg_len)))
    seg_h = (lv["glazed_height"] - 20.0) / segs
    for sx in (-1, 1):
        for sy in (-1, 1):
            label = f"{'R' if sx > 0 else 'L'}{'B' if sy > 0 else 'F'}"
            for s in range(segs):
                z = lv["glazed_bottom"] + 10.0 + seg_h * (s + 0.5)
                out.append(_part(106, f"P06_LED_Diffuser_{label}_{s + 1}",
                                 f"LED diffuser {label} segment {s + 1}",
                                 "Printed", "box", (22.0, 22.0, seg_h),
                                 (sx * (hx - p["post_size"] / 2), sy * (hy - p["post_size"] / 2), z),
                                 printed=True, material="diffuser", family="LED corner diffuser",
                                 explode_dir=(sx, sy, 0), explode_dist=230.0,
                                 print_profile=_profile("Clear PETG", 0.2, 2, 15, False,
                                                        "Print in clear PETG, 2 walls, "
                                                        "no top surface — diffuses the "
                                                        "strip into an even line.")))

    # P07 — glass edge clips.
    clip_rows = [lv["glazed_bottom"] + 60.0, lv["glazed_bottom"] + lv["glazed_height"] / 2,
                 lv["glazed_top"] - 60.0]
    for ri, z in enumerate(clip_rows, start=1):
        for label, x, y in (("F", 0.0, -hy + 25.0), ("B", 0.0, hy - 25.0),
                            ("L", -hx + 25.0, 0.0), ("R", hx - 25.0, 0.0)):
            out.append(_part(107, f"P07_Glass_Clip_{label}{ri}", f"Glass edge clip {label}{ri}",
                             "Printed", "box", (45.0, 45.0, 18.0), (x, y, z),
                             printed=True, material="printed", family="Glass edge clip",
                             explode_dir=(0 if x == 0 else (1 if x > 0 else -1),
                                          0 if y == 0 else (1 if y > 0 else -1), 0),
                             explode_dist=280.0,
                             print_profile=_profile("TPU 95A", 0.2, 3, 25, False,
                                                    "Flexible clip — grips the pane "
                                                    "without a hard glass-on-metal "
                                                    "contact.")))

    # P08 — corner post brackets, top and bottom of each post.
    for sx in (-1, 1):
        for sy in (-1, 1):
            for label, z in (("Bot", lv["glazed_bottom"] + 35.0),
                             ("Top", lv["glazed_top"] - 35.0)):
                corner = f"{'R' if sx > 0 else 'L'}{'B' if sy > 0 else 'F'}"
                out.append(_part(108, f"P08_Corner_Bracket_{corner}_{label}",
                                 f"Corner bracket {corner} {label.lower()}",
                                 "Printed", "box", (70.0, 70.0, 70.0),
                                 (sx * (hx - 35.0), sy * (hy - 35.0), z),
                                 printed=True, material="printed", family="Corner bracket",
                                 explode_dir=(sx, sy, 0.5), explode_dist=200.0,
                                 print_profile=_profile("PETG", 0.2, 5, 60, True,
                                                        "Structural corner — ties post, "
                                                        "plinth and cap. Print solid-ish; "
                                                        "this one carries the glass load.",
                                                        draft=True)))

    # P09 — cable combs down the tray.
    comb_count = max(4, int(p["node_count"]) * 2)
    for i in range(comb_count):
        z = lv["plinth_top"] + 60.0 + i * ((lv["glazed_top"] - lv["plinth_top"] - 120.0)
                                           / max(1, comb_count - 1))
        out.append(_part(109, f"P09_Cable_Comb_{i + 1}", f"Cable comb {i + 1}",
                         "Printed", "box", (90.0, 22.0, 14.0),
                         (0, hy - 20.0, z),
                         printed=True, material="printed", family="Cable comb",
                         explode_dir=(0, 1, 0), explode_dist=160.0,
                         print_profile=_profile("PETG", 0.2, 3, 20, False,
                                                "Dresses the loom into lanes; clips "
                                                "over the tray side rail.")))

    # P10 — VESA 100 adapter plate.
    out.append(_part(110, "P10_VESA_Plate", "VESA 100 adapter plate", "Printed", "box",
                     (140.0, 12.0, 140.0), (0, -hy - 150.0, lv["glazed_top"] - 40.0),
                     printed=True, material="printed", family="VESA adapter plate",
                     explode_dir=(0, -1, 0), explode_dist=200.0,
                     print_profile=_profile("PETG", 0.2, 5, 60, False,
                                            "Adapts the arm to the display's VESA "
                                            "pattern. Print flat, 5 walls.")))

    # P11 — tray end caps.
    for label, z in (("Bottom", lv["plinth_top"] + 12.0), ("Top", lv["glazed_top"] - 12.0)):
        out.append(_part(111, f"P11_Tray_Cap_{label}", f"Tray end cap — {label.lower()}",
                         "Printed", "box", (p["tray_width"] + 8.0, p["tray_depth"] + 8.0, 20.0),
                         (0, tray_y, z),
                         printed=True, material="printed", family="Tray end cap",
                         explode_dir=(0, 0.4, 1 if label == "Top" else -1), explode_dist=190.0,
                         print_profile=_profile("PETG", 0.24, 3, 25, False,
                                                "Caps the cut tray ends — no sharp "
                                                "galvanised edges near cables.")))

    # P12 — caster mount pads spreading load into the plinth floor.
    for sx in (-1, 1):
        for sy in (-1, 1):
            corner = f"{'R' if sx > 0 else 'L'}{'B' if sy > 0 else 'F'}"
            out.append(_part(112, f"P12_Caster_Pad_{corner}", f"Caster mount pad {corner}",
                             "Printed", "box", (95.0, 95.0, 14.0),
                             (sx * (hx - 60.0), sy * (hy - 60.0),
                              p["caster_height"] + 7.0),
                             printed=True, material="printed", family="Caster mount pad",
                             explode_dir=(0, 0, -1), explode_dist=150.0,
                             print_profile=_profile("PETG", 0.2, 6, 60, False,
                                                    "Spreads ~15 kg per corner into the "
                                                    "plinth floor. High wall count.")))

    # P13 — display bezel, segmented into rails and stiles that fit the bed.
    bezel_w, bezel_h, bezel_t = 540.0, 350.0, 25.0
    usable = p["printer_bed"] - 20.0
    bezel_y = -hy - 205.0
    bezel_z = lv["glazed_top"] - 40.0
    n_rail = max(1, int(-(-bezel_w // usable)))          # top and bottom rails
    n_stile = max(1, int(-(-bezel_h // usable)))          # left and right stiles
    rail_seg = bezel_w / n_rail
    stile_seg = bezel_h / n_stile
    for edge, sign in (("Top", 1), ("Bottom", -1)):
        for s in range(n_rail):
            x = -bezel_w / 2 + rail_seg * (s + 0.5)
            out.append(_part(113, f"P13_Bezel_{edge}_{s + 1}",
                             f"Display bezel {edge.lower()} rail {s + 1}",
                             "Printed", "box", (rail_seg, bezel_t, 45.0),
                             (x, bezel_y, bezel_z + sign * (bezel_h / 2 - 22.5)),
                             printed=True, material="printed", family="Display bezel rail",
                             explode_dir=(0, -1, sign * 0.4), explode_dist=240.0,
                             print_profile=_profile("PETG", 0.24, 3, 15, False,
                                                    "Bezel frame, segmented to fit the "
                                                    "bed; dowel and glue the joints.",
                                                    draft=True)))
    for edge, sign in (("Left", -1), ("Right", 1)):
        for s in range(n_stile):
            z = bezel_z - bezel_h / 2 + stile_seg * (s + 0.5)
            out.append(_part(113, f"P13_Bezel_{edge}_{s + 1}",
                             f"Display bezel {edge.lower()} stile {s + 1}",
                             "Printed", "box", (45.0, bezel_t, stile_seg),
                             (sign * (bezel_w / 2 - 22.5), bezel_y, z),
                             printed=True, material="printed", family="Display bezel stile",
                             explode_dir=(sign * 0.4, -1, 0), explode_dist=240.0,
                             print_profile=_profile("PETG", 0.24, 3, 15, False,
                                                    "Bezel frame, segmented to fit the "
                                                    "bed; dowel and glue the joints.",
                                                    draft=True)))

    # P14 — intake dust filter frames.
    for i, (fx, fy) in enumerate(intake_grid):
        out.append(_part(114, f"P14_Filter_Frame_{i + 1}", f"Dust filter frame {i + 1}",
                         "Printed", "box", (fan + 20.0, fan + 20.0, 12.0),
                         (fx, fy, lv["chassis_base"] + 30.0),
                         printed=True, material="printed", family="Dust filter frame",
                         explode_dir=(0, -1, -0.3), explode_dist=260.0,
                         print_profile=_profile("PETG", 0.24, 3, 20, False,
                                                "Holds a cut mesh square; slides out "
                                                "from the front for cleaning.")))
    return out


# --- checks -------------------------------------------------------------------

def validate(params=None) -> list:
    """
    Engineering sanity checks on the parameters.

    These are first-principles estimates, not a substitute for measuring the
    real hardware — but each one flags something that would cost money or
    safety to discover after the glass is cut.
    """
    p = _p(params)
    lv = levels(p)
    warnings = []
    W, D = p["width"], p["depth"]
    n = int(p["node_count"])

    # 1. Do the PSUs physically fit in the plinth?
    psu_w, psu_d, psu_h = PSU_SIZES[p["psu_form"]]
    layout = psu_layout(p)
    if layout["capacity"] < n:
        remedy = ("Switch to SFX units" if p["psu_form"] == "ATX" else
                  "Widen the plinth, raise it for a second layer,")
        warnings.append({
            "severity": "blocker", "topic": "fit",
            "text": (f"{n} x {p['psu_form']} PSUs will not fit the "
                     f"{W:.0f} x {D:.0f} mm plinth: it holds {layout['capacity']} "
                     f"({layout['per_row']} across x {layout['rows']} deep"
                     f"{', rotated' if layout['rotated'] else ''}). {remedy} or move "
                     f"the PSUs to a rear column beside the tray.")})
    if psu_h + 40.0 > p["plinth_height"]:
        warnings.append({
            "severity": "major", "topic": "fit",
            "text": (f"{p['psu_form']} PSU is {psu_h:.0f} mm tall; a "
                     f"{p['plinth_height']:.0f} mm plinth leaves too little room for "
                     f"cabling and the intake plenum.")})

    # 2. Board plus GPU against the tier pitch.
    stack = p["standoff_height"] + p["board_thickness"] + 20.0 + p["gpu_height"]
    if stack > p["tier_pitch"] - 10.0:
        warnings.append({
            "severity": "major", "topic": "fit",
            "text": (f"Board + riser + GPU is ~{stack:.0f} mm per tier but the pitch is "
                     f"{p['tier_pitch']:.0f} mm. Raise the pitch to ~{stack + 25:.0f} mm "
                     f"(and the envelope with it) or lay the GPUs flat on risers.")})

    # 3. Does the cooler fit under the card?
    cooler_top = p["standoff_height"] + p["board_thickness"] + p["cpu_cooler_height"]
    gpu_bottom = p["standoff_height"] + p["board_thickness"] + p["gpu_riser_height"]
    if cooler_top > gpu_bottom:
        needed = p["cpu_cooler_height"] + 10.0
        warnings.append({
            "severity": "major", "topic": "fit",
            "text": (f"A {p['cpu_cooler_height']:.0f} mm tower cooler reaches "
                     f"{cooler_top:.0f} mm above the tray, but the card sits at "
                     f"{gpu_bottom:.0f} mm — they occupy the same space. Fit "
                     f"low-profile coolers (~45 mm), raise the card to "
                     f"{needed:.0f} mm on taller risers, or offset the card so it "
                     f"clears the cooler in plan.")})

    # 4. GPU length against the internal width.
    if p["gpu_length"] > W - 2 * p["glass_thickness"] - 30.0:
        warnings.append({
            "severity": "major", "topic": "fit",
            "text": (f"A {p['gpu_length']:.0f} mm card will not clear a {W:.0f} mm "
                     f"envelope once glass and brackets are in. Widen to "
                     f"~{p['gpu_length'] + 60:.0f} mm or fit shorter cards.")})

    # 5. Airflow: can the fans actually shift the heat?
    node_w = POWER["cpu"] + POWER["gpu"] + POWER["board_and_drives"]
    total_w = node_w * n / POWER["psu_efficiency"]
    delta_t = 15.0
    # Q = P / (rho * cp * dT), air at ~1.2 kg/m3 and 1005 J/kg.K
    q_m3s = total_w / (1.2 * 1005.0 * delta_t)
    cfm_needed = q_m3s * 2118.88
    # ~60 CFM per 120 mm fan free-air, derated hard through grille + filter.
    # Free-air CFM by frame size, derated 45% for grille, filter and duct losses.
    free_air = {120.0: 60.0, 140.0: 100.0, 200.0: 150.0}.get(float(p["fan_size"]),
                                                             float(p["fan_size"]) / 2.0)
    cfm_have = p["exhaust_fans"] * free_air * 0.55
    if cfm_have < cfm_needed:
        warnings.append({
            "severity": "blocker", "topic": "thermal",
            "text": (f"~{total_w:.0f} W of heat needs about {cfm_needed:.0f} CFM to hold "
                     f"a {delta_t:.0f} K rise, but {int(p['exhaust_fans'])} x "
                     f"{p['fan_size']:.0f} mm exhaust fans give roughly {cfm_have:.0f} CFM "
                     f"through a grille. You need about "
                     f"{max(1, int(-(-cfm_needed // (100.0 * 0.55))))} x 140 mm "
                     f"high-static-pressure fans, or accept a bigger temperature rise.")})

    # 6. Electrical load.
    amps = total_w / 230.0
    if amps > 9.0:
        warnings.append({
            "severity": "blocker", "topic": "electrical",
            "text": (f"~{total_w:.0f} W is about {amps:.1f} A at 230 V — at or over a "
                     f"standard 10 A outlet circuit, before anything else on it. Needs "
                     f"its own circuit (or two), and inrush from five PSUs starting "
                     f"together will nuisance-trip a Type B RCBO; specify Type C.")})

    # 7. Weight on the casters.
    glass_area = 2 * (W * lv["glazed_height"]) + 2 * (D * lv["glazed_height"]) + (W * D)
    glass_kg = (glass_area * p["glass_thickness"] / 1e9) * MASSES["glass_density"]
    psu_kg = MASSES["psu_atx"] if p["psu_form"] == "ATX" else MASSES["psu_sfx"]
    total_kg = (glass_kg + n * (MASSES["board"] + MASSES["gpu"] + psu_kg)
                + MASSES["tray_per_m"] * (lv["glazed_height"] / 1000.0)
                + MASSES["frame"] + MASSES["display"])
    warnings.append({
        "severity": "info", "topic": "weight",
        "text": (f"Estimated all-up mass ~{total_kg:.0f} kg ({glass_kg:.0f} kg of that is "
                 f"glass). That is ~{total_kg / 4:.0f} kg per caster — specify 75 mm "
                 f"casters rated 50 kg+ each, and lock them: the centre of mass is high.")})

    # 8. Tall glass box stability.
    if p["height"] / min(W, D) > 2.0:
        warnings.append({
            "severity": "major", "topic": "stability",
            "text": (f"{p['height']:.0f} mm tall on a {min(W, D):.0f} mm base is a "
                     f"{p['height'] / min(W, D):.1f}:1 ratio with the mass up high. "
                     f"Widen the base, add outrigger feet, or plan to strap it to a wall.")})

    return warnings


OUTBOARD_PREFIXES = ("11_", "12_", "P10_", "P13_")  # display, arm, bezel — by design


def check_envelope(spec: dict) -> list:
    """
    Parts that stick out through the case, which a render shows immediately and
    a bounding-box check does not.

    The display, its arm, the VESA plate and the bezel are cantilevered in front
    on purpose; everything else has to live inside the envelope.
    """
    p = spec["params"]
    limits = {0: p["width"] / 2, 1: p["depth"] / 2}
    problems = []
    for part in spec["parts"]:
        if part["key"].startswith(OUTBOARD_PREFIXES):
            continue
        for axis, limit in limits.items():
            low = part["pos"][axis] - part["size"][axis] / 2
            high = part["pos"][axis] + part["size"][axis] / 2
            if low < -limit - 0.5 or high > limit + 0.5:
                over = max(-limit - low, high - limit)
                problems.append(f"{part['key']} protrudes {over:.0f} mm past the "
                                f"{'width' if axis == 0 else 'depth'} envelope")
                break
        top = part["pos"][2] + part["size"][2] / 2
        if top > p["height"] + 0.5:
            problems.append(f"{part['key']} stands {top - p['height']:.0f} mm proud "
                            f"of the top")
    return problems


def totals(params=None, parts=None) -> dict:
    """Counts, printed-part filament estimate and print-bed check."""
    p = _p(params)
    if parts is None:
        parts = build_spec(p)["parts"]
    printed = [q for q in parts if q.get("printed")]

    volume_cm3 = 0.0
    oversize = []
    for part in printed:
        w, d, h = part["size"]
        volume_cm3 += (w * d * h) / 1000.0
        if max(w, d, h) > p["printer_bed"]:
            oversize.append(part["key"])

    solid_cm3 = volume_cm3 * p["filament_fill_factor"]
    grams = solid_cm3 * p["filament_density"]
    return {
        "part_count": len(parts),
        "printed_part_count": len(printed),
        "printed_bbox_volume_cm3": round(volume_cm3, 1),
        "filament_grams_est": round(grams, 0),
        "filament_spools_est": round(grams / 1000.0, 2),
        "filament_cost_est": round(grams / 1000.0 * p["filament_cost_per_kg"], 2),
        "oversize_for_bed": oversize,
    }


def manifest_markdown(spec: dict) -> str:
    """Printed-parts manifest plus the parameter and warning summary."""
    p, lv = spec["params"], spec["levels"]
    t = spec["totals"]
    printed = [q for q in spec["parts"] if q.get("printed")]

    # Group by family, not BOM number: the bezel rails and stiles share a number
    # but are different prints, and each needs its own row.
    grouped = {}
    for part in printed:
        grouped.setdefault((part["no"], part.get("family") or part["name"]), []).append(part)

    out = ["# HEC Node Vault — build manifest", "",
           f"Generated from `spec.py` (HEC-PC-002 Rev A). Envelope "
           f"{p['width']:.0f} x {p['depth']:.0f} x {p['height']:.0f} mm, "
           f"{int(p['node_count'])} nodes at {p['tier_pitch']:.0f} mm pitch, "
           f"{p['board']} boards, {p['psu_form']} PSUs.", "",
           f"- Parts in the assembly: **{t['part_count']}**",
           f"- Printed parts: **{t['printed_part_count']}**",
           f"- Filament estimate: **~{t['filament_grams_est']:.0f} g** "
           f"(~{t['filament_spools_est']:.2f} spools, ~${t['filament_cost_est']:.0f} "
           f"at ${p['filament_cost_per_kg']:.0f}/kg) — bounding-box volume x "
           f"{p['filament_fill_factor']:.0%} fill, so treat it as an upper bound.",
           f"- Shelf heights (mm): {', '.join(f'{z:.0f}' for z in spec['tiers'])}",
           f"- Glazed section: {lv['glazed_bottom']:.0f} to {lv['glazed_top']:.0f} mm", ""]

    if t["oversize_for_bed"]:
        out += [f"> **{len(t['oversize_for_bed'])} part(s) exceed the "
                f"{p['printer_bed']:.0f} mm bed:** "
                + ", ".join(t["oversize_for_bed"]), ""]

    out += ["## Printed parts", "",
            "| # | Part | Qty | Size (mm) | Material | Layer | Walls | Infill | Support | Status |",
            "|--:|------|----:|-----------|----------|-------|------:|-------:|---------|--------|"]
    for key in sorted(grouped):
        no = key[0]
        group = grouped[key]
        first = group[0]
        prof = first.get("print_profile", {})
        w, d, h = first["size"]
        name = first.get("family") or first["name"]
        out.append(
            f"| {no - 100} | {name} | {len(group)} | {w:.0f} x {d:.0f} x {h:.0f} | "
            f"{prof.get('material', '?')} | {prof.get('layer_mm', '?')} | "
            f"{prof.get('walls', '?')} | {prof.get('infill_pct', '?')}% | "
            f"{'yes' if prof.get('supports') else 'no'} | "
            f"{'massing — needs dimensioned CAD' if prof.get('draft') else 'massing — simple form'} |")

    out += ["", "> **Every STL this project exports is a massing envelope**: correct "
            "outside dimensions and mounting position, but no bores, slots, "
            "counterbores or fastener holes. A fan shroud exported today is a solid "
            "block that would seal the airflow path, not duct it. Model the openings "
            "before printing anything for fit.", ""]
    out += ["", "### What each part is for", ""]
    for key in sorted(grouped):
        prof = grouped[key][0].get("print_profile", {})
        out.append(f"- **{key[1]}** — {prof.get('purpose', '')}")

    out += ["", "## Checks", ""]
    for w in spec["warnings"]:
        icon = {"blocker": "**BLOCKER**", "major": "**Major**",
                "info": "Note"}.get(w["severity"], w["severity"])
        out.append(f"- {icon} ({w['topic']}): {w['text']}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import json
    import sys

    preset = "brief"
    if "--preset" in sys.argv:
        preset = sys.argv[sys.argv.index("--preset") + 1]
    if preset not in PRESETS:
        raise SystemExit(f"unknown preset {preset!r}; choose from {', '.join(PRESETS)}")
    spec = build_spec(PRESETS[preset])
    if "--manifest" in sys.argv:
        print(manifest_markdown(spec))
    elif "--json" in sys.argv:
        print(json.dumps(spec, indent=2))
    else:
        t = spec["totals"]
        print(f"{t['part_count']} parts, {t['printed_part_count']} printed, "
              f"~{t['filament_grams_est']:.0f} g filament")
        for w in spec["warnings"]:
            print(f"  [{w['severity']}] {w['topic']}: {w['text']}")
