"""HEC Node Vault — fabrication manifest and assembly sequence.

Everything that gets *made*: 3D-printed parts (from `spec.py`) plus the cut
parts — glass, sheet metal, extrusion, tray, mesh — with a cut list, the
bought-in fixings, and the build sequence.

Pure Python, no bpy. Run it directly:

    python3 fabrication.py --preset resolved              # summary
    python3 fabrication.py --preset resolved --manifest    # full fabrication manifest
    python3 fabrication.py --preset resolved --assembly    # step-by-step build
    python3 fabrication.py --preset resolved --json        # both, as JSON
"""
import json
import sys

import spec as vault_spec

# Processes, in the order a shop would schedule them.
PROCESSES = [
    ("order", "Order in (supplier cuts it)"),
    ("saw", "Saw cut + deburr"),
    ("sheet", "Sheet metal — laser/CNC cut and fold"),
    ("drill", "Drill / punch"),
    ("print", "3D print"),
    ("misc", "Cut by hand"),
]


def _cut(key, name, process, material, size, qty, note="", tool="", stock=""):
    return {"key": key, "name": name, "process": process, "material": material,
            "size": [round(v, 1) for v in size], "qty": qty, "note": note,
            "tool": tool, "stock": stock}


def cut_parts(spec: dict) -> list:
    """Everything fabricated that is not 3D printed."""
    p = spec["params"]
    lv = spec["levels"]
    W, D = p["width"], p["depth"]
    gt = p["glass_thickness"]
    glazed = lv["glazed_height"]
    out = []

    # --- glass ---------------------------------------------------------------
    # Panes sit inside the corner posts, so deduct the post section from each.
    pane_w = W - 2 * p["post_size"]
    pane_d = D - 2 * p["post_size"]
    out.append(_cut("G01", "Glass pane — front", "order", p["glass_material"],
                    (pane_w, glazed, gt), 1,
                    note="Tinted. Specify ALL holes and cutouts before toughening — "
                         "tempered glass cannot be drilled or cut afterwards.",
                    stock="supplier cut to size, edges polished, corners eased"))
    out.append(_cut("G02", "Glass pane — rear", "order", p["glass_material"],
                    (pane_w, glazed, gt), 1,
                    note="Consider a perforated metal rear panel instead, to let the "
                         "chimney cross-flow; it is the face nobody sees.",
                    stock="supplier cut to size"))
    out.append(_cut("G03", "Glass pane — side", "order", p["glass_material"],
                    (pane_d, glazed, gt), 2, note="Left and right, identical.",
                    stock="supplier cut to size"))
    out.append(_cut("G04", "Glass pane — top", "order", p["glass_material"],
                    (pane_w, pane_d, gt), 1,
                    note="Sits under the top cap; carries no load but must be toughened "
                         "— anything above head height that can fall is safety glass.",
                    stock="supplier cut to size"))

    # --- sheet metal ---------------------------------------------------------
    out.append(_cut("S01", "Plinth side panel", "sheet", "1.5 mm aluminium or steel",
                    (W, p["plinth_height"], 1.5), 2,
                    note="Front panel carries the intake cutouts; rear is plain.",
                    tool="laser cut + press brake", stock="1.5 mm sheet"))
    out.append(_cut("S02", "Plinth end panel", "sheet", "1.5 mm aluminium or steel",
                    (D, p["plinth_height"], 1.5), 2, tool="laser cut + press brake",
                    stock="1.5 mm sheet"))
    out.append(_cut("S03", "Plinth deck (top)", "sheet", "2 mm aluminium or steel",
                    (W, D, 2.0), 1,
                    note="Takes the tray load and the node stack above it — 2 mm, and "
                         "add a folded return on all four edges for stiffness.",
                    tool="laser cut + press brake", stock="2 mm sheet"))
    out.append(_cut("S04", "Plinth floor", "sheet", "2 mm aluminium or steel",
                    (W, D, 2.0), 1, note="Caster mounts land here; see the caster pads.",
                    tool="laser cut", stock="2 mm sheet"))
    out.append(_cut("S05", "Top cap side panel", "sheet", "1.5 mm aluminium",
                    (W, p["top_cap_height"], 1.5), 2, tool="laser cut + press brake",
                    stock="1.5 mm sheet"))
    out.append(_cut("S06", "Top cap end panel", "sheet", "1.5 mm aluminium",
                    (D, p["top_cap_height"], 1.5), 2, tool="laser cut + press brake",
                    stock="1.5 mm sheet"))
    out.append(_cut("S07", "Top vent grille", "sheet", "1.5 mm perforated aluminium",
                    (W - 20.0, D - 20.0, 1.5), 1,
                    note="Perforated stock from photos 1 & 2. Open area matters more "
                         "than hole size — aim for 40%+ or it strangles the fans.",
                    tool="laser cut to outline", stock="perforated sheet"))
    fan = p["fan_size"]
    out.append(_cut("S08", "Fan mounting plate", "sheet", "1.5 mm aluminium",
                    (fan + 30.0, fan + 30.0, 1.5),
                    int(p["exhaust_fans"] + p["intake_fans"]),
                    note=f"{fan:.0f} mm bore plus a 105 mm PCD of M4 clearance holes. "
                         f"The punched knockout plate in photo 3 is the same idea.",
                    tool="laser cut", stock="1.5 mm sheet"))

    # --- extrusion and tray --------------------------------------------------
    post_len = glazed + 20.0
    out.append(_cut("E01", "Corner post", "saw",
                    f"{p['post_size']:.0f} x {p['post_size']:.0f} aluminium extrusion",
                    (p["post_size"], p["post_size"], post_len), 4,
                    note="Cut all four in one setup so they are identical — any "
                         "difference here twists the whole case.",
                    tool="mitre saw, metal blade", stock="2 m lengths"))
    tray_len = lv["glazed_top"] - lv["plinth_top"]
    out.append(_cut("T01", "Cable tray — spine", "saw",
                    f"{p['tray_width']:.0f} mm ladder tray",
                    (p["tray_width"], p["tray_depth"], tray_len), 1,
                    note="Cut to length, deburr every cut strand, fit the printed end "
                         "caps. Galvanised swarf in a running machine is a short circuit waiting to happen.",
                    tool="angle grinder or tray cutter", stock="3 m length"))
    out.append(_cut("T02", "Tray rung", "saw", "tray rung stock or 25 x 3 flat bar",
                    (p["tray_width"], 25.0, 3.0), int(p["node_count"]),
                    note="Only if the tray's own rung pitch does not match the tier "
                         "pitch — measure before ordering.",
                    tool="mitre saw", stock="from offcut"))

    # --- misc ----------------------------------------------------------------
    out.append(_cut("M01", "Dust filter mesh", "misc", "nylon or aluminium mesh",
                    (fan + 20.0, fan + 20.0, 1.0), int(p["intake_fans"]),
                    note="Cut squares to drop into the printed filter frames.",
                    tool="scissors / tin snips", stock="mesh sheet"))
    out.append(_cut("M02", "LED strip run", "misc", "addressable RGB strip",
                    (10.0, 5.0, glazed - 20.0), 4,
                    note="Cut on the marked cut lines only, one run per corner post.",
                    tool="scissors", stock="5 m reel"))
    return out


def fixings(spec: dict) -> list:
    """Bought-in hardware. Not fabricated, but the build stops without it."""
    p = spec["params"]
    n = int(p["node_count"])
    fan_count = int(p["exhaust_fans"] + p["intake_fans"])
    return [
        {"key": "F01", "name": "M6 x 20 bolt, nut, washers", "qty": 8 * n,
         "note": "Sled rails and clamps to the tray rungs."},
        {"key": "F02", "name": "M3 x 6 motherboard standoff + screw", "qty": 9 * n,
         "note": "Boards to the printed sled rails."},
        {"key": "F03", "name": "M4 x 30 fan screw", "qty": 4 * fan_count,
         "note": "Fans through shroud into the mounting plate."},
        {"key": "F04", "name": "M5 T-nut + button head for extrusion", "qty": 32,
         "note": "Corner brackets to the posts."},
        {"key": "F05", "name": "M8 x 25 bolt + nyloc", "qty": 16,
         "note": "Casters and plinth corners."},
        {"key": "F06", "name": "75 mm lockable caster, 50 kg+ rated", "qty": 4,
         "note": "All four braked, not two — the mass is high."},
        {"key": "F07", "name": "Earth bonding lug + 4 mm² green/yellow", "qty": 2,
         "note": "Tray and chassis to earth. Non-negotiable on a conductive frame "
                 "holding mains gear."},
        {"key": "F08", "name": "IEC PDU, switched, with C13 outlets", "qty": 1,
         "note": f"One ingress for {n} PSUs. Sequenced switching if you can get it."},
        {"key": "F09", "name": "Glass suction cup (pair)", "qty": 1,
         "note": "Hire or buy — do not hand-carry a 5 kg pane into a slot."},
        {"key": "F10", "name": "Closed-cell foam glazing tape, 3 mm", "qty": 1,
         "note": "Between glass and any metal. Glass on bare metal cracks."},
        {"key": "F11", "name": "VESA M4 screw set", "qty": 1,
         "note": "Display to the printed adapter plate."},
        {"key": "F12", "name": "Cable ties / hook-and-loop", "qty": 1,
         "note": "Loom dressing down the tray."},
    ]


# --- assembly sequence --------------------------------------------------------

def assembly_steps(spec: dict) -> list:
    """
    The build order, written so each step ends somewhere you can safely stop.

    Deliberately puts the smoke test before the glass goes on: everything is
    reachable until the panes are in, and nothing is reachable afterwards.
    """
    p = spec["params"]
    n = int(p["node_count"])
    fan = int(p["fan_size"])
    # Derive the electrical figures rather than hardcoding them: they change
    # with the preset's node count and with the POWER assumptions.
    power = vault_spec.POWER
    total_w = ((power["cpu"] + power["gpu"] + power["board_and_drives"]) * n
               / power["psu_efficiency"])
    amps = total_w / 230.0
    steps = [
        {
            "no": 1, "title": "Measure and check before you make anything",
            "shows": [],
            "time": "1 h",
            "parts": [], "cut": [], "tools": ["tape", "vernier", "the measure sheet"],
            "detail": [
                "Measure the real tray: ladder width, rung pitch, rung hole spacing.",
                "Measure a board (mount-hole pattern), the longest GPU, and a PSU.",
                "Check those against spec.py's parameters and re-run it if they differ.",
                "Only then order the glass — it is the long-lead, non-returnable item.",
            ],
            "warning": "Every dimension in this model came from the brief, not from "
                       "your hardware. This step is what makes the rest true.",
        },
        {
            "no": 2, "title": "Cut the spine",
            "shows": ['03_Tray_Spine', '03_Tray_Rung_*', 'P11_*'],
            "time": "1 h",
            "parts": ["P11_Tray_Cap_Bottom", "P11_Tray_Cap_Top"],
            "cut": ["T01", "T02"],
            "tools": ["tray cutter or grinder", "file", "vacuum"],
            "detail": [
                "Cut the tray to length and deburr every cut strand.",
                "If the tray's rung pitch does not match the tier pitch, add rungs.",
                "Fit the printed end caps top and bottom.",
            ],
            "warning": "Vacuum the swarf now. Galvanised filings inside a running "
                       "machine are a short waiting to happen.",
        },
        {
            "no": 3, "title": "Build the plinth",
            "shows": ['02_Plinth_Shell', '14_Caster_*', '09_Intake_Fan_*', 'P12_*', 'P05_Fan_Shroud_In_*', 'P14_*'],
            "time": "3 h",
            "parts": ["P12_Caster_Pad_*", "P05_Fan_Shroud_In_*", "P14_Filter_Frame_*"],
            "cut": ["S01", "S02", "S03", "S04", "S08", "M01"],
            "tools": ["drill", "rivet gun or M5 hardware", "deburring tool"],
            "detail": [
                "Assemble the folded shell: sides, ends, floor, deck.",
                "Bolt the printed caster pads through the floor, then the casters.",
                f"Fit the {int(p['intake_fans'])} intake fan plates, shrouds and filter frames.",
                "Stand it up and check it does not rock before anything goes on top.",
            ],
        },
        {
            "no": 4, "title": "Power in the plinth",
            "shows": ['06_PSU_*', 'P04_*'],
            "time": "2 h",
            "parts": ["P04_PSU_Cradle_*"],
            "cut": [], "tools": ["crimpers", "multimeter", "label maker"],
            "detail": [
                f"Drop the {n} PSUs into their printed cradles.",
                "Mount the PDU; run one mains lead in through a grommeted entry.",
                "Bond the chassis and the tray to earth, and test continuity.",
                "Label every outlet to its node before the cables disappear upward.",
            ],
            "warning": (f"Your trade, your call — but the model's own check puts this "
                        f"at ~{amps:.1f} A ({total_w:.0f} W) at full tilt on {n} nodes. "
                        f"Dedicated circuit, Type C RCBO for the inrush, and stagger "
                        f"the PSU starts if you can."),
        },
        {
            "no": 5, "title": "Stand the spine",
            "shows": [],
            "time": "1 h",
            "parts": [], "cut": ["T01"], "tools": ["spirit level", "spanner set"],
            "detail": [
                "Bolt the tray to the plinth deck, plumb in both axes.",
                "Check it for rack under hand load — everything above hangs off this.",
            ],
        },
        {
            "no": 6, "title": "Posts and corners",
            "shows": ['P08_*'],
            "time": "2 h",
            "parts": ["P08_Corner_Bracket_*"],
            "cut": ["E01"],
            "tools": ["mitre saw", "hex keys", "square"],
            "detail": [
                "Cut the four posts in one setup so they are identical.",
                "Fit printed corner brackets top and bottom on each post.",
                "Stand the posts on the plinth and square the frame diagonally.",
            ],
            "warning": "Measure both diagonals and make them equal. Out of square here "
                       "means the glass will not drop into its slots later.",
        },
        {
            "no": 7, "title": "Node sleds",
            "shows": ['04_Motherboard_*', 'P01_*', 'P02_*'],
            "time": f"{max(2, n)} h",
            "parts": ["P01_Node_Rail_*", "P02_Rung_Clamp_*"],
            "cut": [], "tools": ["hex keys", "M3 driver"],
            "detail": [
                "Assemble a pair of printed rails per tier with their rung clamps.",
                f"Clamp each sled to its rung, working bottom to top ({n} tiers).",
                "Fit M3 standoffs to match the board pattern, then the boards.",
                "Leave the top tier until last — it is your access for everything else.",
            ],
        },
        {
            "no": 8, "title": "GPUs and cradles",
            "shows": ['05_GPU_*', 'P03_*'],
            "time": "1 h",
            "parts": ["P03_GPU_Cradle_*"],
            "cut": [], "tools": ["hex keys"],
            "detail": [
                "Fit each card on its riser, then slide the printed cradle under the "
                "far end and shim it until the card sits level.",
                "Check the card clears the tier above with the fans spinning.",
            ],
            "warning": "The cradle must carry the card's weight, not preload the slot. "
                       "Snug, not jacked up.",
        },
        {
            "no": 9, "title": "Loom and dress",
            "shows": ['13_Cable_Loom', 'P09_*'],
            "time": "3 h",
            "parts": ["P09_Cable_Comb_*"],
            "cut": [], "tools": ["cable ties", "side cutters"],
            "detail": [
                "Run each node's PSU leads down the rear of the tray.",
                "Clip the printed combs on and dress the loom into lanes.",
                "Add strain relief at each board so the loom's weight never hangs off "
                "a connector.",
                "Leave a service loop per tier so a node can slide out powered down.",
            ],
        },
        {
            "no": 10, "title": "Smoke test — before any glass goes on",
            "shows": [],
            "time": "2 h",
            "parts": [], "cut": [], "tools": ["multimeter", "IR thermometer", "a fire "
                                              "blanket, honestly"],
            "detail": [
                "Power one node. Confirm POST, then shut down.",
                "Repeat per node, then run all of them together for an hour.",
                "Watch the PDU load and feel for hot spots at the top tier.",
            ],
            "warning": "Do not skip the order here. Everything is reachable now and "
                       "nothing is reachable once the panes are in.",
        },
        {
            "no": 11, "title": "Lighting",
            "shows": ['10_LED_Strip_*', 'P06_*'],
            "time": "2 h",
            "parts": ["P06_LED_Diffuser_*"],
            "cut": ["M02"],
            "tools": ["soldering iron", "isopropyl"],
            "detail": [
                "Clean the posts, run one strip per corner, feed to the controller.",
                "Clip the printed diffusers over the strips.",
                "Test the full run before the glass traps it.",
            ],
        },
        {
            "no": 12, "title": "Top cap and exhaust",
            "shows": ['08_Top_Cap_Shell', '07_Exhaust_Fan_*', '08_Top_Vent_Grille', 'P05_Fan_Shroud_Ex_*'],
            "time": "2 h",
            "parts": ["P05_Fan_Shroud_Ex_*"],
            "cut": ["S05", "S06", "S07", "S08"],
            "tools": ["drill", "hex keys"],
            "detail": [
                f"Assemble the cap; fit the {int(p['exhaust_fans'])} x {fan} mm fans "
                f"into their shrouds and plates.",
                "Fit the perforated grille.",
                "Sit the cap on the posts but leave it loose — the top pane goes in first.",
            ],
        },
        {
            "no": 13, "title": "Glazing — two people",
            "shows": ['01_Glass_*', 'P07_*'],
            "time": "3 h",
            "parts": ["P07_Glass_Clip_*"],
            "cut": ["G01", "G02", "G03", "G04"],
            "tools": ["suction cups", "glazing tape", "gloves"],
            "detail": [
                "Run foam glazing tape in every channel first.",
                "Top pane, then rear, then the two sides, then the front.",
                "Fit the printed clips as you go; they should grip, not clamp.",
                "Bolt the top cap down once the top pane is captive.",
            ],
            "warning": "Two people and suction cups. A 5 kg toughened pane that touches "
                       "bare metal or gets dropped an inch is gone, and it is the "
                       "longest-lead part in the build.",
        },
        {
            "no": 14, "title": "Display",
            "shows": ['11_Telemetry_Display', '12_Monitor_Arm', 'P10_*', 'P13_*'],
            "time": "1 h",
            "parts": ["P10_VESA_Plate", "P13_Bezel_*"],
            "cut": [], "tools": ["hex keys", "VESA screws"],
            "detail": [
                "Mount the arm to the top rail, then the printed VESA plate.",
                "Hang the display, join the bezel segments around it.",
                "Route the display feed down inside the rear post.",
            ],
        },
        {
            "no": 15, "title": "Commission",
            "shows": [],
            "time": "2 h",
            "parts": [], "cut": [],
            "tools": ["IR thermometer", "logging on the telemetry display"],
            "detail": [
                "Set fan curves from the node telemetry, not fixed RPM.",
                "Soak test at full load for two hours; log the top-tier temperature.",
                "Add a fan-fail and over-temp alarm to the dashboard.",
                "Label the circuit at the board, and photograph the loom before "
                "you forget how it goes back.",
            ],
            "warning": "If the top tier runs hot at soak, that is the thermal check "
                       "coming true — add exhausts before you add nodes.",
        },
    ]
    return steps


def _match(pattern: str, keys) -> list:
    if pattern.endswith("*"):
        stem = pattern[:-1]
        return sorted(k for k in keys if k.startswith(stem))
    return [pattern] if pattern in keys else []


def resolve_step_parts(spec: dict, step: dict) -> list:
    """Expand the wildcards in a step's part list to real part keys."""
    keys = {part["key"] for part in spec["parts"]}
    resolved = []
    for pattern in step.get("parts", []):
        resolved.extend(_match(pattern, keys))
    return resolved


def parts_visible_at(spec: dict, step_no: int) -> list:
    """
    Model parts installed by the end of `step_no` — cumulative, so rendering the
    sequence shows the machine growing rather than a disconnected part per slide.
    """
    keys = {part["key"] for part in spec["parts"]}
    visible = []
    for step in assembly_steps(spec):
        if step["no"] > step_no:
            break
        for pattern in step.get("shows", []):
            visible.extend(_match(pattern, keys))
    return sorted(set(visible))


def coverage(spec: dict) -> dict:
    """Model parts never shown by any step — a gap in the sequence."""
    keys = {part["key"] for part in spec["parts"]}
    shown = set(parts_visible_at(spec, 99))
    return {"total": len(keys), "shown": len(shown), "missing": sorted(keys - shown)}


def totals(spec: dict) -> dict:
    cuts = cut_parts(spec)
    printed = [q for q in spec["parts"] if q.get("printed")]
    by_process = {}
    for part in cuts:
        by_process[part["process"]] = by_process.get(part["process"], 0) + part["qty"]
    by_process["print"] = len(printed)
    steps = assembly_steps(spec)
    hours = 0.0
    for step in steps:
        text = step.get("time", "0 h").split()[0]
        try:
            hours += float(text)
        except ValueError:
            pass
    return {
        "printed_pieces": len(printed),
        "cut_pieces": sum(part["qty"] for part in cuts),
        "cut_line_items": len(cuts),
        "fixing_line_items": len(fixings(spec)),
        "total_pieces": len(printed) + sum(part["qty"] for part in cuts),
        "by_process": by_process,
        "assembly_steps": len(steps),
        "assembly_hours": round(hours, 1),
    }


# --- rendering ----------------------------------------------------------------

def manifest_markdown(spec: dict) -> str:
    """The full fabrication manifest: printed, cut, and bought."""
    p = spec["params"]
    t = totals(spec)
    printed = [q for q in spec["parts"] if q.get("printed")]
    cuts = cut_parts(spec)

    out = ["# HEC Node Vault — fabrication manifest", "",
           f"HEC-PC-002 Rev A · {p['width']:.0f} x {p['depth']:.0f} x {p['height']:.0f} mm · "
           f"{int(p['node_count'])} nodes · {p['board']} boards · {p['psu_form']} PSUs", "",
           f"**{t['total_pieces']} fabricated pieces**: {t['printed_pieces']} printed, "
           f"{t['cut_pieces']} cut. Plus {t['fixing_line_items']} lines of bought-in "
           f"hardware. Estimated build time {t['assembly_hours']:.0f} h across "
           f"{t['assembly_steps']} steps.", ""]

    # Cut parts, grouped by process.
    out += ["## Cut parts", ""]
    for process, label in PROCESSES:
        rows = [c for c in cuts if c["process"] == process]
        if not rows:
            continue
        out += [f"### {label}", "",
                "| Ref | Part | Qty | Size (mm) | Material | Stock / tooling |",
                "|-----|------|----:|-----------|----------|-----------------|"]
        for row in rows:
            w, d, h = row["size"]
            out.append(f"| {row['key']} | {row['name']} | {row['qty']} | "
                       f"{w:.0f} x {d:.0f} x {h:.1f} | {row['material']} | "
                       f"{row['stock'] or row['tool']} |")
        out.append("")
        for row in rows:
            if row["note"]:
                out.append(f"- **{row['key']}** — {row['note']}")
        out.append("")

    # Printed parts, grouped by family.
    grouped = {}
    for part in printed:
        grouped.setdefault((part["no"], part.get("family") or part["name"]), []).append(part)
    out += ["### 3D printed", "",
            "| Ref | Part | Qty | Size (mm) | Material | Layer | Infill | Support | Status |",
            "|----:|------|----:|-----------|----------|-------|-------:|---------|--------|"]
    for key in sorted(grouped):
        group = grouped[key]
        prof = group[0].get("print_profile", {})
        w, d, h = group[0]["size"]
        out.append(f"| P{key[0] - 100:02d} | {key[1]} | {len(group)} | "
                   f"{w:.0f} x {d:.0f} x {h:.0f} | {prof.get('material', '?')} | "
                   f"{prof.get('layer_mm', '?')} | {prof.get('infill_pct', '?')}% | "
                   f"{'yes' if prof.get('supports') else 'no'} | "
                   f"{'DRAFT' if prof.get('draft') else 'ready'} |")
    out.append("")

    out += ["## Bought in", "",
            "| Ref | Item | Qty | Notes |", "|-----|------|----:|-------|"]
    for item in fixings(spec):
        out.append(f"| {item['key']} | {item['name']} | {item['qty']} | {item['note']} |")
    out.append("")

    out += ["## Checks carried over from the model", ""]
    for w in spec["warnings"]:
        icon = {"blocker": "**BLOCKER**", "major": "**Major**",
                "info": "Note"}.get(w["severity"], w["severity"])
        out.append(f"- {icon} ({w['topic']}): {w['text']}")
    return "\n".join(out) + "\n"


def assembly_markdown(spec: dict) -> str:
    """The build sequence, one section per step."""
    t = totals(spec)
    cuts = {c["key"]: c for c in cut_parts(spec)}
    out = ["# HEC Node Vault — assembly sequence", "",
           f"{t['assembly_steps']} steps, roughly {t['assembly_hours']:.0f} hours of "
           f"work. Each step ends somewhere you can safely walk away.", ""]
    for step in assembly_steps(spec):
        parts = resolve_step_parts(spec, step)
        out += [f"## Step {step['no']} — {step['title']}", "",
                f"*{step['time']}*", ""]
        for line in step["detail"]:
            out.append(f"- {line}")
        if parts:
            out += ["", f"**Printed parts ({len(parts)}):** " + ", ".join(parts)]
        if step.get("cut"):
            named = [f"{k} {cuts[k]['name']}" for k in step["cut"] if k in cuts]
            out += ["", "**Cut parts:** " + "; ".join(named)]
        if step.get("tools"):
            out += ["", "**Tools:** " + ", ".join(step["tools"])]
        if step.get("warning"):
            out += ["", f"> {step['warning']}"]
        out.append("")
    return "\n".join(out) + "\n"


def build(preset: str = "brief") -> dict:
    spec = vault_spec.build_spec(vault_spec.PRESETS[preset])
    return {"spec": spec, "cut_parts": cut_parts(spec), "fixings": fixings(spec),
            "steps": assembly_steps(spec), "totals": totals(spec)}


if __name__ == "__main__":
    preset = "brief"
    if "--preset" in sys.argv:
        preset = sys.argv[sys.argv.index("--preset") + 1]
    if preset not in vault_spec.PRESETS:
        raise SystemExit(f"unknown preset {preset!r}; "
                         f"choose from {', '.join(vault_spec.PRESETS)}")
    data = build(preset)

    if "--manifest" in sys.argv:
        print(manifest_markdown(data["spec"]))
    elif "--assembly" in sys.argv:
        print(assembly_markdown(data["spec"]))
    elif "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        t = data["totals"]
        print(f"{t['total_pieces']} fabricated pieces "
              f"({t['printed_pieces']} printed, {t['cut_pieces']} cut) across "
              f"{t['cut_line_items']} cut line items")
        print(f"{t['assembly_steps']} assembly steps, ~{t['assembly_hours']:.0f} h")
        for process, label in PROCESSES:
            if t["by_process"].get(process):
                print(f"  {label}: {t['by_process'][process]}")
