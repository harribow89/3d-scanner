# HEC Node Vault — build manifest

Generated from `spec.py` (HEC-PC-002 Rev A). Envelope 400 x 400 x 840 mm, 5 nodes at 120 mm pitch, mATX boards, SFX PSUs.

- Parts in the assembly: **151**
- Printed parts: **106**
- Filament estimate: **~7547 g** (~7.55 spools, ~$226 at $30/kg) — bounding-box volume x 34% fill, so treat it as an upper bound.
- Shelf heights (mm): 215, 335, 455, 575, 695
- Glazed section: 225 to 765 mm

## Printed parts

| # | Part | Qty | Size (mm) | Material | Layer | Walls | Infill | Support | Status |
|--:|------|----:|-----------|----------|-------|------:|-------:|---------|--------|
| 1 | Node sled rail | 10 | 40 x 248 x 25 | PETG | 0.24 | 4 | 40% | no | massing — simple form |
| 2 | Tray rung clamp | 20 | 55 x 45 x 30 | PETG | 0.2 | 5 | 50% | no | massing — needs dimensioned CAD |
| 3 | GPU cradle | 5 | 60 x 50 x 90 | PETG | 0.24 | 4 | 35% | yes | massing — needs dimensioned CAD |
| 4 | PSU cradle | 5 | 112 x 137 x 22 | ASA | 0.24 | 4 | 30% | no | massing — needs dimensioned CAD |
| 5 | Fan shroud | 5 | 130 x 130 x 28 | PETG | 0.28 | 3 | 20% | no | massing — simple form |
| 6 | LED corner diffuser | 12 | 22 x 22 x 173 | Clear PETG | 0.2 | 2 | 15% | no | massing — simple form |
| 7 | Glass edge clip | 12 | 45 x 45 x 18 | TPU 95A | 0.2 | 3 | 25% | no | massing — simple form |
| 8 | Corner bracket | 8 | 70 x 70 x 70 | PETG | 0.2 | 5 | 60% | yes | massing — needs dimensioned CAD |
| 9 | Cable comb | 10 | 90 x 22 x 14 | PETG | 0.2 | 3 | 20% | no | massing — simple form |
| 10 | VESA adapter plate | 1 | 140 x 12 x 140 | PETG | 0.2 | 5 | 60% | no | massing — simple form |
| 11 | Tray end cap | 2 | 158 x 68 x 20 | PETG | 0.24 | 3 | 25% | no | massing — simple form |
| 12 | Caster mount pad | 4 | 95 x 95 x 14 | PETG | 0.2 | 6 | 60% | no | massing — simple form |
| 13 | Display bezel rail | 6 | 180 x 25 x 45 | PETG | 0.24 | 3 | 15% | no | massing — needs dimensioned CAD |
| 13 | Display bezel stile | 4 | 45 x 25 x 175 | PETG | 0.24 | 3 | 15% | no | massing — needs dimensioned CAD |
| 14 | Dust filter frame | 2 | 140 x 140 x 12 | PETG | 0.24 | 3 | 20% | no | massing — simple form |

> **Every STL this project exports is a massing envelope**: correct outside dimensions and mounting position, but no bores, slots, counterbores or fastener holes. A fan shroud exported today is a solid block that would seal the airflow path, not duct it. Model the openings before printing anything for fit.


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

- **Major** (fit): Board + riser + GPU is ~151 mm per tier but the pitch is 120 mm. Raise the pitch to ~176 mm (and the envelope with it) or lay the GPUs flat on risers.
- **Major** (fit): A 79 mm tower cooler reaches 90 mm above the tray, but the card sits at 31 mm — they occupy the same space. Fit low-profile coolers (~45 mm), raise the card to 89 mm on taller risers, or offset the card so it clears the cooler in plan.
- **BLOCKER** (thermal): ~2417 W of heat needs about 283 CFM to hold a 15 K rise, but 3 x 120 mm exhaust fans give roughly 99 CFM through a grille. You need about 6 x 140 mm high-static-pressure fans, or accept a bigger temperature rise.
- **BLOCKER** (electrical): ~2417 W is about 10.5 A at 230 V — at or over a standard 10 A outlet circuit, before anything else on it. Needs its own circuit (or two), and inrush from five PSUs starting together will nuisance-trip a Type B RCBO; specify Type C.
- Note (weight): Estimated all-up mass ~52 kg (15 kg of that is glass). That is ~13 kg per caster — specify 75 mm casters rated 50 kg+ each, and lock them: the centre of mass is high.
- **Major** (stability): 840 mm tall on a 400 mm base is a 2.1:1 ratio with the mass up high. Widen the base, add outrigger feet, or plan to strap it to a wall.
- **Major** (fit): 05_GPU_5 stands 4 mm proud of the top

