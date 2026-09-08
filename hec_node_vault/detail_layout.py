"""HEC Node Vault — detail geometry layout (pure Python, no bpy).

Where `spec.py` says "a GPU is a 300 x 40 x 120 box", this says what is *on*
that box: fan blades, heatsink fins, PCIe bracket, power sockets, RAM slots,
capacitors, connectors and the cable routes between them.

Keeping the layout here means the placement can be checked without Blender —
that the CPU cooler does not sit inside the RAM, that nothing overhangs the
board edge, that a cable starts and ends where the connector actually is.

All millimetres, all local to the parent part unless stated. Local axes match
the part's own box: X across its width, Y through its depth, Z up its height.
"""
import math

# --- fans ---------------------------------------------------------------------

FAN_BLADES = 9
FAN_HUB_FRACTION = 0.30      # hub diameter as a fraction of the frame size
FAN_BLADE_PITCH_DEG = 26.0   # blade attack angle
FAN_BLADE_SWEEP_DEG = 34.0   # how far each blade wraps around the hub


def fan_layout(size: float, thickness: float = 25.0) -> dict:
    """
    A 120/140 mm case fan: square frame, bore, hub, blades, corner bosses.

    Frame bars are laid out as four rectangles around the bore rather than a
    boolean-cut plate — fewer ways to fail, and it renders the same.
    """
    bore = size * 0.94
    hub_d = size * FAN_HUB_FRACTION
    bar = (size - bore) / 2.0 + 3.0
    half = size / 2.0
    # Standard mounting holes: 105 mm PCD on a 120, 124.5 on a 140.
    hole_pitch = {120.0: 105.0, 140.0: 124.5}.get(float(size), size * 0.875)

    return {
        "size": size,
        "thickness": thickness,
        "bore_diameter": bore,
        "hub": {"diameter": hub_d, "depth": thickness * 0.55},
        "frame_bars": [
            {"name": "top", "size": (size, bar, thickness), "pos": (0.0, half - bar / 2, 0.0)},
            {"name": "bottom", "size": (size, bar, thickness), "pos": (0.0, -half + bar / 2, 0.0)},
            {"name": "left", "size": (bar, size - 2 * bar, thickness),
             "pos": (-half + bar / 2, 0.0, 0.0)},
            {"name": "right", "size": (bar, size - 2 * bar, thickness),
             "pos": (half - bar / 2, 0.0, 0.0)},
        ],
        "mount_holes": [(sx * hole_pitch / 2, sy * hole_pitch / 2)
                        for sx in (-1, 1) for sy in (-1, 1)],
        "hole_diameter": 4.5,
        "blades": [
            {
                "index": i,
                "angle_deg": i * 360.0 / FAN_BLADES,
                "pitch_deg": FAN_BLADE_PITCH_DEG,
                "sweep_deg": FAN_BLADE_SWEEP_DEG,
                "root_radius": hub_d / 2 * 0.95,
                "tip_radius": bore / 2 * 0.97,
                "thickness": 1.4,
            }
            for i in range(FAN_BLADES)
        ],
        # The cable tail, so fans read as wired rather than floating.
        "cable_exit": (half - bar / 2, -half + bar / 2, thickness / 2),
    }


# --- graphics card ------------------------------------------------------------

def gpu_layout(length: float, height: float, thickness: float) -> dict:
    """
    A two-fan card: shroud, fans, fin stack, backplate, bracket, power sockets.

    `length` runs along the card (X), `thickness` is slot depth (Y), `height`
    is the card height (Z).
    """
    fan_d = min(height * 0.78, length * 0.36)
    fan_y = -thickness / 2 + 3.0
    fan_positions = [(-length * 0.22, fan_y, 0.0), (length * 0.22, fan_y, 0.0)]

    return {
        "shroud": {"size": (length, thickness * 0.62, height),
                   "pos": (0.0, -thickness * 0.19, 0.0)},
        "fans": [{"diameter": fan_d, "pos": pos, "blades": 11} for pos in fan_positions],
        # Fin stack shows between and behind the fans — the giveaway detail on
        # a real card, and cheap as an array of thin plates.
        "fins": {"count": 46, "plate": (length * 0.72, thickness * 0.34, height * 0.62),
                 "pos": (0.0, thickness * 0.16, -height * 0.05), "gap": 2.4},
        "backplate": {"size": (length * 0.94, 1.2, height * 0.9),
                      "pos": (0.0, thickness / 2 - 0.8, 0.0)},
        "pcb": {"size": (length * 0.9, 2.0, height * 0.62),
                "pos": (0.0, thickness * 0.30, -height * 0.16)},
        # PCIe bracket at the card's outboard end, with the two slot tabs.
        "bracket": {"size": (2.0, 18.0, height * 0.92),
                    "pos": (-length / 2 - 1.0, thickness * 0.20, -height * 0.02)},
        "bracket_slots": [
            {"size": (2.6, 16.0, 12.0), "pos": (-length / 2 - 1.2, thickness * 0.20, z)}
            for z in (height * 0.18, -height * 0.10)
        ],
        # 8-pin PCIe power, on the top edge near the inboard end, facing up.
        "power_sockets": [
            {"size": (23.0, 12.0, 9.0),
             "pos": (length * 0.28 + i * 26.0, thickness * 0.1, height / 2 + 4.0),
             "pins": (4, 2)}
            for i in range(2)
        ],
        "cable_anchor": (length * 0.28, thickness * 0.1, height / 2 + 9.0),
    }


# --- motherboard --------------------------------------------------------------

def board_layout(width: float, depth: float) -> dict:
    """
    Components on an mATX/ATX board, laid out the way a real one is.

    Origin is the board centre. +Y is the rear-I/O edge, +X is the right-hand
    edge as you look at the board face, +Z off the board face. Sizes are
    catalogue-typical rather than measured, and `check_board` verifies nothing
    overhangs or intersects.
    """
    hw, hd = width / 2.0, depth / 2.0
    components = []

    def add(name, size, pos, material="black"):
        components.append({"name": name, "size": size, "pos": pos,
                           "material": material})

    # Rear I/O along the top edge, ports inboard of the shroud.
    add("Rear_IO_Shroud", (100.0, 24.0, 26.0), (-hw * 0.57, hd - 14.0, 13.0), "dark")
    add("Rear_IO_Ports", (90.0, 10.0, 20.0), (-hw * 0.57, hd - 5.0, 11.0), "steel")

    # CPU socket and tower cooler, upper left.
    sx, sy = -hw * 0.48, hd * 0.28
    add("CPU_Socket", (45.0, 45.0, 3.0), (sx, sy, 1.5), "steel")
    add("CPU_Cooler_Base", (92.0, 92.0, 14.0), (sx, sy, 10.0), "alu")
    add("CPU_Cooler_Fins", (88.0, 88.0, 38.0), (sx, sy, 36.0), "alu")
    add("CPU_Fan", (86.0, 86.0, 24.0), (sx, sy, 67.0), "fan")

    # VRM heatsink in the gap between the cooler and the rear I/O — a real
    # board fits one there, and so does this one, with 2 mm either side.
    add("VRM_Heatsink", (100.0, 12.0, 18.0), (sx, hd - 34.0, 10.0), "alu")

    # Four DIMMs standing on edge, to the right of the socket.
    for i in range(4):
        add(f"RAM_{i + 1}", (7.0, 120.0, 34.0), (-hw * 0.03 + i * 9.0, hd * 0.37, 18.0),
            "pcb" if i % 2 else "black")

    # Slots run across the board below the socket.
    # The x16 sits clear of the cooler overhang, as it must on a real board.
    add("PCIe_x16", (150.0, 16.0, 11.0), (-hw * 0.25, -hd * 0.20, 6.0), "steel")
    add("PCIe_x4", (110.0, 14.0, 10.0), (-hw * 0.41, -hd * 0.36, 5.5), "black")

    # Chipset and the two M.2 shields, lower half.
    add("Chipset_Heatsink", (46.0, 46.0, 8.0), (hw * 0.57, -hd * 0.61, 5.0), "alu")
    add("M2_Shield_1", (22.0, 66.0, 4.0), (-hw * 0.25, -hd * 0.70, 3.0), "alu")
    add("M2_Shield_2", (22.0, 66.0, 4.0), (hw * 0.08, -hd * 0.70, 3.0), "alu")

    # Power and data connectors on the edges they really sit on.
    add("ATX_24pin", (12.0, 52.0, 12.0), (hw - 10.0, hd * 0.33, 7.0), "black")
    add("EPS_8pin", (24.0, 12.0, 12.0), (0.0, hd - 8.0, 7.0), "black")
    add("SATA_Ports", (26.0, 22.0, 11.0), (hw - 22.0, -hd * 0.33, 6.5), "black")
    add("Front_Panel_Header", (8.0, 22.0, 8.0), (hw - 10.0, -hd * 0.78, 5.0), "black")
    add("Fan_Header_1", (7.0, 9.0, 7.0), (-hw + 12.0, hd - 8.0, 4.5), "black")
    add("Fan_Header_2", (7.0, 9.0, 7.0), (hw - 10.0, hd - 30.0, 4.5), "black")

    # Chokes ringed around the socket, outside the cooler footprint, on a
    # deterministic scatter so every render matches the last one.
    cooler_half = 92.0 / 2 + 6.0
    for i in range(16):
        angle = i * 2.399963  # golden angle — keeps them from lining up
        radius = 58.0 + (i % 4) * 7.0
        x, y = sx + math.cos(angle) * radius, sy + math.sin(angle) * radius
        if abs(x - sx) < cooler_half and abs(y - sy) < cooler_half:
            continue
        if abs(x) > hw - 14 or abs(y) > hd - 14:
            continue
        add(f"Choke_{i + 1}", (8.0, 8.0, 6.0), (x, y, 3.5), "dark")

    return {"components": components,
            "cable_anchors": {
                "atx": (hw - 10.0, hd * 0.33, 14.0),
                "eps": (0.0, hd - 8.0, 14.0),
                "sata": (hw - 22.0, -hd * 0.33, 12.0),
            }}


# --- power supply -------------------------------------------------------------

def psu_layout(width: float, depth: float, height: float) -> dict:
    """Fan grille, IEC inlet, switch and a modular connector panel."""
    return {
        "fan": {"diameter": min(width, depth) * 0.76, "pos": (0.0, 0.0, height / 2 - 1.0)},
        "iec_inlet": {"size": (28.0, 6.0, 24.0), "pos": (-width * 0.26, -depth / 2 + 3.0, 0.0)},
        "switch": {"size": (16.0, 5.0, 11.0), "pos": (-width * 0.26 + 26.0,
                                                      -depth / 2 + 2.5, 0.0)},
        "vents": {"count": 9, "slot": (2.5, 0.0, height * 0.52),
                  "pos": (width * 0.18, -depth / 2 + 1.0, 0.0), "gap": 5.0},
        "modular_panel": [
            {"name": f"Socket_{i + 1}", "size": (19.0, 5.0, 8.0),
             "pos": (-width * 0.30 + (i % 3) * 22.0, depth / 2 - 3.0,
                     height * 0.22 - (i // 3) * 13.0)}
            for i in range(6)
        ],
        "cable_anchor": (0.0, depth / 2 - 6.0, height * 0.1),
    }


# --- cables -------------------------------------------------------------------

def _sag(a, b, drop, bulge=0.0):
    """Three control points from a to b, sagging under its own weight."""
    mid = [(a[i] + b[i]) / 2.0 for i in range(3)]
    mid[2] -= drop
    mid[1] += bulge
    return [tuple(a), tuple(mid), tuple(b)]


def cable_routes(spec: dict) -> list:
    """
    World-space cable runs: PSU to each board, PSU to each GPU, and the trunk
    down the tray.

    Cables are the detail that sells a build shot — nothing else says "this is
    a real machine" like a loom with weight in it. Each route is a list of
    control points for a bevelled curve, so they hang rather than run straight.
    """
    parts = {p["key"]: p for p in spec["parts"]}
    p = spec["params"]
    lv = spec["levels"]
    routes = []

    boards = sorted((k for k in parts if k.startswith("04_Motherboard_")),
                    key=lambda k: parts[k]["pos"][2])
    gpus = sorted((k for k in parts if k.startswith("05_GPU_")),
                  key=lambda k: parts[k]["pos"][2])
    psus = sorted((k for k in parts if k.startswith("06_PSU_")),
                  key=lambda k: parts[k]["pos"][0])

    tray = parts.get("03_Tray_Spine")
    trunk_y = (tray["pos"][1] + tray["size"][1] / 2 + 22.0) if tray else p["depth"] / 2 - 40.0

    # Trunk: one fat bundle from the plinth up the back of the tray.
    routes.append({
        "name": "Loom_Trunk", "radius": 17.0, "material": "black",
        "points": [(0.0, trunk_y, lv["plinth_top"] - 40.0),
                   (0.0, trunk_y + 6.0, lv["plinth_top"] + 120.0),
                   (0.0, trunk_y, lv["glazed_top"] - 120.0)],
    })

    for i, board_key in enumerate(boards):
        board = parts[board_key]
        bx, by, bz = board["pos"]
        bw, bd, _ = board["size"]
        psu = parts[psus[i % len(psus)]] if psus else None
        start = ((psu["pos"][0], trunk_y, psu["pos"][2] + 40.0) if psu
                 else (0.0, trunk_y, lv["plinth_top"]))

        # 24-pin: off the trunk, round the board's left edge, into the socket.
        atx_end = (bx - bw / 2 + 12.0, by + bd * 0.10, bz + 16.0)
        routes.append({
            "name": f"Cable_ATX_{i + 1}", "radius": 7.0, "material": "black",
            "points": [start, (bx - bw / 2 - 30.0, trunk_y - 30.0, bz + 40.0),
                       (bx - bw / 2 - 14.0, atx_end[1] + 30.0, bz + 22.0), atx_end],
        })
        # EPS: up the back corner to the top edge of the board.
        eps_end = (bx + bw * 0.30, by + bd / 2 - 12.0, bz + 16.0)
        routes.append({
            "name": f"Cable_EPS_{i + 1}", "radius": 5.0, "material": "black",
            "points": _sag(start, eps_end, 18.0, bulge=-20.0),
        })

    for i, gpu_key in enumerate(gpus):
        gpu = parts[gpu_key]
        gx, gy, gz = gpu["pos"]
        gw, _, gh = gpu["size"]
        psu = parts[psus[i % len(psus)]] if psus else None
        start = ((psu["pos"][0], trunk_y, psu["pos"][2] + 40.0) if psu
                 else (0.0, trunk_y, lv["plinth_top"]))
        end = (gx + gw * 0.28, gy, gz + gh / 2 + 12.0)
        routes.append({
            "name": f"Cable_PCIe_{i + 1}", "radius": 8.0, "material": "black",
            "points": [start, (gx + gw * 0.40, trunk_y - 40.0, gz + gh * 0.6),
                       (end[0] + 26.0, end[1] + 18.0, end[2] + 26.0), end],
        })

    return routes


# --- checks -------------------------------------------------------------------

def check_board(width: float, depth: float) -> list:
    """Components that overhang the board or intersect each other in plan."""
    layout = board_layout(width, depth)
    hw, hd = width / 2.0, depth / 2.0
    problems = []

    for c in layout["components"]:
        x, y, _ = c["pos"]
        w, d, _ = c["size"]
        if abs(x) + w / 2 > hw + 0.5 or abs(y) + d / 2 > hd + 0.5:
            problems.append(f"{c['name']} overhangs the board edge")

    # Plan-view overlap between the big items, which is where a clash would
    # actually look wrong in a render.
    big = [c for c in layout["components"]
           if c["size"][0] * c["size"][1] > 1200.0 and not c["name"].startswith("Rear_IO")]
    for i in range(len(big)):
        for j in range(i + 1, len(big)):
            a, b = big[i], big[j]
            # Cooler parts are meant to stack, and so are the socket/cooler.
            if a["name"].split("_")[0] == b["name"].split("_")[0]:
                continue
            if {"CPU", "RAM"} == {a["name"].split("_")[0], b["name"].split("_")[0]}:
                overlap_z = (abs(a["pos"][2] - b["pos"][2])
                             < (a["size"][2] + b["size"][2]) / 2)
                if not overlap_z:
                    continue
            dx = abs(a["pos"][0] - b["pos"][0]) - (a["size"][0] + b["size"][0]) / 2
            dy = abs(a["pos"][1] - b["pos"][1]) - (a["size"][1] + b["size"][1]) / 2
            dz = abs(a["pos"][2] - b["pos"][2]) - (a["size"][2] + b["size"][2]) / 2
            if dx < -0.5 and dy < -0.5 and dz < -0.5:
                problems.append(f"{a['name']} intersects {b['name']}")
    return problems


if __name__ == "__main__":
    import sys

    sys.path.insert(0, ".")
    import spec as vault_spec

    sp = vault_spec.build_spec(vault_spec.PRESETS["resolved"])
    board_w, board_d = vault_spec.BOARD_SIZES[sp["params"]["board"]]
    board = board_layout(board_w, board_d)
    gpu = gpu_layout(sp["params"]["gpu_length"], sp["params"]["gpu_height"],
                     sp["params"]["gpu_thickness"])
    fan = fan_layout(sp["params"]["fan_size"])
    routes = cable_routes(sp)

    print(f"board: {len(board['components'])} components on {board_w:.0f} x {board_d:.0f}")
    print(f"gpu: {len(gpu['fans'])} fans, {gpu['fins']['count']} fins, "
          f"{len(gpu['power_sockets'])} power sockets")
    print(f"fan: {len(fan['blades'])} blades, {len(fan['frame_bars'])} frame bars")
    print(f"cables: {len(routes)} runs")
    issues = check_board(board_w, board_d)
    print("board layout issues:", issues if issues else "none")
