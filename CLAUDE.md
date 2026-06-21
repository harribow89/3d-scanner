# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ROS 3D Scanner** — An AI-assisted point cloud scanning application using RTAB-Map + OpenNI2 (PrimeSense/ASUS Xtion depth cameras). A Claude or GPT-4 agent watches the scan in real time and issues control commands to optimize mesh quality.

## Architecture

### Core Stack

- **scanner_gui.py** — Primary PySide6-based control panel (Docker-native, no host ROS required). Six operational tabs:
  - *Camera Preview* — test camera stream, capture single frames
  - *Mapping* — quick SLAM scan or full hands-off GUI (auto-mapping + rtabmap_viz)
  - *Export* — save PLY files, view in 3D, apply post-processing
  - *Multi-Camera* — live view and markerless auto-calibration (if multiple Xtions mounted)
  - *Settings* — adjust voxel size, depth quality, max depth; clear DB, rebuild Docker
  - *Log* — command output and diagnostics
  - Real-time telemetry bar (status, nodes, DB size) refreshes every 2 seconds from RTAB-Map DB.

- **ros_scanner_app.py** — Legacy Tkinter GUI (requires host ROS; deprecated on Kali). Kept for reference; users should prefer `scanner_gui.py`.

- **ai_scanner_agent.py** — Background thread that reads scanner state every tick, sends it to Claude/GPT-4 with embedded skill knowledge about how the scanner works, streams the response to the UI, and parses/dispatches commands (CAPTURE, START_AUTO, BUILD_MESH, EXPORT_LIVE, ISOLATE_LATEST, SET_ISOLATION, etc.) back through a ScannerBridge.

- **point_cloud_tools.py** — Post-processing pipeline offering five isolation strategies (raw_clean, largest_cluster, tabletop_object, center_focus, aggressive_hybrid) and an all_variants mode. Uses Open3D for voxel downsampling, plane removal, statistical/radius outlier filtering, and cluster extraction. Saves multiple variants for side-by-side comparison.

- **view_cloud.py** — Lightweight PLY viewer. Downsamples large point clouds to ~1–2M points for smooth rendering and estimates surface normals.

- **rgb_display_tools.py** (optional) — Enhanced visualization module with live RGB/depth stream display and camera calibration tools. Can be integrated into `ros_scanner_app.py` for real-time video feedback during scanning.

- **station_scan.py** + **capture_station.py** — **Matterport-style station/sweep scanning** (Stage 1 of `MATTERPORT_REPLICATION_PLAN.md`, derived from reverse-engineering the Matterport app). Instead of continuous handheld SLAM, capture discrete 360°-ish "sweeps" from fixed tripod positions: each `capture` stacks N depth frames per camera (temporal denoise), fuses the multi-Xtion clouds into one sweep via `output/camera_extrinsics.json`, then `build` globally registers all sweeps (FPFH+RANSAC→ICP, same as `calibrate_multi.py`) with an Open3D pose-graph optimization → `output/room.ply`. Run via `./run_scanner_docker.sh station capture|build|list`. `station_scan.py` runs host-side in `.venv` and spins up the camera container itself per sweep.

- **run_prebuilt_stack.sh** — Bash launcher for ROS/RTAB-Map modes (camera node, full stack, RViz, export utilities, auto-restart watchers).

- **run_stack_control_app.sh** — Simple GUI launcher.

### State & Config

- **ros_scanner_settings.json** — Persistent user settings (isolation profile, depth window, quality preset, scan mode).
- **calibration.json** — Camera calibration (if needed).
- **rtabmap.db** — RTAB-Map pose graph and point cloud data (created at runtime).

## Common Commands

### Start the Scanner GUI (PySide6 control panel — recommended)

```bash
./launch_gui.sh
```

Opens the modern control panel with six operational tabs, real-time telemetry, and easy access to all scanner modes (camera preview, mapping, export, multi-camera, settings, log). All commands run in Docker; no host ROS needed. See **SCANNER_GUI_GUIDE.md** for detailed feature walkthrough.

### Start ROS/RTAB-Map Stack Manually

```bash
./run_prebuilt_stack.sh ros_all
```

Launches OpenNI2 camera node + RTAB-Map in the background. Logs go to `ros_logs/`. Check status with:

```bash
./run_prebuilt_stack.sh ros_status
```

### Stop the Stack

```bash
./run_prebuilt_stack.sh ros_stop
```

### Export a Point Cloud

```bash
./run_prebuilt_stack.sh ros_export_cloud rtabmap.db output/scan.ply
```

Exports the RTAB-Map database to a .ply file. Omit arguments to use defaults (rtabmap.db → output/).

### View a Point Cloud

```bash
python3 view_cloud.py output/scan.ply
```

Opens an interactive viewer. Drag to rotate, scroll to zoom, Q to quit.

### Run an Automated Scan with AI Agent

```bash
./run_prebuilt_stack.sh ros_agent_scan 60 output/ai_scan.ply
```

Starts the ROS stack, runs the AI agent for N seconds, then exports a cloud. The agent will issue commands to guide the scan.

### Watch ROS/RTAB-Map with Auto-Restart

```bash
./run_prebuilt_stack.sh ros_watch 5
```

Keeps the mapping stack alive, restarting if processes die. Useful for long unattended scans.

## Development Workflow

### Adding AI Commands

The agent can issue these commands (one per line, verb first):

- **CAPTURE** — Take a single frame (surface mode)
- **START_AUTO / STOP_AUTO** — Toggle continuous auto-capture
- **BUILD_MESH** — Run full ICP + Poisson reconstruction
- **EXPORT_LIVE** — Save the current TSDF mesh
- **ISOLATE_LATEST** — Re-run isolation on the latest export
- **OPEN_LATEST** — Preview the latest saved cloud
- **OPEN_BLENDER** — Send last export to Blender
- **SET_ISOLATION:x** — Change isolation strategy (x = raw_clean, largest_cluster, etc.)
- **SET_DEPTH_WINDOW:min:max** — Adjust near/far depth gates (mm)
- **SET_AUTO_EXPORT:on|off** — Toggle auto-export on scan stop
- **SET_MODE:<mode>** — Switch scan mode (handheld, turntable, surface)
- **CLEAR_SESSION** — Discard frames and restart
- **SAY:<text>** — Post a status message
- **WAIT** — Do nothing this tick

To add a command, modify the response parser in `ai_scanner_agent.py` and update the skill prompt with decision rules.

### Tuning Point Cloud Isolation

Edit `ISOLATION_PROFILES` in `point_cloud_tools.py` to add or modify isolation strategies. Each profile is a function that takes a point cloud and returns a cleaned variant. Key tuning parameters:

- **voxel_size** — Downsampling grid size (meters). Smaller = more detail, slower.
- **statistical_outlier** — Remove noisy points far from neighbors.
- **radius_outlier** — Remove sparse clusters.
- **plane_removal** — Strip dominant planes (good for tabletop scenes).
- **cluster_size** — Keep only the largest cluster, or filter by size.

### Integration with Claude/GPT-4

The agent reads `SCANNER_SKILL` at startup — a prompt that describes scanner modes, telemetry interpretation (depth quality, motion tracking, vertex/frame counts), and decision rules (when to START_AUTO, when to BUILD_MESH, etc.). Update this skill to teach the agent new heuristics without changing code.

### LLM provider selection (incl. no-key mode)

`AIAgentController` (in `ai_scanner_agent.py`) picks a provider via `_resolve_provider`:

- **`claude_cli` (no API key needed)** — routes each tick through the local
  **Claude Code CLI** (`claude -p`), using your existing Claude Code auth instead
  of a raw key. This is the **automatic fallback** when neither `ANTHROPIC_API_KEY`
  nor `OPENAI_API_KEY` is set and a `claude` binary is found (searches `PATH`,
  then `~/.local/bin/claude`, `/usr/local/bin/claude`). Force it explicitly with
  `provider="claude_cli"`.
  - Model alias is `haiku` by default (cheap/fast for the ~4 s loop); override with
    the `SCANNER_CLAUDE_CLI_MODEL` env var (e.g. `sonnet`).
  - The skill prompt is passed via `--append-system-prompt`; the rolling
    conversation is piped on stdin. Calls are bounded by a 90 s timeout — on
    timeout or non-zero exit the tick degrades to `WAIT` and reports via `on_error`.
  - Unlike the SDK paths, the CLI returns the full answer at once (no token
    streaming), so the GUI thought panel updates once per tick rather than live.
- **`anthropic` / `openai`** — used when the corresponding key is present (or set
  explicitly). Unchanged from before.

## Feature Roadmap

From `agency_feature_plan.md`:

1. **Export Quality Lane** — Post-export cleanup, tabletop segmentation, center-focus cropping, aggressive hybrid cleanup.
2. **Live Guidance Lane** — Convert odometry and loop-closure telemetry into immediate scan advice inside the UI.
3. **AI Control Lane** — Let the agent change export/isolation settings and trigger follow-up actions (implemented).
4. **Operator Workflow Lane** — Keep latest raw and recommended isolated cloud easy to preview and reprocess (implemented).

Planned additions: mesh reconstruction, export-side mesh decimation presets, auto-scoring isolated variants, guided scan checklist with coverage progress.

## Troubleshooting

### ROS Stack Won't Start

Check that OpenNI2 is installed and the camera is connected. View raw depth with:

```bash
./run_prebuilt_stack.sh niviewer
```

If niviewer freezes or shows no frames, the camera is not detected.

### Agent Sends No Commands

No API key is required if the **Claude Code CLI** is installed — the agent falls
back to `claude -p` automatically (see "LLM provider selection" above). If you
*do* want the SDK path, set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. If the agent
is silent, check: the `claude` binary is on `PATH` (or `~/.local/bin/claude`) and
`claude -p "hi"` works; otherwise look at the GUI log window for parse/timeout errors.

### Point Cloud Is Noisy or Sparse

- Low depth quality (<0.3) suggests the sensor is warming up, the object is too far, or lighting is poor.
- Increase frames captured before running BUILD_MESH (aim for 20+ in handheld mode).
- Try a tighter isolation profile (center_focus, aggressive_hybrid).

### Segmentation Fault During Export

This typically means RTAB-Map or the pose graph is corrupted. Clear the database and restart:

```bash
rm rtabmap.db
./run_prebuilt_stack.sh ros_stop
./run_prebuilt_stack.sh ros_all
```
