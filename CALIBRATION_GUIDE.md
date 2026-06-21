# Multi-Camera Calibration Guide

Why your room/multi-cam scans come out "all over the place," and the exact steps
to fix it.

## The core problem: overlap

The three Xtions can only be fused into one cloud if **adjacent cameras see the
same things**. The calibrator (`calibrate_multi.py`) recovers each camera's
position by matching the *shared geometry* between neighbors. If a wide fan aims
the cameras outward, neighbors barely overlap, so:

- the calibration **can't solve** (it rejects pairs with overlap fitness < 0.40), and
- even hand-entered angles **can't stitch** the clouds, because there's almost
  nothing in common to stitch.

Measured on the current rig: adjacent pairs scored **0.04–0.05** (need ≥ 0.40).
That's the whole problem. Fix the aim and everything downstream improves.

There is a trade-off: **more overlap = better fusion but less total coverage.**
Aim for the *minimum* fan that still gives every adjacent pair ~30–50% overlap.

---

## Step-by-step

### 1. Build a good calibration scene
- Put **3-D structure** ~**1–1.5 m** in front of the rig: a cluttered shelf, a
  box fort, a chair + objects, a room corner with stuff. **Not** a blank wall and
  **not** a single flat plane — feature-based matching needs depth variation.
- Keep it **static** (nothing moves during capture — cameras are captured one at
  a time).

### 2. Aim the cameras for overlap
- Toe the cameras **inward** so each adjacent pair looks at a **shared region**
  of the scene, overlapping roughly **30–50%**.
- `camera1` is the reference (world root). `camera2` should bridge `camera1` and
  `camera3` (see `cameras.json` — roster order must match physical left→right
  fan order).

### 3. Check the overlap BEFORE calibrating
```bash
cd /home/jim/Desktop/scanner
.venv/bin/python overlap_check.py
```
It captures each camera and reports per pair:
- **GOOD** (≥0.40) — ready to calibrate
- **MARGINAL** (0.25–0.40) — toe in a bit more / add structure
- **POOR** (<0.25) — re-aim; neighbors barely share a view

Nudge the cameras and re-run until **every adjacent pair is GOOD**. To re-test
the clouds you just captured without re-capturing, add `--offline`. To check just
one pair: `.venv/bin/python overlap_check.py camera1 camera2`.

### 4. Calibrate
Once all pairs are GOOD:
```bash
.venv/bin/python calibrate_multi.py
```
It captures each camera, solves the full 6-DOF transforms (FPFH+RANSAC→ICP,
chained pairwise), and writes `output/camera_extrinsics.json`. Watch the printed
`fit=` per pair — they should be ≥0.40 with a physically small baseline
(t ≈ a few cm, not metres).

### 5. Verify the fused alignment
```bash
.venv/bin/python fuse_check.py
.venv/bin/python view_cloud.py output/alignment_check.ply
```
The three clouds should line up into one coherent scene. Or run live:
```bash
./run_scanner_docker.sh multi
```

### 6. Then do a room scan
With good extrinsics, capture sweeps and build:
```bash
./run_scanner_docker.sh station capture   # repeat from each tripod position
./run_scanner_docker.sh station build
.venv/bin/python view_cloud.py output/room.ply
```

---

## Troubleshooting

- **Clouds fan the wrong way after calibration** — the roster's physical order is
  off. Swap `port` entries in `cameras.json`, or flip the sign of every `qz` in
  `output/camera_extrinsics.json`.
- **A pair stays POOR no matter what** — the fan is too wide for that pair to
  ever overlap. Either narrow the fan (less coverage, better fusion) or accept
  that those two cameras can't be auto-calibrated to each other.
- **Calibration `fit` is good but fusion still looks off** — re-run with the
  cameras aimed at a scene with more 3-D relief; flat/low-texture scenes give
  high-but-misleading fits.
- **Single-camera handheld scans drift** — that's a different path (RTAB-Map
  SLAM), tuned via `RTAB_TUNE` in `run_scanner_docker.sh`. Move slowly, keep the
  object 0.5–3 m away, and ensure good lighting/texture.

## Hardware note
All three Xtions share one USB-2 controller, so they **cannot stream
simultaneously** at usable resolution — every tool here captures them **one at a
time**, which is why the scene must stay still. Simultaneous 3-cam streaming
would require a separate PCIe USB controller card.
