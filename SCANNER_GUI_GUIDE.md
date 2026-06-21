# Scanner GUI User Guide

## Quick Start

### Launch the GUI

```bash
cd /home/jim/Desktop/scanner
./launch_gui.sh
```

Or on the desktop, double-click `scanner_gui.desktop` if installed.

The main window displays **real-time telemetry** (scan status, node count, database size) and provides **six operational tabs**.

---

## Tabs Overview

### 1. Camera Preview

**Test the camera stream before full scans.**

- **Launch Live Preview (30 sec)** — Streams live depth + RGB from all connected Xtions, displays ROS topic list and point-cloud publish rate (~30 Hz for a single QVGA camera).
- **Capture Single Frame** — Grabs one depth frame and saves a PLY file. Use this to verify image quality.

**Output:** `output/single_frame.ply` (editable path).

---

### 2. Mapping

**Run full RTAB-Map SLAM scans.**

#### Quick SLAM Scan
- Continuously builds a 3D map as you move the camera.
- Move slowly and cover the scene methodically.
- Press **Stop / Cancel** (bottom of window) to finish and export.

#### Full SLAM GUI (Hands-Off)
- **Best for user comfort:** Launches the integrated ROS stack (camera + RTAB-Map node + rtabmap_viz in one window).
- **Auto-mapping** — begins the moment it opens; no "Start" button to press.
- Move the camera around your subject. Loop closures auto-correct drift.
- **Close the window to finish** — applies loop-closure optimization and prepares the map for export.

#### Object Capture Helper
- **Apply Object-Capture Preset** — tightens the scan defaults for single-object work: finer voxel size, lower max depth, and `tabletop_object` isolation.
- **Start Object Capture Scan** — applies the preset and launches the normal quick scan.
- Use this when the goal is one object rather than a full room.

#### Capture Guide
- **Coverage Progress** — a live meter that estimates how complete the scan is from RTAB-Map telemetry.
- **Checklist** — shows the current capture steps in order, so you can see when to orbit, raise the camera, and add a top pass.
- **Start Guided Object Scan** — combines the object preset with the live guide so the app can coach the scan toward export-ready coverage.

**Live Statistics:**
- **Keyframes** — pose-graph nodes (one per significant camera motion).
- **Links** — spatial constraints between nodes (odometry + loop closures).
- **Last Loop** — most recent loop-closure pair (e.g., "nodes 15↔22").

---

### 3. Export

**Save and post-process point clouds.**

#### Export Point Cloud
- **Output File** — Path and filename (default: `output/scan.ply`). Click **Browse...** to change.
- If you choose an `.stl` filename, the GUI still exports the cloud as a PLY behind the scenes and uses the same stem for STL generation.
- **Database** — Source RTAB-Map DB (default: current `rtabmap.db`).
- **Export Cloud** — Extracts the map from the DB and saves a PLY. The GUI passes the configured voxel and depth settings through to the export step.
- **View in 3D** — Opens the exported cloud in the embedded viewer (rotatable, downsampled for smooth rendering).
- **Generate STL** — Reprocesses the cleaned cloud into a triangle mesh and writes an STL file beside the cloud export.
- **Export + STL** — Runs export, object isolation, and STL generation in one step.
- The export pipeline writes `<name>_cloud.ply` automatically, so the GUI resolves the real file name after export.

#### Post-Processing
- **Isolation Strategy** — Choose how to clean the cloud:
  - `raw_clean` — Remove outliers, keep everything else.
  - `largest_cluster` — Keep only the main object (DBSCAN).
  - `tabletop_object` — Plane removal + largest cluster (good for objects on tables).
  - `center_focus` — Bias toward the center, trim edges.
  - `aggressive_hybrid` — Combine all methods for tough scenes.
  - `all_variants` — Save all strategies for side-by-side comparison.

- **Apply Isolation** — Processes the exported cloud with the chosen strategy, creates `output/<name>_<strategy>.ply`.
- The same voxel setting is used when isolation is rerun, so the Settings tab directly affects cleanup quality.

**Recent Files** — Lists exported PLY and STL files in `output/` with file sizes.

---

### 4. Multi-Camera

**Control multiple ASUS Xtions (when physically mounted).**

#### Multi-Camera Live View
- Shows all cameras from `cameras.json` (e.g., camera1, camera2, camera3).
- Uses **calibrated extrinsics** from `output/camera_extrinsics.json`.
- Displays in RViz with all cameras' point clouds aligned in one 3D space.

#### Auto-Calibration
- Markerless extrinsic calibration using FPFH + RANSAC + ICP.
- **Requires overlapping views** of a structured scene with parallax (not coaxial stacking).
- Updates `output/camera_extrinsics.json`, which the multi-camera launch reads on next run.

**Detected Cameras** — Shows `cameras.json` roster (editable in a text editor to add new devices).

---

### 5. Settings

**Configure scanner parameters and maintenance.**

#### Scanner Parameters
- **Voxel Size (m)** — Downsampling grid. Smaller = more detail, slower (default 0.012 m = 1 cm).
- **Depth Quality Gate** — Slider for filtering poor-quality depth (0–100%).
- **Max Depth (mm)** — Ignore pixels beyond this distance (default 2500 mm).

#### Maintenance
- **Clear RTAB-Map Database** ⚠️ — Deletes `rtabmap.db`, losing all map data. Use to start fresh.
- **Rebuild Docker Image** ⚠️ — Re-downloads and builds `scanner-ros:jazzy` (useful if Dockerfile changed).

---

### 6. Log

**View command output and diagnostics.**

All Docker commands, export operations, and isolation runs log their output here. Scroll to see warnings or errors.

---

## Real-Time Status Bar

**Top of window** displays:
- **Status** — Current operation (Idle, Camera Preview, Mapping, etc.). Color-coded:
  - 🟢 **Green** = online/complete
  - 🟠 **Orange** = running/processing
  - 🔴 **Red** = error or stopped
- **Nodes** — Number of map nodes (keyframes) captured.
- **DB** — RTAB-Map database file size in MB (grows as you scan).

---

## Typical Workflow

### Single-Camera Scan
1. **Camera Preview** tab → **Launch Live Preview** (verify image quality).
2. **Mapping** tab → **Full SLAM GUI** (move camera slowly around subject).
3. Close the GUI window when done mapping.
4. **Export** tab → **Export Cloud** (saves `output/scan.ply`).
5. **Export** tab → **Post-Processing** → choose **Isolation Strategy** → **Apply Isolation**.
6. **View in 3D** to inspect the result.

### Multi-Camera Setup (After Housing Assembly)
1. Mount Xtions in the 3D-printed housing.
2. Edit `cameras.json` to list each camera's namespace and device URI.
3. **Multi-Camera** tab → **Run Auto-Calibration** (aim all cameras at a scene with structure).
4. **Multi-Camera** tab → **Launch Multi-Camera RViz** to verify alignment.
5. Use **Full SLAM GUI** for unified 3D scanning.

---

## Troubleshooting

### "Camera Preview shows 0 Hz"
- Check USB connection and camera power.
- Try a different USB port (Xtions need good USB 2+ bandwidth).
- Restart Docker: **Settings** → **Rebuild Docker Image** is overkill; try `docker restart <container>` from a terminal.

### "Export Failed: No cloud data"
- Ensure you ran **Mapping** before export (no map = no mesh).
- Check the RTAB-Map `rtabmap.db` exists and isn't corrupted.
- If corrupted, **Settings** → **Clear RTAB-Map Database** and rescan.

### "Post-Processing produces empty cloud"
- The isolation strategy may have been too aggressive for that scene.
- Try a less restrictive strategy (e.g., `raw_clean` instead of `tabletop_object`).
- If all fail, check the log tab for exact error.

### "Multi-Camera Calibration rejected (fitness < 0.5)"
- Cameras are too close together (near-zero baseline) or views don't overlap.
- Ensure *all* cameras see the *same* scene with visible parallax.
- Move housing apart slightly or aim at a richer scene (not a blank wall).

---

## Advanced: Command-Line Equivalents

The GUI wraps these Docker commands:

```bash
# Camera test (30 sec stream)
./run_scanner_docker.sh camera

# Single frame
./run_scanner_docker.sh snap output/single_frame.ply

# Quick map (no GUI)
./run_scanner_docker.sh map

# Full GUI (camera + RTAB-Map + rtabmap_viz)
./run_scanner_docker.sh gui

# Multi-camera view
./run_scanner_docker.sh multi

# Export
./run_scanner_docker.sh export rtabmap.db output/scan.ply

# Multi-camera calibration
./.venv/bin/python calibrate_multi.py

# Post-process isolation
./.venv/bin/python point_cloud_tools.py output/scan.ply --strategy tabletop_object
```

---

## Notes

- The GUI is **non-intrusive** — all underlying operations (RTAB-Map, Docker, export) work independently. You can mix GUI and CLI commands.
- **Telemetry updates every 2 seconds** from the RTAB-Map database; if the DB is being written, slight delays are expected.
- **Multi-camera mode requires overlapping views** — if cameras are coaxial (stacked vertically), the markerless calibrator will reject the solution (no parallax).
- **Isolation strategies are statistical** — they work best on single-object scenes; room-scale scans may need manual editing in Blender or meshlab.

