"""HEC Node Vault — photoreal studio rendering (Blender-side).

Turns the block model into product-shot renders: real materials (tinted glass
with absorption, brushed aluminium, powder coat, PETG with layer lines,
emissive RGB), bevelled edges, a seamless studio backdrop, soft-box lighting
and a depth-of-field camera, rendered in Cycles.

    blender --background -P build_blender.py -- --preset resolved \\
        --studio --render shots/ --views hero,front,detail,night --samples 256

Runs inside Blender only (imports bpy).
"""
import math
import os

import bpy
from mathutils import Vector

# Each view: camera direction from the subject, lens, framing margin, and
# whether the lights drop away for the dark "hero at night" look.
VIEWS = {
    "hero":   {"dir": (0.85, -1.0, 0.42), "lens": 60.0, "margin": 1.28, "night": False},
    "front":  {"dir": (0.0, -1.0, 0.16),  "lens": 85.0, "margin": 1.22, "night": False},
    "side":   {"dir": (1.0, -0.12, 0.16), "lens": 85.0, "margin": 1.22, "night": False},
    # Close on one node, glazing off — this is the shot that shows the fan
    # blades, heatsinks, connectors and loom.
    "detail": {"dir": (0.62, -1.0, 0.10), "lens": 85.0, "margin": 0.62,
               "night": False, "hide": ("01_Glass", "08_Top_Cap", "11_", "12_", "P13_")},
    "night":  {"dir": (0.9, -1.0, 0.30),  "lens": 60.0, "margin": 1.30, "night": True},
    # Same framing as the hero with the glazing off — the only way to actually
    # see inside a tinted box, and the shot that shows the build.
    "cutaway": {"dir": (0.85, -1.0, 0.42), "lens": 60.0, "margin": 1.22,
                "night": False, "hide": ("01_Glass", "08_Top_Cap")},
}

STUDIO_PREFIX = "HEC_STUDIO_"


# --- materials ----------------------------------------------------------------

def _principled(mat):
    return mat.node_tree.nodes.get("Principled BSDF") if mat.use_nodes else None


def _set(bsdf, names, value):
    """Set the first socket that exists — names moved between Blender versions."""
    for name in names:
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = value
            return True
    return False


def _add_bump(mat, bsdf, scale=180.0, strength=0.12, detail=6.0):
    """Fine surface noise — nothing real is perfectly smooth."""
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    if "Detail" in noise.inputs:
        noise.inputs["Detail"].default_value = detail
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    if "Normal" in bsdf.inputs:
        links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def _add_layer_lines(mat, bsdf, layer_mm=0.24):
    """
    Print layer lines on the printed parts.

    A wave texture banded at the layer height, pushed through a bump node. It is
    the single cue that makes a printed part read as printed rather than moulded.
    """
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    coord = nodes.new("ShaderNodeTexCoord")
    wave = nodes.new("ShaderNodeTexWave")
    wave.wave_type = 'BANDS'
    if hasattr(wave, "bands_direction"):
        wave.bands_direction = 'Z'
    wave.inputs["Scale"].default_value = max(1.0, 1.0 / (layer_mm * 0.001) / 40.0)
    if "Distortion" in wave.inputs:
        wave.inputs["Distortion"].default_value = 0.0
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    links.new(coord.outputs["Object"], wave.inputs["Vector"])
    links.new(wave.outputs["Fac"], bump.inputs["Height"])
    if "Normal" in bsdf.inputs:
        links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def _perforate(mat, bsdf, pitch=6.0, hole=0.42):
    """
    Punch a real perforation pattern into the grille with alpha.

    Two sine waves on a grid threshold into round-ish holes — cheaper than
    booleaning several thousand holes, and it reads correctly at product-shot
    distance because the light passes through.
    """
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    coord = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    scale = 1000.0 / pitch
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    sep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], sep.inputs["Vector"])

    def sine(socket):
        node = nodes.new("ShaderNodeMath")
        node.operation = 'SINE'
        links.new(socket, node.inputs[0])
        square = nodes.new("ShaderNodeMath")
        square.operation = 'POWER'
        square.inputs[1].default_value = 2.0
        links.new(node.outputs[0], square.inputs[0])
        return square.outputs[0]

    add = nodes.new("ShaderNodeMath")
    add.operation = 'ADD'
    links.new(sine(sep.outputs["X"]), add.inputs[0])
    links.new(sine(sep.outputs["Y"]), add.inputs[1])
    threshold = nodes.new("ShaderNodeMath")
    threshold.operation = 'GREATER_THAN'
    threshold.inputs[1].default_value = hole
    links.new(add.outputs[0], threshold.inputs[0])
    if "Alpha" in bsdf.inputs:
        links.new(threshold.outputs[0], bsdf.inputs["Alpha"])
    if hasattr(mat, "blend_method"):
        mat.blend_method = 'HASHED'


def upgrade_materials():
    """Replace the flat block-model materials with product-shot shading."""
    recipes = {
        # Tint is the transmission colour, so it darkens everything seen
        # through it: a near-black base renders as a black box, not glass.
        "HEC_glass": {"base": (0.46, 0.52, 0.54, 1.0), "rough": 0.03,
                      "metal": 0.0, "transmission": 1.0, "ior": 1.52},
        "HEC_alu": {"base": (0.72, 0.74, 0.78, 1.0), "rough": 0.22, "metal": 1.0,
                    "bump": (900.0, 0.06)},
        "HEC_steel": {"base": (0.52, 0.54, 0.58, 1.0), "rough": 0.38, "metal": 1.0,
                      "bump": (600.0, 0.08)},
        "HEC_tray": {"base": (0.62, 0.64, 0.67, 1.0), "rough": 0.34, "metal": 1.0,
                     "bump": (400.0, 0.14)},
        "HEC_dark": {"base": (0.055, 0.059, 0.065, 1.0), "rough": 0.5, "metal": 0.1,
                     "bump": (2200.0, 0.10)},
        "HEC_black": {"base": (0.032, 0.034, 0.038, 1.0), "rough": 0.45, "metal": 0.15,
                      "bump": (1600.0, 0.06)},
        "HEC_fan": {"base": (0.016, 0.017, 0.02, 1.0), "rough": 0.55, "metal": 0.0},
        "HEC_pcb": {"base": (0.02, 0.14, 0.07, 1.0), "rough": 0.42, "metal": 0.1},
        "HEC_printed": {"base": (0.40, 0.42, 0.46, 1.0), "rough": 0.58, "metal": 0.0,
                        "layers": 0.24},
        # IOR ~1 so the diffuser does not refract: at 1.46 the strip inside it
        # became a light trap and the corners rendered dead.
        "HEC_diffuser": {"base": (0.97, 0.98, 0.99, 1.0), "rough": 0.12, "metal": 0.0,
                         "transmission": 1.0, "ior": 1.02},
        "HEC_led": {"base": (0.25, 0.85, 0.80, 1.0), "emission": 120.0},
    }

    for name, recipe in recipes.items():
        mat = bpy.data.materials.get(name)
        if mat is None:
            continue
        mat.use_nodes = True
        bsdf = _principled(mat)
        if bsdf is None:
            continue
        _set(bsdf, ("Base Color",), recipe.get("base", (0.5, 0.5, 0.5, 1.0)))
        _set(bsdf, ("Roughness",), recipe.get("rough", 0.5))
        _set(bsdf, ("Metallic",), recipe.get("metal", 0.0))
        if "transmission" in recipe:
            _set(bsdf, ("Transmission Weight", "Transmission"), recipe["transmission"])
            _set(bsdf, ("IOR",), recipe.get("ior", 1.45))
            if hasattr(mat, "use_screen_refraction"):
                mat.use_screen_refraction = True
            if hasattr(mat, "blend_method"):
                mat.blend_method = 'BLEND'
        if "emission" in recipe:
            _set(bsdf, ("Emission Color", "Emission"), recipe["base"])
            _set(bsdf, ("Emission Strength",), recipe["emission"])
        if "bump" in recipe:
            _add_bump(mat, bsdf, scale=recipe["bump"][0], strength=recipe["bump"][1])
        if "layers" in recipe:
            _add_layer_lines(mat, bsdf, recipe["layers"])

    grille = bpy.data.materials.get("HEC_alu")
    perforated = grille.copy() if grille else None
    if perforated:
        perforated.name = "HEC_alu_perforated"
        bsdf = _principled(perforated)
        if bsdf:
            _perforate(perforated, bsdf)
        for obj in bpy.context.scene.objects:
            if "Vent_Grille" in obj.name and obj.data.materials:
                obj.data.materials[0] = perforated


def add_bevels(width_mm=0.6, segments=2):
    """
    Bevel every edge slightly and smooth-shade by angle.

    Perfectly sharp edges are the giveaway that something is CG — a real part
    has a catch of light along every edge.
    """
    width = width_mm * 0.001
    glazing = {"HEC_glass", "HEC_diffuser"}
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH' or obj.name.startswith(STUDIO_PREFIX):
            continue
        if obj.data.materials and any(m and m.name in glazing
                                      for m in obj.data.materials):
            continue
        if not any(m.type == 'BEVEL' for m in obj.modifiers):
            bevel = obj.modifiers.new("StudioBevel", 'BEVEL')
            bevel.width = width
            bevel.segments = segments
            bevel.limit_method = 'ANGLE'
            bevel.angle_limit = math.radians(40.0)
            # harden_normals rewrites custom normals, which wrecks refraction —
            # the glass rendered as an opaque black box with it on.
            bevel.harden_normals = False
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
        if hasattr(obj.data, "use_auto_smooth"):       # Blender < 4.1
            obj.data.use_auto_smooth = True
            obj.data.auto_smooth_angle = math.radians(40.0)


# --- studio -------------------------------------------------------------------

def _subject_bounds(scene):
    corners = []
    for obj in scene.objects:
        if obj.type != 'MESH' or obj.name.startswith(STUDIO_PREFIX) or obj.hide_render:
            continue
        corners.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
    if not corners:
        return Vector((0, 0, 0)), 1.0, Vector((0, 0, 0))
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners),
                 min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners),
                 max(c.z for c in corners)))
    return (lo + hi) * 0.5, max((hi - lo).length * 0.5, 0.05), lo


def clear_studio():
    for obj in [o for o in bpy.data.objects if o.name.startswith(STUDIO_PREFIX)]:
        bpy.data.objects.remove(obj, do_unlink=True)


OUTBOARD_PREFIXES = ("11_", "12_", "P10_", "P13_", STUDIO_PREFIX)


def _cabinet_bounds(scene):
    """Bounds of the case itself — the display and its arm hang outside it."""
    corners = []
    for obj in scene.objects:
        if obj.type != 'MESH' or obj.name.startswith(OUTBOARD_PREFIXES):
            continue
        corners.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
    if not corners:
        return Vector((0, 0, 0)), 1.0, 1.0
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners),
                 min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners),
                 max(c.z for c in corners)))
    span = hi - lo
    return (lo + hi) * 0.5, min(span.x, span.y), span.z


def glass_casts_no_shadow(scene=None):
    """
    Let the lamps light the inside of the case.

    Light reaching a surface *through* a refractive panel is a caustic path,
    and Cycles cannot sample it with next-event estimation — so a sealed glass
    cabinet renders with a pitch-black interior no matter how bright the studio
    is. Turning off shadow visibility on the glazing lets light straight in
    while the camera still sees the panes, their tint and their reflections.
    It is the standard trick for product shots of anything behind glass.
    """
    scene = scene or bpy.context.scene
    glazing = {"HEC_glass", "HEC_diffuser"}
    count = 0
    for obj in scene.objects:
        if obj.type != 'MESH' or not obj.data.materials:
            continue
        if any(m and m.name in glazing for m in obj.data.materials):
            if hasattr(obj, "visible_shadow"):
                obj.visible_shadow = False
                count += 1
    return count


def _interior_fill(scene, centre, radius, night=False):
    """Emissive panels inside the case, hidden from camera and refraction."""
    material = bpy.data.materials.get(STUDIO_PREFIX + "Fill")
    if material is None:
        material = bpy.data.materials.new(STUDIO_PREFIX + "Fill")
        material.use_nodes = True
        bsdf = _principled(material)
        if bsdf:
            _set(bsdf, ("Emission Color", "Emission"), (1.0, 0.97, 0.92, 1.0))
            _set(bsdf, ("Emission Strength",), 1.5 if night else 2.5)
            _set(bsdf, ("Base Color",), (0.0, 0.0, 0.0, 1.0))

    case_centre, case_width, case_height = _cabinet_bounds(scene)
    for level in (0.28, 0.0, -0.28):
        bpy.ops.mesh.primitive_cube_add(
            size=case_width * 0.7,
            location=(case_centre.x, case_centre.y,
                      case_centre.z + case_height * level))
        panel = bpy.context.active_object
        panel.name = f"{STUDIO_PREFIX}Fill_{level:+.2f}"
        panel.scale = (1.0, 1.0, 0.02)
        panel.data.materials.append(material)
        for ray_type in ("visible_camera", "visible_glossy", "visible_transmission",
                         "visible_diffuse"):
            if hasattr(panel, ray_type):
                setattr(panel, ray_type, ray_type == "visible_diffuse")


def build_studio(scene=None, night=False):
    """Seamless backdrop, soft key/fill/rim, and a dark world."""
    scene = scene or bpy.context.scene
    clear_studio()
    centre, radius, lo = _subject_bounds(scene)

    # Floor, big enough that its edge never enters frame.
    bpy.ops.mesh.primitive_plane_add(size=radius * 40.0,
                                     location=(centre.x, centre.y, lo.z))
    floor = bpy.context.active_object
    floor.name = STUDIO_PREFIX + "Floor"
    floor_mat = bpy.data.materials.new(STUDIO_PREFIX + "Floor")
    floor_mat.use_nodes = True
    bsdf = _principled(floor_mat)
    if bsdf:
        _set(bsdf, ("Base Color",), (0.021, 0.023, 0.027, 1.0))
        _set(bsdf, ("Roughness",), 0.18 if night else 0.28)
        _set(bsdf, ("Metallic",), 0.0)
        _set(bsdf, ("Specular IOR Level", "Specular"), 0.7)
    floor.data.materials.append(floor_mat)

    world = scene.world or bpy.data.worlds.new("HEC_World")
    scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background:
        background.inputs["Color"].default_value = (0.012, 0.014, 0.018, 1.0)
        background.inputs["Strength"].default_value = 0.08 if night else 0.55

    # Interior fill. A lamp inside does not work: object ray-visibility flags
    # do not stick on light objects, so the bare lamp renders as a blown-out
    # disc through the panes. Emissive planes do respect them — they light the
    # guts and stay invisible to camera and refraction rays alike.
    _interior_fill(scene, centre, radius, night)

    # Key / fill / rim as area lights scaled to the subject.
    rig = (
        ("Key", (1.5, -2.1, 1.9), 1.0, 0.28 if night else 1.0),
        ("Fill", (-2.0, -1.4, 0.7), 0.75, 0.10 if night else 0.42),
        ("Rim", (-0.6, 2.2, 1.6), 0.8, 0.55 if night else 0.75),
        ("Top", (0.1, 0.2, 3.0), 1.3, 0.20 if night else 0.55),
    )
    for name, offset, size_factor, power in rig:
        data = bpy.data.lights.new(STUDIO_PREFIX + name, type='AREA')
        data.shape = 'RECTANGLE'
        data.size = radius * 2.2 * size_factor
        data.size_y = radius * 3.0 * size_factor
        # Area light power scales with the square of the distance to the subject.
        data.energy = power * 900.0 * max(radius, 0.15) ** 2
        light = bpy.data.objects.new(STUDIO_PREFIX + name, data)
        light.location = centre + Vector(offset) * radius * 2.0
        direction = centre - light.location
        light.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
        scene.collection.objects.link(light)
    return centre, radius


def place_camera(view: str, scene=None):
    scene = scene or bpy.context.scene
    spec = VIEWS.get(view, VIEWS["hero"])
    centre, radius, lo = _subject_bounds(scene)

    if view == "detail":
        # Frame the middle node stack rather than the whole cabinet.
        centre = Vector((centre.x, centre.y, lo.z + (centre.z - lo.z) * 1.25))

    data = bpy.data.cameras.new(STUDIO_PREFIX + "Cam")
    data.lens = spec["lens"]
    camera = bpy.data.objects.new(STUDIO_PREFIX + "Cam", data)
    scene.collection.objects.link(camera)

    direction = Vector(spec["dir"])
    direction.normalize()
    distance = radius / max(math.tan(data.angle * 0.5), 1e-4) * spec["margin"]
    camera.location = centre + direction * distance
    camera.rotation_euler = (centre - camera.location).to_track_quat('-Z', 'Y').to_euler()

    data.dof.use_dof = True
    data.dof.focus_distance = distance
    data.dof.aperture_fstop = 5.6 if view != "detail" else 2.8
    data.clip_start = max(radius * 0.005, 1e-4)
    data.clip_end = distance + radius * 20.0

    scene.camera = camera
    return camera


def enable_gpu() -> str:
    """
    Turn on GPU compute for Cycles and report what it found.

    In background mode Cycles renders on the CPU unless both halves are set:
    the add-on preference picking a backend with devices enabled, and the
    scene's device set to GPU. Setting only scene.cycles.device silently keeps
    rendering on the CPU.
    """
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except (KeyError, AttributeError):
        return "CPU (cycles preferences unavailable)"

    for backend in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = backend
        except (TypeError, ValueError):
            continue
        for refresh in ("refresh_devices", "get_devices"):
            if hasattr(prefs, refresh):
                try:
                    getattr(prefs, refresh)()
                except Exception:
                    pass
                break
        devices = [d for d in getattr(prefs, "devices", []) if d.type == backend]
        if not devices:
            continue
        for device in prefs.devices:
            device.use = device.type == backend
        return f"{backend}: " + ", ".join(d.name for d in devices)
    return "CPU (no GPU devices found)"


def _use_engine(render, engine: str) -> bool:
    """
    Try to select a render engine and confirm it took.

    Do NOT test membership of RenderSettings.bl_rna's engine enum first: that
    enum lists only the built-in engines, so Cycles — which registers as an
    add-on — never appears in it. Checking it silently sends every render to
    EEVEE, whose screen-space refraction cannot see through a glass slab to
    what is behind it, which is exactly how this project ended up with a
    pitch-black glass cabinet for a dozen renders.
    """
    try:
        render.engine = engine
    except (TypeError, ValueError):
        return False
    return render.engine == engine


def configure_render(scene=None, samples=192, resolution=2000, engine="CYCLES"):
    scene = scene or bpy.context.scene
    render = scene.render

    using_cycles = engine.upper() == "CYCLES" and _use_engine(render, 'CYCLES')
    if using_cycles:
        cycles = getattr(scene, "cycles", None)
        if cycles:
            cycles.samples = samples
            cycles.use_denoising = True
            cycles.max_bounces = 24
            cycles.transmission_bounces = 24
            cycles.transparent_max_bounces = 24
            cycles.use_adaptive_sampling = True
            compute = enable_gpu()
            cycles.device = 'CPU' if compute.startswith("CPU") else 'GPU'
            print(f"[HEC] Cycles compute: {compute}")
    else:
        for candidate in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            if _use_engine(render, candidate):
                break
        eevee = getattr(scene, "eevee", None)
        if eevee:
            if hasattr(eevee, "taa_render_samples"):
                eevee.taa_render_samples = max(64, samples)
            for attr in ("use_ssr", "use_ssr_refraction", "use_gtao", "use_bloom",
                         "use_raytracing"):
                if hasattr(eevee, attr):
                    setattr(eevee, attr, True)

    render.resolution_x = resolution
    render.resolution_y = int(resolution * 1.25)
    render.resolution_percentage = 100
    render.film_transparent = False
    render.image_settings.file_format = 'PNG'
    render.image_settings.color_mode = 'RGB'
    render.image_settings.compression = 15

    view_settings = scene.view_settings
    for look in ("AgX", "Filmic"):
        try:
            view_settings.view_transform = look
            break
        except Exception:
            continue
    view_settings.exposure = 0.0
    view_settings.gamma = 1.0


def render_views(out_dir: str, views=("hero",), samples=192, resolution=2000,
                 engine="CYCLES", scene=None) -> dict:
    """Studio-render each named view. Returns {view: path}."""
    scene = scene or bpy.context.scene
    os.makedirs(out_dir, exist_ok=True)
    written = {}
    for view in views:
        spec = VIEWS.get(view)
        if not spec:
            continue
        hidden = []
        for prefix in spec.get("hide", ()):
            for obj in scene.objects:
                if obj.name.startswith(prefix) and not obj.hide_render:
                    obj.hide_render = True
                    hidden.append(obj)
        build_studio(scene, night=spec["night"])
        camera = place_camera(view, scene)
        configure_render(scene, samples=samples, resolution=resolution, engine=engine)
        path = os.path.join(out_dir, f"{view}.png")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        if os.path.isfile(path):
            written[view] = os.path.abspath(path)
        bpy.data.objects.remove(camera, do_unlink=True)
        for obj in hidden:
            obj.hide_render = False
    clear_studio()
    return written


def prepare(layer_bevels=True):
    """Everything that has to happen once before any studio render."""
    upgrade_materials()
    if layer_bevels:
        add_bevels()
    panes = glass_casts_no_shadow()
    print(f"[HEC] glazing set to cast no shadow on {panes} object(s)")
