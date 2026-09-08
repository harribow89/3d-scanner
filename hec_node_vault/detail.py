"""HEC Node Vault — build the detail geometry in Blender.

Replaces the block placeholders with the things that make a render read as a
real machine: fan frames and twisted blades, GPU shrouds with fin stacks and
power sockets, board components down to the chokes, PSU faces, and cables that
hang under their own weight.

Every detail object is parented to its placeholder, so the explode slider still
drives the whole assembly, and detail can be added or skipped without touching
`spec.py` — the fabrication truth stays exactly as it was.

    blender --background -P build_blender.py -- --preset resolved --detail \\
        --render shots/ --views hero,detail

Runs inside Blender only (imports bpy).
"""
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import detail_layout as dl  # noqa: E402

MM = 0.001
PREFIX = "DTL_"


# --- primitives ---------------------------------------------------------------

def _material(name):
    """Reuse the build's materials so studio shading applies to detail too."""
    import build_blender
    return build_blender.get_material(name)


def _link(obj, collection):
    for existing in list(obj.users_collection):
        existing.objects.unlink(obj)
    collection.objects.link(obj)


def _finish(obj, name, material, parent, collection):
    obj.name = PREFIX + name
    obj.data.name = PREFIX + name
    obj.data.materials.append(_material(material))
    _link(obj, collection)
    if parent:
        # Keep the world transform; parenting is only so explode carries it.
        obj.parent = parent
        obj.matrix_parent_inverse = parent.matrix_world.inverted()
    return obj


def box(name, size, pos, material, parent, collection, rotation=None):
    """A box in millimetres, positioned in the parent's local frame."""
    w, d, h = (v * MM for v in size)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.data.transform(Matrix.Diagonal((w, d, h, 1.0)))
    obj.location = _world(pos, parent)
    if rotation:
        obj.rotation_euler = rotation
    return _finish(obj, name, material, parent, collection)


def cylinder(name, diameter, depth, pos, material, parent, collection,
             axis="z", vertices=32):
    r, h = diameter * MM / 2.0, depth * MM
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=h, vertices=vertices,
                                        location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.location = _world(pos, parent)
    if axis == "y":
        obj.rotation_euler = (math.radians(90), 0, 0)
    elif axis == "x":
        obj.rotation_euler = (0, math.radians(90), 0)
    return _finish(obj, name, material, parent, collection)


def _world(pos_mm, parent):
    """Local millimetres to world metres, through the parent's transform."""
    local = Vector((pos_mm[0] * MM, pos_mm[1] * MM, pos_mm[2] * MM))
    return (parent.matrix_world @ local) if parent else local


# --- fans ---------------------------------------------------------------------

def build_fan(name, size, thickness, pos, parent, collection, axis="z",
              blade_count=None, material="fan"):
    """
    A case fan: frame bars, corner mount holes, hub, and twisted blades.

    Blades are a swept quad per blade, pitched and bent — enough to catch the
    light like a real impeller instead of reading as a flat disc.
    """
    layout = dl.fan_layout(size, thickness)
    made = []

    for bar in layout["frame_bars"]:
        made.append(box(f"{name}_Frame_{bar['name']}", bar["size"],
                        _oriented(bar["pos"], pos, axis), material, parent, collection))

    made.append(cylinder(f"{name}_Hub", layout["hub"]["diameter"],
                         layout["hub"]["depth"], pos, "dark", parent, collection,
                         axis=axis, vertices=24))

    count = blade_count or len(layout["blades"])
    for i in range(count):
        blade = layout["blades"][i % len(layout["blades"])]
        angle = math.radians(i * 360.0 / count)
        radius = (blade["root_radius"] + blade["tip_radius"]) / 2.0
        length = blade["tip_radius"] - blade["root_radius"]
        width = 2.0 * math.pi * radius * (blade["sweep_deg"] / 360.0)

        local = (radius * math.cos(angle), radius * math.sin(angle), 0.0)
        obj = box(f"{name}_Blade_{i + 1}", (length, width, blade["thickness"]),
                  _oriented(local, pos, axis), material, parent, collection)

        # Pitch the blade, then spin it to its station around the hub.
        spin = Matrix.Rotation(angle, 4, 'Z')
        pitch = Matrix.Rotation(math.radians(blade["pitch_deg"]), 4, 'Y')
        basis = spin @ pitch
        if axis == "y":
            basis = Matrix.Rotation(math.radians(90), 4, 'X') @ basis
        elif axis == "x":
            basis = Matrix.Rotation(math.radians(90), 4, 'Y') @ basis
        obj.rotation_euler = basis.to_euler()

        bend = obj.modifiers.new("BladeCurve", 'SIMPLE_DEFORM')
        bend.deform_method = 'BEND'
        bend.angle = math.radians(26.0)
        bend.deform_axis = 'Z'
        made.append(obj)

    return made


def _oriented(local, origin, axis):
    """Place a fan-plane coordinate on the axis the fan actually faces."""
    lx, ly, lz = local
    if axis == "z":
        offset = (lx, ly, lz)
    elif axis == "y":
        offset = (lx, lz, ly)
    else:
        offset = (lz, lx, ly)
    return tuple(origin[i] + offset[i] for i in range(3))


# --- graphics card ------------------------------------------------------------

def build_gpu(placeholder, collection):
    size = _size_mm(placeholder)
    layout = dl.gpu_layout(size[0], size[2], size[1])
    made = []

    made.append(box("GPU_Shroud", layout["shroud"]["size"], layout["shroud"]["pos"],
                    "dark", placeholder, collection))
    made.append(box("GPU_PCB", layout["pcb"]["size"], layout["pcb"]["pos"],
                    "pcb", placeholder, collection))
    made.append(box("GPU_Backplate", layout["backplate"]["size"],
                    layout["backplate"]["pos"], "alu", placeholder, collection))

    # Fin stack as one plate plus an array — 46 fins for one object's cost.
    fins = layout["fins"]
    plate = box("GPU_Fins", (fins["plate"][0], fins["plate"][1] / fins["count"],
                             fins["plate"][2]), fins["pos"], "alu",
                placeholder, collection)
    array = plate.modifiers.new("Fins", 'ARRAY')
    array.count = fins["count"]
    array.use_relative_offset = False
    array.use_constant_offset = True
    array.constant_offset_displace = (0.0, fins["gap"] * MM, 0.0)
    made.append(plate)

    for i, fan in enumerate(layout["fans"], start=1):
        made += build_fan(f"GPU_Fan_{i}", fan["diameter"], 16.0, fan["pos"],
                          placeholder, collection, axis="y", blade_count=fan["blades"])

    made.append(box("GPU_Bracket", layout["bracket"]["size"], layout["bracket"]["pos"],
                    "steel", placeholder, collection))
    for i, slot in enumerate(layout["bracket_slots"], start=1):
        made.append(box(f"GPU_Bracket_Slot_{i}", slot["size"], slot["pos"],
                        "dark", placeholder, collection))

    for i, socket in enumerate(layout["power_sockets"], start=1):
        made.append(box(f"GPU_Power_{i}", socket["size"], socket["pos"],
                        "black", placeholder, collection))
        # Pin block inside the shell, so the socket reads as a connector.
        made.append(box(f"GPU_Power_Pins_{i}",
                        (socket["size"][0] - 4.0, socket["size"][1] - 4.0, 3.0),
                        (socket["pos"][0], socket["pos"][1],
                         socket["pos"][2] + socket["size"][2] / 2 - 2.0),
                        "alu", placeholder, collection))
    return made


# --- motherboard --------------------------------------------------------------

def build_board(placeholder, collection):
    size = _size_mm(placeholder)
    layout = dl.board_layout(size[0], size[1])
    made = []
    for component in layout["components"]:
        # The board's own plane is X/Y with Z off its face, which is how the
        # layout is written — no axis juggling needed here.
        if component["name"] == "CPU_Fan":
            made += build_fan("CPU_Fan", component["size"][0], component["size"][2],
                              component["pos"], placeholder, collection, axis="z",
                              blade_count=7)
            continue
        made.append(box(component["name"], component["size"], component["pos"],
                        component["material"], placeholder, collection))
    return made


# --- power supply -------------------------------------------------------------

def build_psu(placeholder, collection):
    size = _size_mm(placeholder)
    layout = dl.psu_layout(*size)
    made = []
    made += build_fan("PSU_Fan", layout["fan"]["diameter"], 20.0, layout["fan"]["pos"],
                      placeholder, collection, axis="z", blade_count=9)
    made.append(box("PSU_IEC", layout["iec_inlet"]["size"], layout["iec_inlet"]["pos"],
                    "dark", placeholder, collection))
    made.append(box("PSU_Switch", layout["switch"]["size"], layout["switch"]["pos"],
                    "black", placeholder, collection))

    vents = layout["vents"]
    slot = box("PSU_Vents", (vents["slot"][0], 2.0, vents["slot"][2]), vents["pos"],
               "dark", placeholder, collection)
    array = slot.modifiers.new("Vents", 'ARRAY')
    array.count = vents["count"]
    array.use_relative_offset = False
    array.use_constant_offset = True
    array.constant_offset_displace = (vents["gap"] * MM, 0.0, 0.0)
    made.append(slot)

    for socket in layout["modular_panel"]:
        made.append(box(f"PSU_{socket['name']}", socket["size"], socket["pos"],
                        "black", placeholder, collection))
    return made


# --- cables -------------------------------------------------------------------

def build_cables(spec, collection):
    """Bevelled bezier runs, in world space, parented to the tray."""
    tray = bpy.context.scene.objects.get("03_Tray_Spine")
    made = []
    for route in dl.cable_routes(spec):
        curve = bpy.data.curves.new(PREFIX + route["name"], type='CURVE')
        curve.dimensions = '3D'
        curve.resolution_u = 12
        curve.bevel_depth = route["radius"] * MM / 2.0
        curve.bevel_resolution = 6
        curve.use_fill_caps = True

        spline = curve.splines.new('BEZIER')
        points = route["points"]
        spline.bezier_points.add(len(points) - 1)
        for i, point in enumerate(points):
            bp = spline.bezier_points[i]
            bp.co = Vector((point[0] * MM, point[1] * MM, point[2] * MM))
            bp.handle_left_type = 'AUTO'
            bp.handle_right_type = 'AUTO'

        obj = bpy.data.objects.new(PREFIX + route["name"], curve)
        obj.data.materials.append(_material(route["material"]))
        collection.objects.link(obj)
        if tray:
            obj.parent = tray
            obj.matrix_parent_inverse = tray.matrix_world.inverted()
        made.append(obj)
    return made


# --- driver -------------------------------------------------------------------

def _size_mm(obj):
    """The placeholder's size in millimetres, from its actual dimensions."""
    return tuple(round(v / MM, 3) for v in obj.dimensions)


def clear_detail():
    for obj in [o for o in bpy.data.objects if o.name.startswith(PREFIX)]:
        bpy.data.objects.remove(obj, do_unlink=True)


def apply_all(spec, hide_placeholders=True) -> dict:
    """
    Detail every part that has a detailed form, and hang cables between them.

    Placeholders that are fully replaced (fans, GPUs, PSUs) are hidden rather
    than deleted, so they keep driving the explode and stay in the part counts.
    """
    import build_blender

    clear_detail()
    scene = bpy.context.scene
    root = build_blender.get_collection("HEC_Node_Vault")
    collection = build_blender.get_collection("HEC_Detail", root)
    counts = {"fans": 0, "gpus": 0, "boards": 0, "psus": 0, "cables": 0, "objects": 0}

    for obj in list(scene.objects):
        name = obj.name
        if name.startswith(PREFIX):
            continue
        made = []
        if "_Fan_" in name and name[0].isdigit():          # 07_/09_ case fans
            size = _size_mm(obj)
            made = build_fan(name, size[0], size[2], (0, 0, 0), obj, collection,
                             axis="z")
            counts["fans"] += 1
        elif name.startswith("05_GPU_"):
            made = build_gpu(obj, collection)
            counts["gpus"] += 1
        elif name.startswith("04_Motherboard_"):
            made = build_board(obj, collection)
            counts["boards"] += 1
        elif name.startswith("06_PSU_"):
            made = build_psu(obj, collection)
            counts["psus"] += 1
        if not made:
            continue
        counts["objects"] += len(made)
        # The board stays visible — it is the PCB the components sit on.
        if hide_placeholders and not name.startswith("04_Motherboard_"):
            obj.hide_render = True
            obj.hide_viewport = True

    cables = build_cables(spec, collection)
    counts["cables"] = len(cables)
    counts["objects"] += len(cables)
    return counts
