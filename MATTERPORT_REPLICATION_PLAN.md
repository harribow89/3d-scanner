# Replicating Matterport scan quality with this scanner

Derived from a static reverse-engineering of the Matterport Capture app v2.80.1
(full report: `~/Desktop/matterport work/analysis/FINDINGS.md`). This is the
actionable upgrade path for *this* project.

## The one-sentence takeaway

Your hardware is the **same sensor class Matterport uses** (PrimeSense/Carmine
structured light — their code literally calibrates by "xtion serial"). The quality gap
is **100 % in processing**, and every processing stage they use has an open equivalent
you can run on this rig. Nothing about the gap is licensed or sensor-locked.

## Matterport's pipeline vs. ours

| Stage | Matterport (EOS) | This scanner today | Gap to close |
|---|---|---|---|
| Capture | Discrete **sweeps** at fixed tripod positions | Continuous handheld RTAB-Map SLAM | Add a **station/sweep mode** |
| Depth | Structured light **+ per-sensor `.vdc` correction** | Raw OpenNI2 depth | Add per-sensor depth correction |
| Depth cleanup | Neural depth+**normals**+**uncertainty**; fuse only confident px | none | Add neural depth + confidence gating |
| Specular | **Mirror/window segmentation**, masked out | none (glass = garbage depth) | Add glass/mirror masking |
| Registration | Feature + weighted ICP + Ceres BA across sweeps | FPFH+RANSAC→ICP (in `calibrate_multi.py`) | Reuse it for sweep-to-sweep + add pose-graph |
| Fusion | TSDF → mesh | RTAB-Map / Open3D | mostly there |
| "Idealize" | Wall/floor planes, `auto_floor_separation`, furniture kept | none | Add plane idealization + floor split |

## Staged plan (each stage is independently useful)

### Stage 1 — Station/sweep capture mode  ✅ IMPLEMENTED  *(biggest quality win, lowest effort)*
For a static room, stop scanning like a handheld SLAM and scan like Matterport. Implemented in
**`station_scan.py`** (host, `.venv`+Open3D) + **`capture_station.py`** (container frame-grab),
wired as **`./run_scanner_docker.sh station …`**:

- At each tripod position, **stack N depth frames per camera** (default 12) and voxel-average
  them — kills structured-light temporal jitter (≈√N noise reduction).
- **Fuses the multi-camera clouds into one "sweep"** using the existing
  `output/camera_extrinsics.json` (same data `multi_camera.launch.py` uses).
- **Globally registers sweeps** reusing `calibrate_multi.py`'s FPFH+RANSAC→point-to-plane ICP,
  then runs **Open3D pose-graph `global_optimization`** (the open analogue of EOS's Ceres BA;
  no new deps) → one fused `output/room.ply`.

Usage:
```bash
# at each tripod position (move the rig between sweeps):
./run_scanner_docker.sh station capture      # -> output/sweeps/sweep_00.ply, _01, …
./run_scanner_docker.sh station list
./run_scanner_docker.sh station build         # register + fuse -> output/room.ply
python3 view_cloud.py output/room.ply
```
Verified offline: multi-cam fusion + a synthetic-transform round-trip recovered a 20°/15 cm
move at fit=1.00, rmse=1.6 mm. Next refinements: image-space (not cloud-space) frame averaging,
and TSDF/Poisson meshing of `room.ply` (feeds Stage 3/5).

### Stage 2 — Per-sensor depth correction (`.vdc` analogue)
Matterport keeps a per-serial correction (corrected intrinsics + per-pixel ray table +
baseline deltas). Cheap version for us:
- Calibrate each Xtion's depth intrinsics once (OpenCV, planar target) and store a per-serial
  JSON (extend the existing `calibration.json` / `cameras.json`).
- Apply a per-pixel depth bias/scale map (flat-wall capture at known distances → fit the
  residual). Open3D/numpy. Removes the structured-light "bowing" on flat walls.

### Stage 3 — Neural depth + confidence + normals
Their `DepthUCFPredictor` outputs `depth_map` + `normals_map` + `depth_err_map` and fuses
only confident pixels. Open replacements (all run on the x86 box, GPU optional):
- **Depth Anything V2** or **Metric3D v2** for dense depth/normals from the RGB panorama.
- Fuse RGB-D = structured-light depth where the sensor is confident, neural depth to **fill
  holes** (windows, dark/far/thin surfaces). Gate by agreement between the two as a poor-man's
  `depth_err_map`. New dep: `torch` + the model weights (public).
- Use predicted **normals** to improve TSDF/Poisson surface quality.

### Stage 4 — Mirror/window masking
Glass and mirrors are the #1 destroyer of structured-light depth; Matterport detects and
masks them (`{window|mirror}` ontology via Grounded-SAM / RT-DETR).
- Run **SAM2 + Grounding DINO** (or a small YOLO/RT-DETR fine-tune) on each panorama, prompt
  `"mirror, window, glass"`, and **zero out depth** under those masks before fusion. Removes
  the phantom geometry behind glass.

### Stage 5 — "Idealize" (floor plan / clean walls)
Matterport builds `VirtualWall`/`WallType`/`Floor` + `auto_floor_separation`.
- RANSAC plane extraction (Open3D) on the fused cloud → snap near-vertical clusters to clean
  wall planes, near-horizontal to floor/ceiling; split floors by height histogram. Keep
  furniture clusters as-is. Optional, mostly for floor-plan output.

## What we will NOT do (and why)
- **Lift the EOS `.so` libs into the scanner** — ARM64-only, compiled C++, anti-tamper,
  cloud/license-tied. Not portable to x86 ROS, and not legal to redistribute.
- **Extract their model weights** — encrypted at rest; keys live in the runtime. Unnecessary:
  the *same public models* (DINOv2, Depth Anything, SAM, RT-DETR) are what they're using.
- **Talk to a Matterport Pro over WiFi (`10.77.80.1`)** — irrelevant; we have the raw sensors,
  not an assembled Pro unit.

## First concrete step
Confirm the pulled cameras enumerate, then prototype Stage 1:
```bash
lsusb | grep -iE '1d27|2bc5|primesense|orbbec|xtion'   # confirm VID:PID of the Matterport-pulled modules
./run_scanner_docker.sh camera                          # verify a live stream via OpenNI2
```
If they show as `1d27:06xx` (PrimeSense) or `2bc5:xxxx` (Orbbec), the existing OpenNI2 stack
drives them as-is and Stage 1 is pure software.
