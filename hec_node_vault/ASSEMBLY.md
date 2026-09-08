# HEC Node Vault — assembly sequence

15 steps, roughly 30 hours of work. Each step ends somewhere you can safely walk away.

## Step 1 — Measure and check before you make anything

*1 h*

- Measure the real tray: ladder width, rung pitch, rung hole spacing.
- Measure a board (mount-hole pattern), the longest GPU, and a PSU.
- Check those against spec.py's parameters and re-run it if they differ.
- Only then order the glass — it is the long-lead, non-returnable item.

**Tools:** tape, vernier, the measure sheet

> Every dimension in this model came from the brief, not from your hardware. This step is what makes the rest true.

## Step 2 — Cut the spine

*1 h*

- Cut the tray to length and deburr every cut strand.
- If the tray's rung pitch does not match the tier pitch, add rungs.
- Fit the printed end caps top and bottom.

**Printed parts (2):** P11_Tray_Cap_Bottom, P11_Tray_Cap_Top

**Cut parts:** T01 Cable tray — spine; T02 Tray rung

**Tools:** tray cutter or grinder, file, vacuum

> Vacuum the swarf now. Galvanised filings inside a running machine are a short waiting to happen.

## Step 3 — Build the plinth

*3 h*

- Assemble the folded shell: sides, ends, floor, deck.
- Bolt the printed caster pads through the floor, then the casters.
- Fit the 4 intake fan plates, shrouds and filter frames.
- Stand it up and check it does not rock before anything goes on top.

**Printed parts (12):** P12_Caster_Pad_LB, P12_Caster_Pad_LF, P12_Caster_Pad_RB, P12_Caster_Pad_RF, P05_Fan_Shroud_In_1, P05_Fan_Shroud_In_2, P05_Fan_Shroud_In_3, P05_Fan_Shroud_In_4, P14_Filter_Frame_1, P14_Filter_Frame_2, P14_Filter_Frame_3, P14_Filter_Frame_4

**Cut parts:** S01 Plinth side panel; S02 Plinth end panel; S03 Plinth deck (top); S04 Plinth floor; S08 Fan mounting plate; M01 Dust filter mesh

**Tools:** drill, rivet gun or M5 hardware, deburring tool

## Step 4 — Power in the plinth

*2 h*

- Drop the 4 PSUs into their printed cradles.
- Mount the PDU; run one mains lead in through a grommeted entry.
- Bond the chassis and the tray to earth, and test continuity.
- Label every outlet to its node before the cables disappear upward.

**Printed parts (4):** P04_PSU_Cradle_1, P04_PSU_Cradle_2, P04_PSU_Cradle_3, P04_PSU_Cradle_4

**Tools:** crimpers, multimeter, label maker

> Your trade, your call — but the model's own check puts this at ~8.4 A (1933 W) at full tilt on 4 nodes. Dedicated circuit, Type C RCBO for the inrush, and stagger the PSU starts if you can.

## Step 5 — Stand the spine

*1 h*

- Bolt the tray to the plinth deck, plumb in both axes.
- Check it for rack under hand load — everything above hangs off this.

**Cut parts:** T01 Cable tray — spine

**Tools:** spirit level, spanner set

## Step 6 — Posts and corners

*2 h*

- Cut the four posts in one setup so they are identical.
- Fit printed corner brackets top and bottom on each post.
- Stand the posts on the plinth and square the frame diagonally.

**Printed parts (8):** P08_Corner_Bracket_LB_Bot, P08_Corner_Bracket_LB_Top, P08_Corner_Bracket_LF_Bot, P08_Corner_Bracket_LF_Top, P08_Corner_Bracket_RB_Bot, P08_Corner_Bracket_RB_Top, P08_Corner_Bracket_RF_Bot, P08_Corner_Bracket_RF_Top

**Cut parts:** E01 Corner post

**Tools:** mitre saw, hex keys, square

> Measure both diagonals and make them equal. Out of square here means the glass will not drop into its slots later.

## Step 7 — Node sleds

*4 h*

- Assemble a pair of printed rails per tier with their rung clamps.
- Clamp each sled to its rung, working bottom to top (4 tiers).
- Fit M3 standoffs to match the board pattern, then the boards.
- Leave the top tier until last — it is your access for everything else.

**Printed parts (24):** P01_Node_Rail_1L, P01_Node_Rail_1R, P01_Node_Rail_2L, P01_Node_Rail_2R, P01_Node_Rail_3L, P01_Node_Rail_3R, P01_Node_Rail_4L, P01_Node_Rail_4R, P02_Rung_Clamp_1_1, P02_Rung_Clamp_1_2, P02_Rung_Clamp_1_3, P02_Rung_Clamp_1_4, P02_Rung_Clamp_2_1, P02_Rung_Clamp_2_2, P02_Rung_Clamp_2_3, P02_Rung_Clamp_2_4, P02_Rung_Clamp_3_1, P02_Rung_Clamp_3_2, P02_Rung_Clamp_3_3, P02_Rung_Clamp_3_4, P02_Rung_Clamp_4_1, P02_Rung_Clamp_4_2, P02_Rung_Clamp_4_3, P02_Rung_Clamp_4_4

**Tools:** hex keys, M3 driver

## Step 8 — GPUs and cradles

*1 h*

- Fit each card on its riser, then slide the printed cradle under the far end and shim it until the card sits level.
- Check the card clears the tier above with the fans spinning.

**Printed parts (4):** P03_GPU_Cradle_1, P03_GPU_Cradle_2, P03_GPU_Cradle_3, P03_GPU_Cradle_4

**Tools:** hex keys

> The cradle must carry the card's weight, not preload the slot. Snug, not jacked up.

## Step 9 — Loom and dress

*3 h*

- Run each node's PSU leads down the rear of the tray.
- Clip the printed combs on and dress the loom into lanes.
- Add strain relief at each board so the loom's weight never hangs off a connector.
- Leave a service loop per tier so a node can slide out powered down.

**Printed parts (8):** P09_Cable_Comb_1, P09_Cable_Comb_2, P09_Cable_Comb_3, P09_Cable_Comb_4, P09_Cable_Comb_5, P09_Cable_Comb_6, P09_Cable_Comb_7, P09_Cable_Comb_8

**Tools:** cable ties, side cutters

## Step 10 — Smoke test — before any glass goes on

*2 h*

- Power one node. Confirm POST, then shut down.
- Repeat per node, then run all of them together for an hour.
- Watch the PDU load and feel for hot spots at the top tier.

**Tools:** multimeter, IR thermometer, a fire blanket, honestly

> Do not skip the order here. Everything is reachable now and nothing is reachable once the panes are in.

## Step 11 — Lighting

*2 h*

- Clean the posts, run one strip per corner, feed to the controller.
- Clip the printed diffusers over the strips.
- Test the full run before the glass traps it.

**Printed parts (16):** P06_LED_Diffuser_LB_1, P06_LED_Diffuser_LB_2, P06_LED_Diffuser_LB_3, P06_LED_Diffuser_LB_4, P06_LED_Diffuser_LF_1, P06_LED_Diffuser_LF_2, P06_LED_Diffuser_LF_3, P06_LED_Diffuser_LF_4, P06_LED_Diffuser_RB_1, P06_LED_Diffuser_RB_2, P06_LED_Diffuser_RB_3, P06_LED_Diffuser_RB_4, P06_LED_Diffuser_RF_1, P06_LED_Diffuser_RF_2, P06_LED_Diffuser_RF_3, P06_LED_Diffuser_RF_4

**Cut parts:** M02 LED strip run

**Tools:** soldering iron, isopropyl

## Step 12 — Top cap and exhaust

*2 h*

- Assemble the cap; fit the 6 x 140 mm fans into their shrouds and plates.
- Fit the perforated grille.
- Sit the cap on the posts but leave it loose — the top pane goes in first.

**Printed parts (6):** P05_Fan_Shroud_Ex_1, P05_Fan_Shroud_Ex_2, P05_Fan_Shroud_Ex_3, P05_Fan_Shroud_Ex_4, P05_Fan_Shroud_Ex_5, P05_Fan_Shroud_Ex_6

**Cut parts:** S05 Top cap side panel; S06 Top cap end panel; S07 Top vent grille; S08 Fan mounting plate

**Tools:** drill, hex keys

## Step 13 — Glazing — two people

*3 h*

- Run foam glazing tape in every channel first.
- Top pane, then rear, then the two sides, then the front.
- Fit the printed clips as you go; they should grip, not clamp.
- Bolt the top cap down once the top pane is captive.

**Printed parts (12):** P07_Glass_Clip_B1, P07_Glass_Clip_B2, P07_Glass_Clip_B3, P07_Glass_Clip_F1, P07_Glass_Clip_F2, P07_Glass_Clip_F3, P07_Glass_Clip_L1, P07_Glass_Clip_L2, P07_Glass_Clip_L3, P07_Glass_Clip_R1, P07_Glass_Clip_R2, P07_Glass_Clip_R3

**Cut parts:** G01 Glass pane — front; G02 Glass pane — rear; G03 Glass pane — side; G04 Glass pane — top

**Tools:** suction cups, glazing tape, gloves

> Two people and suction cups. A 5 kg toughened pane that touches bare metal or gets dropped an inch is gone, and it is the longest-lead part in the build.

## Step 14 — Display

*1 h*

- Mount the arm to the top rail, then the printed VESA plate.
- Hang the display, join the bezel segments around it.
- Route the display feed down inside the rear post.

**Printed parts (11):** P10_VESA_Plate, P13_Bezel_Bottom_1, P13_Bezel_Bottom_2, P13_Bezel_Bottom_3, P13_Bezel_Left_1, P13_Bezel_Left_2, P13_Bezel_Right_1, P13_Bezel_Right_2, P13_Bezel_Top_1, P13_Bezel_Top_2, P13_Bezel_Top_3

**Tools:** hex keys, VESA screws

## Step 15 — Commission

*2 h*

- Set fan curves from the node telemetry, not fixed RPM.
- Soak test at full load for two hours; log the top-tier temperature.
- Add a fan-fail and over-temp alarm to the dashboard.
- Label the circuit at the board, and photograph the loom before you forget how it goes back.

**Tools:** IR thermometer, logging on the telemetry display

> If the top tier runs hot at soak, that is the thermal check coming true — add exhausts before you add nodes.


