"""HEC Node Vault — build the assembly in Blender.

    # interactive (GUI): open in the Text Editor and press Run Script, or
    blender -P build_blender.py

    # headless build + STLs + manifests
    blender --background --factory-startup -P build_blender.py -- \
        --preset resolved --save vault.blend --export-stl printed/ \
        --fabrication FABRICATION.md --assembly ASSEMBLY.md

    # photoreal product shots, and the assembly sequence as images
    blender --background -P build_blender.py -- --preset resolved \
        --render shots/ --views hero,front,detail,night --samples 256
    blender --background -P build_blender.py -- --preset resolved \
        --render-steps steps/

Everything is driven by `spec.py`, which holds the dimensions and does the fit,
thermal, electrical and weight checks. This module only turns that spec into
Blender objects, wires the explode slider, and exports.

The explode control is a scene custom property, `hec_explode` (0 = assembled,
1 = fully exploded), driven onto every object's location — so dragging the
slider animates the whole assembly apart and back, and it can be keyframed.
"""
import os
import sys

import bpy
from mathutils import Matrix

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import spec as vault_spec  # noqa: E402

MM = 0.001  # Blender works in metres; the spec is in millimetres.

EXPLODE_PROP = "hec_explode"

# name -> (base colour RGBA, metallic, roughness, special)
MATERIALS = {
    "steel": ((0.54, 0.56, 0.60, 1.0), 0.72, 0.46, ""),
    "tray": ((0.60, 0.63, 0.66, 1.0), 0.78, 0.40, ""),
    "dark": ((0.21, 0.23, 0.26, 1.0), 0.60, 0.50, ""),
    "alu": ((0.72, 0.74, 0.78, 1.0), 0.80, 0.34, ""),
    "pcb": ((0.11, 0.42, 0.23, 1.0), 0.25, 0.62, ""),
    "black": ((0.08, 0.09, 0.11, 1.0), 0.40, 0.55, ""),
    "fan": ((0.12, 0.13, 0.16, 1.0), 0.40, 0.60, ""),
    "glass": ((0.62, 0.83, 0.90, 1.0), 0.0, 0.06, "glass"),
    "led": ((0.31, 0.82, 0.77, 1.0), 0.0, 0.5, "emission"),
    "diffuser": ((0.85, 0.95, 0.98, 1.0), 0.0, 0.35, "diffuse_glass"),
    "printed": ((0.85, 0.45, 0.15, 1.0), 0.0, 0.62, ""),
}


def _args():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def _arg(name, default=None):
    args = _args()
    return args[args.index(name) + 1] if name in args else default


def _flag(name):
    return name in _args()


# --- scene helpers ------------------------------------------------------------

def clear_scene():
    """Remove everything, including orphaned data, for a repeatable build."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for block in (bpy.data.meshes, bpy.data.materials):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def get_material(name: str):
    existing = bpy.data.materials.get(f"HEC_{name}")
    if existing:
        return existing
    colour, metallic, roughness, special = MATERIALS.get(name, MATERIALS["steel"])
    mat = bpy.data.materials.new(f"HEC_{name}")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = colour
        bsdf.inputs["Metallic"].default_value = metallic
        bsdf.inputs["Roughness"].default_value = roughness
        # Socket names moved between Blender versions; set what exists.
        if special == "glass":
            for key in ("Transmission Weight", "Transmission"):
                if key in bsdf.inputs:
                    bsdf.inputs[key].default_value = 0.95
                    break
            if "IOR" in bsdf.inputs:
                bsdf.inputs["IOR"].default_value = 1.52
            mat.blend_method = 'BLEND'
        elif special == "diffuse_glass":
            for key in ("Transmission Weight", "Transmission"):
                if key in bsdf.inputs:
                    bsdf.inputs[key].default_value = 0.6
                    break
        elif special == "emission":
            for key in ("Emission Color", "Emission"):
                if key in bsdf.inputs:
                    bsdf.inputs[key].default_value = colour
                    break
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = 4.0
    return mat


def get_collection(name: str, parent=None):
    existing = bpy.data.collections.get(name)
    if existing is None:
        existing = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(existing)
    return existing


def _mesh_object(part, collection):
    """A box or cylinder sized and placed per the spec, in metres."""
    w, d, h = (v * MM for v in part["size"])
    x, y, z = (v * MM for v in part["pos"])

    if part["kind"] == "cyl":
        radius = part.get("radius", part["size"][0] / 2.0) * MM
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=h, location=(x, y, z),
                                            vertices=24)
    else:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, y, z))

    obj = bpy.context.active_object
    obj.name = part["key"]
    obj.data.name = part["key"]
    if part["kind"] != "cyl":
        # Scale the mesh data rather than the object, so scale stays 1.0 and
        # dimensions read true in the sidebar and in exports.
        obj.data.transform(Matrix.Diagonal((w, d, h, 1.0)))

    obj.data.materials.append(get_material(part["material"]))

    for collection_link in list(obj.users_collection):
        collection_link.objects.unlink(obj)
    collection.objects.link(obj)

    obj["bom"] = part["no"]
    obj["hec_name"] = part["name"]
    obj["printed"] = bool(part.get("printed"))
    obj["note"] = part.get("note", "")
    if part.get("print_profile"):
        profile = part["print_profile"]
        obj["print_material"] = profile.get("material", "")
        obj["print_layer_mm"] = profile.get("layer_mm", 0.0)
        obj["print_infill_pct"] = profile.get("infill_pct", 0)
        obj["print_supports"] = bool(profile.get("supports"))
        obj["print_status"] = "draft" if profile.get("draft") else "ready"
    return obj


def add_explode_drivers(obj, part, scene):
    """
    Drive the object's location from the scene's explode property.

    location[i] = base[i] + explode * direction[i] * distance
    """
    direction = part.get("explode_dir") or (0.0, 0.0, 0.0)
    distance = float(part.get("explode_dist") or 0.0)
    if distance <= 0 or not any(direction):
        return
    length = sum(component * component for component in direction) ** 0.5 or 1.0

    for axis in range(3):
        delta = (direction[axis] / length) * distance * MM
        if abs(delta) < 1e-9:
            continue
        base = obj.location[axis]
        fcurve = obj.driver_add("location", axis)
        driver = fcurve.driver
        driver.type = 'SCRIPTED'
        var = driver.variables.new()
        var.name = "e"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'SCENE'
        var.targets[0].id = scene
        var.targets[0].data_path = f'["{EXPLODE_PROP}"]'
        driver.expression = f"{base:.6f} + e * {delta:.6f}"


def setup_explode_property(scene, value=0.0):
    scene[EXPLODE_PROP] = value
    try:
        ui = scene.id_properties_ui(EXPLODE_PROP)
        ui.update(min=0.0, max=1.0, soft_min=0.0, soft_max=1.0,
                  description="0 = assembled, 1 = fully exploded")
    except Exception:
        # Older Blender: the property still works, just without a clamped slider.
        pass


# --- viewport UI (interactive sessions) ---------------------------------------

class HEC_PT_Panel(bpy.types.Panel):
    bl_label = "HEC Node Vault"
    bl_idname = "HEC_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "HEC"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        if EXPLODE_PROP in scene:
            layout.prop(scene, f'["{EXPLODE_PROP}"]', text="Explode", slider=True)
        row = layout.row(align=True)
        row.operator("hec.set_explode", text="Assembled").value = 0.0
        row.operator("hec.set_explode", text="Exploded").value = 1.0
        layout.separator()
        layout.operator("hec.isolate_printed", text="Show printed parts only")
        layout.operator("hec.show_all", text="Show everything")
        obj = context.active_object
        if obj and "hec_name" in obj:
            box = layout.box()
            box.label(text=obj["hec_name"])
            box.label(text=f"BOM item {obj['bom']}")
            if obj.get("printed"):
                box.label(text=f"{obj.get('print_material', '')} · "
                               f"{obj.get('print_infill_pct', 0)}% infill · "
                               f"{obj.get('print_status', '')}")
            dims = obj.dimensions
            box.label(text=f"{dims.x * 1000:.0f} x {dims.y * 1000:.0f} x "
                           f"{dims.z * 1000:.0f} mm")


class HEC_OT_SetExplode(bpy.types.Operator):
    bl_idname = "hec.set_explode"
    bl_label = "Set explode"
    bl_options = {'REGISTER', 'UNDO'}
    value: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)

    def execute(self, context):
        context.scene[EXPLODE_PROP] = self.value
        # Drivers re-evaluate on a depsgraph tag.
        for obj in context.scene.objects:
            obj.update_tag()
        return {'FINISHED'}


class HEC_OT_IsolatePrinted(bpy.types.Operator):
    bl_idname = "hec.isolate_printed"
    bl_label = "Show printed parts only"

    def execute(self, context):
        for obj in context.scene.objects:
            obj.hide_set(not obj.get("printed", False))
        return {'FINISHED'}


class HEC_OT_ShowAll(bpy.types.Operator):
    bl_idname = "hec.show_all"
    bl_label = "Show everything"

    def execute(self, context):
        for obj in context.scene.objects:
            obj.hide_set(False)
        return {'FINISHED'}


UI_CLASSES = (HEC_PT_Panel, HEC_OT_SetExplode, HEC_OT_IsolatePrinted, HEC_OT_ShowAll)


def register_ui():
    for cls in UI_CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass


def unregister_ui():
    for cls in reversed(UI_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


# --- build --------------------------------------------------------------------

def build(preset: str = "brief", explode: float = 0.0) -> dict:
    """Build the whole assembly and return the spec it was built from."""
    params = vault_spec.PRESETS.get(preset, {})
    spec = vault_spec.build_spec(params)
    scene = bpy.context.scene

    root = get_collection("HEC_Node_Vault")
    groups = {}
    for part in spec["parts"]:
        groups.setdefault(part["group"], get_collection(f"HEC_{part['group']}", root))

    setup_explode_property(scene, explode)
    for part in spec["parts"]:
        obj = _mesh_object(part, groups[part["group"]])
        add_explode_drivers(obj, part, scene)

    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.length_unit = 'MILLIMETERS'
    return spec


def export_printed_stls(directory: str) -> list:
    """
    Write one STL per printed part, moved to the origin for slicing.

    Parts are exported as generated: the ones flagged draft in the manifest are
    massing models and need dimensioned CAD before they will fit real hardware.
    """
    os.makedirs(directory, exist_ok=True)
    written = []
    printed = [o for o in bpy.context.scene.objects if o.get("printed")]

    for obj in printed:
        duplicate = obj.copy()
        duplicate.data = obj.data.copy()
        duplicate.animation_data_clear()  # drop the explode drivers
        duplicate.location = (0.0, 0.0, 0.0)
        bpy.context.scene.collection.objects.link(duplicate)

        bpy.ops.object.select_all(action='DESELECT')
        duplicate.select_set(True)
        bpy.context.view_layer.objects.active = duplicate

        path = os.path.join(directory, f"{obj.name}.stl")
        try:
            if hasattr(bpy.ops.wm, "stl_export"):
                bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True,
                                      global_scale=1000.0)
            else:
                bpy.ops.export_mesh.stl(filepath=path, use_selection=True,
                                        global_scale=1000.0)
            written.append(path)
        finally:
            bpy.data.objects.remove(duplicate, do_unlink=True)
    return written


def render_assembly_steps(spec, out_dir: str, samples: int = 64, resolution: int = 1400,
                          engine: str = "CYCLES", view: str = "hero") -> dict:
    """
    Render the build sequence as progressive images: step N shows everything
    installed up to and including step N, from one fixed camera.

    One camera and one lighting setup across the whole set, so the slides read
    as a build rather than a gallery.
    """
    import fabrication
    import render_studio

    os.makedirs(out_dir, exist_ok=True)
    scene = bpy.context.scene
    steps = fabrication.assembly_steps(spec)

    # Frame the finished machine once, so nothing jumps between slides.
    render_studio.build_studio(scene, night=False)
    camera = render_studio.place_camera(view, scene)
    render_studio.configure_render(scene, samples=samples, resolution=resolution,
                                   engine=engine)

    original = {o.name: o.hide_render for o in scene.objects}
    written = {}
    try:
        for step in steps:
            visible = set(fabrication.parts_visible_at(spec, step["no"]))
            for obj in scene.objects:
                if obj.name.startswith(render_studio.STUDIO_PREFIX):
                    continue
                obj.hide_render = obj.name not in visible
            path = os.path.join(out_dir, f"step-{step['no']:02d}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)
            if os.path.isfile(path):
                written[step["no"]] = os.path.abspath(path)
    finally:
        for obj in scene.objects:
            if obj.name in original:
                obj.hide_render = original[obj.name]
        bpy.data.objects.remove(camera, do_unlink=True)
        render_studio.clear_studio()
    return written


def main():
    preset = _arg("--preset", "brief")
    if preset not in vault_spec.PRESETS:
        raise SystemExit(f"unknown preset {preset!r}; "
                         f"choose from {', '.join(vault_spec.PRESETS)}")

    if not _flag("--keep"):
        clear_scene()

    spec = build(preset, explode=float(_arg("--explode", "0")))
    print(f"[HEC] built '{preset}': {spec['totals']['part_count']} parts "
          f"({spec['totals']['printed_part_count']} printed)")
    for warning in spec["warnings"]:
        print(f"[HEC] {warning['severity'].upper()} ({warning['topic']}): {warning['text']}")

    manifest = _arg("--manifest")
    if manifest:
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(vault_spec.manifest_markdown(spec))
        print(f"[HEC] manifest -> {manifest}")

    stl_dir = _arg("--export-stl")
    if stl_dir:
        written = export_printed_stls(stl_dir)
        print(f"[HEC] {len(written)} STL(s) -> {stl_dir}")

    if _flag("--studio") or _arg("--render") or _arg("--render-steps"):
        import render_studio
        render_studio.prepare()
        print("[HEC] studio materials and bevels applied")

    shots = _arg("--render")
    if shots:
        import render_studio
        views = [v.strip() for v in _arg("--views", "hero").split(",") if v.strip()]
        written = render_studio.render_views(
            shots, views=views,
            samples=int(_arg("--samples", "192")),
            resolution=int(_arg("--res", "2000")),
            engine=_arg("--engine", "CYCLES").upper())
        for view, path in written.items():
            print(f"[HEC] {view} -> {path}")

    step_dir = _arg("--render-steps")
    if step_dir:
        written = render_assembly_steps(
            spec, step_dir,
            samples=int(_arg("--step-samples", "64")),
            resolution=int(_arg("--step-res", "1400")),
            engine=_arg("--engine", "CYCLES").upper())
        print(f"[HEC] {len(written)} assembly step render(s) -> {step_dir}")

    fab = _arg("--fabrication")
    if fab:
        import fabrication as fabrication_mod
        with open(fab, "w", encoding="utf-8") as f:
            f.write(fabrication_mod.manifest_markdown(spec))
        print(f"[HEC] fabrication manifest -> {fab}")

    assembly = _arg("--assembly")
    if assembly:
        import fabrication as fabrication_mod
        with open(assembly, "w", encoding="utf-8") as f:
            f.write(fabrication_mod.assembly_markdown(spec))
        print(f"[HEC] assembly sequence -> {assembly}")

    blend = _arg("--save")
    if blend:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend))
        print(f"[HEC] saved -> {blend}")

    if not bpy.app.background:
        register_ui()
        print("[HEC] Sidebar: View3D > N > HEC tab for the explode slider.")


if __name__ == "__main__":
    main()
