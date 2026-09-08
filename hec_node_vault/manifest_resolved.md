# HEC Node Vault — build manifest

Generated from `spec.py` (HEC-PC-002 Rev A). Envelope 500 x 500 x 1145 mm, 4 nodes at 180 mm pitch, mATX boards, SFX PSUs.

- Parts in the assembly: **149**
- Printed parts: **103**
- Filament estimate: **~9145 g** (~9.14 spools, ~$274 at $30/kg) — bounding-box volume x 34% fill, so treat it as an upper bound.
- Shelf heights (mm): 355, 535, 715, 895
- Glazed section: 275 to 1055 mm

## Printed parts

| # | Part | Qty | Size (mm) | Material | Layer | Walls | Infill | Support | Status |
|--:|------|----:|-----------|----------|-------|------:|-------:|---------|--------|
| 1 | Node sled rail | 8 | 40 x 248 x 25 | PETG | 0.24 | 4 | 40% | no | ready to slice |
| 2 | Tray rung clamp | 16 | 55 x 45 x 30 | PETG | 0.2 | 5 | 50% | no | DRAFT — needs CAD |
| 3 | GPU cradle | 4 | 60 x 50 x 90 | PETG | 0.24 | 4 | 35% | yes | DRAFT — needs CAD |
| 4 | PSU cradle | 4 | 112 x 137 x 22 | ASA | 0.24 | 4 | 30% | no | DRAFT — needs CAD |
| 5 | Fan shroud | 10 | 150 x 150 x 28 | PETG | 0.28 | 3 | 20% | no | ready to slice |
| 6 | LED corner diffuser | 12 | 22 x 22 x 253 | Clear PETG | 0.2 | 2 | 15% | no | ready to slice |
| 7 | Glass edge clip | 12 | 45 x 45 x 18 | TPU 95A | 0.2 | 3 | 25% | no | ready to slice |
| 8 | Corner bracket | 8 | 70 x 70 x 70 | PETG | 0.2 | 5 | 60% | yes | DRAFT — needs CAD |
| 9 | Cable comb | 8 | 90 x 22 x 14 | PETG | 0.2 | 3 | 20% | no | ready to slice |
| 10 | VESA adapter plate | 1 | 140 x 12 x 140 | PETG | 0.2 | 5 | 60% | no | ready to slice |
| 11 | Tray end cap | 2 | 158 x 68 x 20 | PETG | 0.24 | 3 | 25% | no | ready to slice |
| 12 | Caster mount pad | 4 | 95 x 95 x 14 | PETG | 0.2 | 6 | 60% | no | ready to slice |
| 13 | Display bezel rail | 6 | 180 x 25 x 45 | PETG | 0.24 | 3 | 15% | no | DRAFT — needs CAD |
| 13 | Display bezel stile | 4 | 45 x 25 x 175 | PETG | 0.24 | 3 | 15% | no | DRAFT — needs CAD |
| 14 | Dust filter frame | 4 | 160 x 160 x 12 | PETG | 0.24 | 3 | 20% | no | ready to slice |

### What each part is for

- **Node sled rail** — Carries a node on M3 standoffs; clamps to the tray rung.
- **Tray rung clamp** — Clamps the sled rail to the ladder rung; M6 bolt through.
- **GPU cradle** — Takes the far end of the card so the slot carries no cantilever load.
- **PSU cradle** — Locates and isolates the PSU; ASA because it sits in the hot air path.
- **Fan shroud** — Seals fan to grille so air is not recirculated around the frame.
- **LED corner diffuser** — Print in clear PETG, 2 walls, no top surface — diffuses the strip into an even line.
- **Glass edge clip** — Flexible clip — grips the pane without a hard glass-on-metal contact.
- **Corner bracket** — Structural corner — ties post, plinth and cap. Print solid-ish; this one carries the glass load.
- **Cable comb** — Dresses the loom into lanes; clips over the tray side rail.
- **VESA adapter plate** — Adapts the arm to the display's VESA pattern. Print flat, 5 walls.
- **Tray end cap** — Caps the cut tray ends — no sharp galvanised edges near cables.
- **Caster mount pad** — Spreads ~15 kg per corner into the plinth floor. High wall count.
- **Display bezel rail** — Bezel frame, segmented to fit the bed; dowel and glue the joints.
- **Display bezel stile** — Bezel frame, segmented to fit the bed; dowel and glue the joints.
- **Dust filter frame** — Holds a cut mesh square; slides out from the front for cleaning.

## Checks

- Note (weight): Estimated all-up mass ~60 kg (27 kg of that is glass). That is ~15 kg per caster — specify 75 mm casters rated 50 kg+ each, and lock them: the centre of mass is high.
- **Major** (stability): 1145 mm tall on a 500 mm base is a 2.3:1 ratio with the mass up high. Widen the base, add outrigger feet, or plan to strap it to a wall.

