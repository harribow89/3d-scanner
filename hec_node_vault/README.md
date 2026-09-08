# HEC Node Vault — parametric Blender model

Drawing **HEC-PC-002 Rev A**. A full interactive assembly of the 4–5 node glass
vault: every part placed, an explode slider that drives the whole thing apart
and back, all 3D-printed parts included, and STL export for the printed set.

Built from the design brief and the 28 part photos. Companion to
`exploded.html` (the three.js viewer) — same BOM numbering, same layout.

## Files

| File | What it is |
|------|------------|
| `spec.py` | Every dimension, position and explode vector, in millimetres. Pure Python — runs without Blender. Also does the fit / thermal / electrical / weight checks. |
| `build_blender.py` | Turns the spec into Blender objects, wires the explode slider, exports STLs and the manifest. |
| `fabrication.py` | The full fabrication manifest — printed **and** cut parts, cut list, bought-in fixings — plus the 15-step assembly sequence. Pure Python. |
| `render_studio.py` | Photoreal product rendering: real materials, bevels, studio lighting, Cycles. |
| `detail_layout.py` | Where every fan blade, RAM stick, connector and cable run sits. Pure Python, and it checks itself for clashes. |
| `detail.py` | Builds that detail in Blender and parents it to the placeholders. |
| `FABRICATION.md` | Every fabricated piece for the `resolved` preset, grouped by process. |
| `ASSEMBLY.md` | Step-by-step build sequence. |
| `manifest_brief.md` / `manifest_resolved.md` | Printed-parts manifests per preset. |
| `FABRICATION_brief.md` | Fabrication manifest for the brief as written. |

## Run it

**Interactive (this is the one you want):**

```bash
blender -P build_blender.py                      # brief as written
blender -P build_blender.py -- --preset resolved # the version that actually works
```

Then press **N** in the 3D viewport and open the **HEC** tab: an *Explode*
slider (0 = assembled, 1 = fully exploded), Assembled / Exploded buttons, a
"show printed parts only" toggle, and a readout of whatever part you click —
its BOM number, real size in mm, and print settings if it's a printed part.

The slider is a scene property (`hec_explode`) driving every object's location,
so you can keyframe it: set 0 at frame 1, 1 at frame 120, and you have the
exploded-assembly animation for the build video.

**Headless — model, STLs and manifest in one go:**

```bash
blender --background --factory-startup -P build_blender.py -- \
    --preset resolved --save vault.blend \
    --export-stl printed/ --manifest manifest.md
```

**Detail geometry** (implied by any `--render`, or on its own with `--detail`):

```bash
blender -P build_blender.py -- --preset resolved --detail
```

Adds ~590 objects on top of the 149 model parts: fan frames with twisted,
bent blades and corner mounts; GPU shrouds with two fans, a 46-fin stack,
PCIe bracket, backplate and 8-pin sockets; boards down to RAM sticks, tower
cooler, VRM and chipset heatsinks, slots, headers and chokes; PSU faces with
grille, IEC inlet, switch and modular panel; and cable runs as bevelled curves
that sag under their own weight from PSU to board and card.

Every detail object is parented to its placeholder, so the explode slider still
drives the lot, and `spec.py` — the fabrication truth — is untouched. Pass
`--no-detail` to render the plain block model.

**Photoreal product shots:**

```bash
blender --background -P build_blender.py -- --preset resolved \
    --render shots/ --views hero,front,detail,night --samples 256 --res 2400
```

Real materials (tinted glass with absorption, brushed aluminium, powder coat,
PETG with visible layer lines, emissive RGB), bevelled edges, a seamless
backdrop, four-light softbox rig and a depth-of-field camera, in Cycles. The
`night` view drops the key light so the corner lighting carries the shot.
Budget 10-30 minutes per frame at 256 samples on the desktop's GPU; drop to
`--samples 96 --engine EEVEE` for a fast look.

**The assembly sequence as images:**

```bash
blender --background -P build_blender.py -- --preset resolved --render-steps steps/
```

One image per build step from a fixed camera, each showing everything installed
up to that point — the machine growing, not a gallery of loose parts.

**Fabrication paperwork:**

```bash
python3 fabrication.py --preset resolved --manifest > FABRICATION.md
python3 fabrication.py --preset resolved --assembly > ASSEMBLY.md
```

**Just the numbers, no Blender:**

```bash
python3 spec.py                      # part counts, filament estimate, warnings
python3 spec.py --preset resolved --manifest   # full printed-parts manifest
python3 spec.py --json               # the whole spec as JSON
```

## Presets

| Preset | Nodes | Envelope (W×D×H) | Pitch | Fans | Notes |
|--------|------:|------------------|------:|------|-------|
| `brief` | 5 | 400 × 400 × 840 | 120 | 3 × 120 exhaust | The brief as written. Reports 2 blockers + 2 majors. |
| `resolved` | 4 | 500 × 500 × 1145 | 180 | 6 × 140 exhaust | Fit and thermal blockers designed out. |
| `resolved-5` | 5 | 500 × 500 × 1325 | 180 | 6 × 140 exhaust | All five nodes; 1.3 m tall. |

## What the checks found

`spec.py` runs first-principles checks on the parameters. On the brief as
written:

- **Thermal (blocker).** Five nodes is roughly 2.4 kW of heat. Holding a 15 K
  rise needs about 283 CFM; three 120 mm fans through a grille give about
  99 CFM. You need six 140 mm high-static-pressure fans, or a much bigger
  temperature rise inside a sealed glass box.
- **Electrical (blocker).** ~2.4 kW is ~10.5 A at 230 V — at or over a standard
  10 A outlet circuit before anything else is on it. It needs its own circuit,
  and five PSUs starting together will nuisance-trip a Type B RCBO. Specify
  Type C.
- **Tier pitch (major).** Board + riser + GPU is ~151 mm per node against a
  120 mm pitch. Either the pitch goes to ~180 mm (and the case gets taller) or
  the GPUs lie flat on risers.
- **PSU fit.** Five ATX units will not fit a 400 × 400 plinth — it holds four.
  SFX units rotated 90° do fit. The spec picks the better orientation itself.
- **Stability (major).** 840 mm on a 400 mm base is 2.1:1 with the mass high up.
  Outrigger feet, a wider base, or a wall strap.
- **Cooler vs card (major).** A 79 mm tower cooler reaches 90 mm above the
  tray while the card sits at 31 mm — they want the same space. Low-profile
  coolers (~45 mm), taller risers, or offset the card in plan. This one only
  surfaced once the detail model put a real cooler on the board.
- **Weight.** ~52 kg all-up on the brief numbers, ~60 kg resolved, about a
  quarter of it glass. Casters need to be rated 50 kg+ each.

`--preset resolved` clears every blocker. Stability stays flagged — a tall glass
tower is inherently tippy, and that wants outriggers rather than a parameter
change.

## What gets fabricated

146 pieces on the `resolved` preset: **103 printed**, **43 cut**, plus 12 lines
of bought-in hardware. `FABRICATION.md` has the lot, grouped by process:

| Process | Pieces | What |
|---------|-------:|------|
| Order in | 5 | The glass — 4 panes + top, cut and toughened by the supplier |
| Saw cut | 9 | Corner posts, tray spine, extra rungs |
| Sheet metal | 21 | Plinth shell and decks, top cap, vent grille, fan plates |
| 3D print | 103 | The bracketry — see below |
| Cut by hand | 8 | Filter mesh, LED strip runs |

Estimated build time is ~30 hours across 15 steps.

## The printed parts

14 printed part types, ~95 pieces, roughly 10 kg of filament on the resolved
preset (bounding-box volume × 34% fill — treat it as an upper bound; real
slicing will come in well under). Full table with material, layer height, walls,
infill and supports is in `manifest_resolved.md`.

The set: node sled rails, tray rung clamps, GPU cradles, PSU cradles, fan
shrouds, corner LED diffusers, glass edge clips, corner brackets, cable combs,
a VESA adapter plate, tray end caps, caster pads, display bezel halves, and dust
filter frames.

**Read this before you print anything.** Parts are generated as parametric
massing with correct bounding sizes and mounting positions. The manifest marks
each one `ready to slice` or `DRAFT — needs CAD`:

- **Ready** (comb, caster pad, fan shroud, LED diffuser, VESA plate, filter
  frame, tray cap, glass clip): simple enough that the generated geometry is
  close to final — you still need to confirm hole positions against the real
  parts.
- **Draft** (rung clamps, GPU cradle, PSU cradle, corner brackets, display
  bezel): these need dimensioned CAD against the actual tray, cards and PSUs.
  The model gives you the envelope and position; it does not give you the
  fastener geometry.

Nothing here has been test-fitted, because none of the real dimensions have been
measured yet — every size traces back to the brief's assumptions.

## Design notes and suggestions

**Cooling.** Bottom-to-top chimney is the right instinct with a vertical stack.
Add a baffle per tier so each node pulls its own air instead of the bottom node
heating everything above it, and turn the GPUs to exhaust into the chimney
rather than at the glass. Six 140 mm fans at low RPM are quieter than three at
high RPM for the same airflow.

**Dust vs. flow.** Slight positive pressure (more intake than exhaust) keeps
dust off the inside of the glass, which matters a lot on a display piece — but
it fights the chimney. The `resolved` preset runs 4 intake / 6 exhaust, i.e.
chimney-biased. Worth a decision either way.

**Glass.** Tempered glass cannot be cut or drilled after tempering — every hole
and cutout has to be specified before it goes in the oven. Get that wrong and
you buy the panel twice. If the front needs service access, decide now: hinged
front pane on a piano hinge with gas struts is the clean answer, and it changes
the corner bracket design.

**Electrical (your side of the fence).** Dedicated circuit, Type C RCBO for the
inrush, and bond the galvanised tray to earth — it is a conductive structure
holding mains-powered gear. A single internal IEC PDU with one master switch and
one ingress point beats five leads out the back, and sequenced start (stagger
the PSUs by a second each) kills the inrush problem entirely. Label it.

**Fail-safes.** Fan-fail and over-temp alarms on the telemetry display, and a
thermal cut-out that drops the PDU. A sealed glass box full of 2.4 kW deserves a
smoke detector above it.

**Strain relief.** Add a printed strain-relief block per node so the board's
connectors don't carry loom weight down the tray — that is what kills
motherboard headers over time.

## What this model is not

It is a design and layout model: correct sizes, positions, clearances and
assembly relationships. With `--detail` the electronics are convincingly
detailed for a render, but they are *representative* hardware — a generic
two-fan card and a generic mATX board, not your specific parts. It is not
manufacturing CAD — no fasteners, no fillets, no sheet-metal bend allowances,
no real tray profile. Use it to settle the layout, check fit, print the
brackets, and make the exploded animation; use measured CAD for anything that
gets cut in metal or glass.

## Build sheet (interactive)

`build_sheet.html` is the shop-floor version: the 15-step sequence with a live
front elevation showing what is installed at each step, the full fabrication
manifest (order / saw / sheet / print / hand-cut, plus bought-in hardware), and
the model's own checks. Ticks persist, and sync across devices when it is opened
as a published artifact.

It is generated from the same data as everything else — regenerate the payload
with `python3 fabrication.py --preset resolved --json` if parameters change.
