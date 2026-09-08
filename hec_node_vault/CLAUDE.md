# CLAUDE.md — HEC Node Vault

Guidance for Claude Code working in `hec_node_vault/`. Read this before
touching the model or the render pipeline; most of it is hard-won and not
obvious from the code.

## What this is

A parametric model of the HEC Node Vault (drawing HEC-PC-002 Rev A): a 4–5
node glass cabinet built on a vertical cable-tray spine. One source of truth
drives everything — the Blender model, the STL exports, the fabrication
manifest, the assembly sequence and the shop-floor build sheet.

There is no build system, no test framework and no linter in this repo. The
checks are plain scripts (below) and they are the safety net — keep them
passing.

## Module split, and the invariant that matters

**`spec.py`, `fabrication.py`, `detail_layout.py` must never import bpy.**
That is what lets the layout, the fit checks and every document be verified
without Blender. The Blender-side modules are `build_blender.py`,
`detail.py`, `agent_step`-style helpers, `render_studio.py`.

| File | Role |
|------|------|
| `spec.py` | Every part: size, position, explode vector, in millimetres. Presets (`brief`, `resolved`, `resolved-5`). Fit / thermal / electrical / weight / envelope checks. |
| `fabrication.py` | Cut parts, bought-in fixings, the 15-step assembly sequence, and which parts are installed at each step. |
| `detail_layout.py` | Where every fan blade, RAM stick, connector and cable run sits, with `check_board()` clash detection. |
| `build_blender.py` | Spec → Blender objects, explode drivers, STL export, step renders. |
| `detail.py` | Builds the detail geometry and parents it to the placeholders. |
| `render_studio.py` | Materials, studio lighting, cameras, Cycles configuration. |
| `build_sheet.html` | Generated shop-floor page. Regenerate it when the spec changes (see below). |

Generated files — `FABRICATION*.md`, `ASSEMBLY.md`, `manifest_*.md`,
`build_sheet.html` — are committed, so **regenerate them in the same commit
as any spec change** or the docs start lying about the model.

## Commands

```bash
# numbers only, no Blender
python3 spec.py --preset resolved                 # counts + warnings
python3 fabrication.py --preset resolved --manifest > FABRICATION.md
python3 fabrication.py --preset resolved --assembly > ASSEMBLY.md
python3 spec.py --preset resolved --manifest > manifest_resolved.md

# interactive model: N panel → HEC tab for the explode slider
blender -P build_blender.py -- --preset resolved --detail

# headless: model + STLs + docs
blender --background -P build_blender.py -- --preset resolved \
    --save vault.blend --export-stl printed/ \
    --fabrication FABRICATION.md --assembly ASSEMBLY.md

# renders (detail geometry is implied by --render)
blender --background -P build_blender.py -- --preset resolved \
    --render shots/ --views hero,night,cutaway,detail --samples 512 --res 2400
blender --background -P build_blender.py -- --preset resolved --render-steps steps/
```

## Environment traps — every one of these cost hours

**Cycles is not in the engine enum.** `RenderSettings.bl_rna.properties["engine"].enum_items`
lists only built-in engines; Cycles registers as an add-on and never appears,
even when `scene.cycles` exists and works. Testing membership silently sends
every render to EEVEE — whose screen-space refraction cannot see through a
glass slab, so the cabinet renders pitch black. **Assign the engine and verify
it took** (`_use_engine`), never pre-check that enum.

**GPU needs both halves.** `scene.cycles.device = 'GPU'` alone still renders on
the CPU. The add-on preference must also name a backend with devices enabled —
`enable_gpu()` does this and prints `[HEC] Cycles compute: ...`. Read that line;
if it says `CPU (no GPU devices found)`, a big render will take hours.

**Distro Blender has no denoiser.** Ubuntu's packaged build raises
"Build without OpenImageDenoiser" from the render operator itself.
`_render_still` catches it, disables denoising and re-renders — raise
`--samples` to compensate, because nothing is cleaning up the noise.

**Glass needs `visible_shadow = False`.** Light reaching a surface *through* a
refractive panel is a caustic path Cycles cannot sample with next-event
estimation, so a sealed cabinet renders dark however bright the studio is.
`glass_casts_no_shadow()` handles it. Do not "fix" a dark interior by cranking
lamp power.

**Do not bevel the glazing.** Rounding the edge of a 6 mm pane turns its border
into a lens and smears everything seen through it. `add_bevels()` skips glass
and diffusers deliberately.

**Blender version differences** are guarded, not assumed: `wm.stl_export` (4.2+)
falls back to `export_mesh.stl`; `BLENDER_EEVEE_NEXT` falls back to
`BLENDER_EEVEE`; `use_auto_smooth` (removed in 4.1) and `blend_method` are
`hasattr`-guarded. Guard with a real `if hasattr(...)` block — a ternary whose
else-branch reads the same attribute defeats the check and raises.

## Checks — run these before committing

```bash
python3 -m pyflakes *.py
python3 -m py_compile *.py
python3 -c "import spec as s, detail_layout as d; \
  print(d.check_board(*s.BOARD_SIZES['mATX']) or 'board clean'); \
  [print(p, s.check_envelope(s.build_spec(s.PRESETS[p])) or 'clean') \
   for p in ('brief','resolved','resolved-5')]"
```

`resolved` and `resolved-5` must come back clean. `brief` legitimately reports
`05_GPU_5 stands 4 mm proud of the top` — that is a real consequence of five
tiers at 120 mm pitch, and it stays reported.

**Testing Blender-side code without Blender:** `pip install bpy==4.5.13` gives a
full Blender as a Python module (Python 3.11). Headless rendering needs
`libegl1 libgl1 libglx-mesa0` installed. Never name a test script after a
stdlib module (`bisect.py` shadows `bisect`, which bpy imports — it re-executes
your script and dies with "InitGoogleLogging twice").

## The state of the geometry, stated plainly

Every exported STL is a **massing envelope**: correct outside dimensions and
mounting position, no bores, slots or fastener holes. A fan shroud exported
today is a solid block that would seal the airflow path. The manifest grades
parts "massing — simple form" or "massing — needs dimensioned CAD". Do not
describe any of them as print-ready.

Every dimension traces back to the design brief, not to measured hardware.
Nothing has been test-fitted.

## Open work, roughly in order

1. **Measure the real hardware** — tray rung pitch and ladder width, board
   mount pattern, longest GPU, PSU form factor, glass panel sizes. Everything
   downstream is assumption until this is done.
2. **Real CAD on the printed parts** — bores in the fan shrouds, windows in the
   filter frames, slots in the combs, the VESA hole pattern, fastener geometry
   on the clamps and cradles.
3. **Resolve the cooler/GPU clash** — a 79 mm tower cooler reaches 90 mm above
   the tray while the card sits at 31 mm. Low-profile coolers, taller risers,
   or offset the card in plan.
4. The `brief` preset's blockers (thermal, electrical, tier pitch) are designed
   out in `resolved`; see README.md for the numbers.
