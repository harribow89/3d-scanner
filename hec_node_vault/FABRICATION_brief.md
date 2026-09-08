# HEC Node Vault — fabrication manifest

HEC-PC-002 Rev A · 400 x 400 x 840 mm · 5 nodes · mATX boards · SFX PSUs

**143 fabricated pieces**: 106 printed, 37 cut. Plus 12 lines of bought-in hardware. Estimated build time 31 h across 15 steps.

## Cut parts

### Order in (supplier cuts it)

| Ref | Part | Qty | Size (mm) | Material | Stock / tooling |
|-----|------|----:|-----------|----------|-----------------|
| G01 | Glass pane — front | 1 | 350 x 540 x 6.0 | tinted tempered glass | supplier cut to size, edges polished, corners eased |
| G02 | Glass pane — rear | 1 | 350 x 540 x 6.0 | tinted tempered glass | supplier cut to size |
| G03 | Glass pane — side | 2 | 350 x 540 x 6.0 | tinted tempered glass | supplier cut to size |
| G04 | Glass pane — top | 1 | 350 x 350 x 6.0 | tinted tempered glass | supplier cut to size |

- **G01** — Tinted. Specify ALL holes and cutouts before toughening — tempered glass cannot be drilled or cut afterwards.
- **G02** — Consider a perforated metal rear panel instead, to let the chimney cross-flow; it is the face nobody sees.
- **G03** — Left and right, identical.
- **G04** — Sits under the top cap; carries no load but must be toughened — anything above head height that can fall is safety glass.

### Saw cut + deburr

| Ref | Part | Qty | Size (mm) | Material | Stock / tooling |
|-----|------|----:|-----------|----------|-----------------|
| E01 | Corner post | 4 | 25 x 25 x 560.0 | 25 x 25 aluminium extrusion | 2 m lengths |
| T01 | Cable tray — spine | 1 | 150 x 60 x 540.0 | 150 mm ladder tray | 3 m length |
| T02 | Tray rung | 5 | 150 x 25 x 3.0 | tray rung stock or 25 x 3 flat bar | from offcut |

- **E01** — Cut all four in one setup so they are identical — any difference here twists the whole case.
- **T01** — Cut to length, deburr every cut strand, fit the printed end caps. Galvanised swarf in a running machine is a short circuit waiting to happen.
- **T02** — Only if the tray's own rung pitch does not match the tier pitch — measure before ordering.

### Sheet metal — laser/CNC cut and fold

| Ref | Part | Qty | Size (mm) | Material | Stock / tooling |
|-----|------|----:|-----------|----------|-----------------|
| S01 | Plinth side panel | 2 | 400 x 150 x 1.5 | 1.5 mm aluminium or steel | 1.5 mm sheet |
| S02 | Plinth end panel | 2 | 400 x 150 x 1.5 | 1.5 mm aluminium or steel | 1.5 mm sheet |
| S03 | Plinth deck (top) | 1 | 400 x 400 x 2.0 | 2 mm aluminium or steel | 2 mm sheet |
| S04 | Plinth floor | 1 | 400 x 400 x 2.0 | 2 mm aluminium or steel | 2 mm sheet |
| S05 | Top cap side panel | 2 | 400 x 75 x 1.5 | 1.5 mm aluminium | 1.5 mm sheet |
| S06 | Top cap end panel | 2 | 400 x 75 x 1.5 | 1.5 mm aluminium | 1.5 mm sheet |
| S07 | Top vent grille | 1 | 380 x 380 x 1.5 | 1.5 mm perforated aluminium | perforated sheet |
| S08 | Fan mounting plate | 5 | 150 x 150 x 1.5 | 1.5 mm aluminium | 1.5 mm sheet |

- **S01** — Front panel carries the intake cutouts; rear is plain.
- **S03** — Takes the tray load and the node stack above it — 2 mm, and add a folded return on all four edges for stiffness.
- **S04** — Caster mounts land here; see the caster pads.
- **S07** — Perforated stock from photos 1 & 2. Open area matters more than hole size — aim for 40%+ or it strangles the fans.
- **S08** — 120 mm bore plus a 105 mm PCD of M4 clearance holes. The punched knockout plate in photo 3 is the same idea.

### Cut by hand

| Ref | Part | Qty | Size (mm) | Material | Stock / tooling |
|-----|------|----:|-----------|----------|-----------------|
| M01 | Dust filter mesh | 2 | 140 x 140 x 1.0 | nylon or aluminium mesh | mesh sheet |
| M02 | LED strip run | 4 | 10 x 5 x 520.0 | addressable RGB strip | 5 m reel |

- **M01** — Cut squares to drop into the printed filter frames.
- **M02** — Cut on the marked cut lines only, one run per corner post.

### 3D printed

| Ref | Part | Qty | Size (mm) | Material | Layer | Infill | Support | Status |
|----:|------|----:|-----------|----------|-------|-------:|---------|--------|
| P01 | Node sled rail | 10 | 40 x 248 x 25 | PETG | 0.24 | 40% | no | ready |
| P02 | Tray rung clamp | 20 | 55 x 45 x 30 | PETG | 0.2 | 50% | no | DRAFT |
| P03 | GPU cradle | 5 | 60 x 50 x 90 | PETG | 0.24 | 35% | yes | DRAFT |
| P04 | PSU cradle | 5 | 112 x 137 x 22 | ASA | 0.24 | 30% | no | DRAFT |
| P05 | Fan shroud | 5 | 130 x 130 x 28 | PETG | 0.28 | 20% | no | ready |
| P06 | LED corner diffuser | 12 | 22 x 22 x 173 | Clear PETG | 0.2 | 15% | no | ready |
| P07 | Glass edge clip | 12 | 45 x 45 x 18 | TPU 95A | 0.2 | 25% | no | ready |
| P08 | Corner bracket | 8 | 70 x 70 x 70 | PETG | 0.2 | 60% | yes | DRAFT |
| P09 | Cable comb | 10 | 90 x 22 x 14 | PETG | 0.2 | 20% | no | ready |
| P10 | VESA adapter plate | 1 | 140 x 12 x 140 | PETG | 0.2 | 60% | no | ready |
| P11 | Tray end cap | 2 | 158 x 68 x 20 | PETG | 0.24 | 25% | no | ready |
| P12 | Caster mount pad | 4 | 95 x 95 x 14 | PETG | 0.2 | 60% | no | ready |
| P13 | Display bezel rail | 6 | 180 x 25 x 45 | PETG | 0.24 | 15% | no | DRAFT |
| P13 | Display bezel stile | 4 | 45 x 25 x 175 | PETG | 0.24 | 15% | no | DRAFT |
| P14 | Dust filter frame | 2 | 140 x 140 x 12 | PETG | 0.24 | 20% | no | ready |

## Bought in

| Ref | Item | Qty | Notes |
|-----|------|----:|-------|
| F01 | M6 x 20 bolt, nut, washers | 40 | Sled rails and clamps to the tray rungs. |
| F02 | M3 x 6 motherboard standoff + screw | 45 | Boards to the printed sled rails. |
| F03 | M4 x 30 fan screw | 20 | Fans through shroud into the mounting plate. |
| F04 | M5 T-nut + button head for extrusion | 32 | Corner brackets to the posts. |
| F05 | M8 x 25 bolt + nyloc | 16 | Casters and plinth corners. |
| F06 | 75 mm lockable caster, 50 kg+ rated | 4 | All four braked, not two — the mass is high. |
| F07 | Earth bonding lug + 4 mm² green/yellow | 2 | Tray and chassis to earth. Non-negotiable on a conductive frame holding mains gear. |
| F08 | IEC PDU, switched, with C13 outlets | 1 | One ingress for 5 PSUs. Sequenced switching if you can get it. |
| F09 | Glass suction cup (pair) | 1 | Hire or buy — do not hand-carry a 5 kg pane into a slot. |
| F10 | Closed-cell foam glazing tape, 3 mm | 1 | Between glass and any metal. Glass on bare metal cracks. |
| F11 | VESA M4 screw set | 1 | Display to the printed adapter plate. |
| F12 | Cable ties / hook-and-loop | 1 | Loom dressing down the tray. |

## Checks carried over from the model

- **Major** (fit): Board + riser + GPU is ~151 mm per tier but the pitch is 120 mm. Raise the pitch to ~176 mm (and the envelope with it) or lay the GPUs flat on risers.
- **Major** (fit): A 79 mm tower cooler reaches 90 mm above the tray, but the card sits at 31 mm — they occupy the same space. Fit low-profile coolers (~45 mm), raise the card to 89 mm on taller risers, or offset the card so it clears the cooler in plan.
- **BLOCKER** (thermal): ~2417 W of heat needs about 283 CFM to hold a 15 K rise, but 3 x 120 mm exhaust fans give roughly 99 CFM through a grille. You need about 6 x 140 mm high-static-pressure fans, or accept a bigger temperature rise.
- **BLOCKER** (electrical): ~2417 W is about 10.5 A at 230 V — at or over a standard 10 A outlet circuit, before anything else on it. Needs its own circuit (or two), and inrush from five PSUs starting together will nuisance-trip a Type B RCBO; specify Type C.
- Note (weight): Estimated all-up mass ~52 kg (15 kg of that is glass). That is ~13 kg per caster — specify 75 mm casters rated 50 kg+ each, and lock them: the centre of mass is high.
- **Major** (stability): 840 mm tall on a 400 mm base is a 2.1:1 ratio with the mass up high. Widen the base, add outrigger feet, or plan to strap it to a wall.
- **Major** (fit): 05_GPU_5 stands 4 mm proud of the top

