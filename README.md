# ROS 3D Scanner

An AI-assisted point cloud scanning application built on **RTAB-Map + OpenNI2** (PrimeSense / ASUS Xtion depth cameras). A Claude or GPT-4 agent watches the scan in real time and issues control commands to optimize mesh quality.

## Features

- **Live telemetry GUI** — depth quality, motion tracking, vertex count, and frame count, updated in real time.
- **AI scanning agent** — reads scanner state each tick, reasons about it with embedded scanner knowledge, streams guidance to the UI, and dispatches control commands.
- **Point cloud isolation** — five post-processing strategies plus a side-by-side `all_variants` mode (Open3D voxel downsampling, plane removal, outlier filtering, cluster extraction).
- **STL mesh export** — reconstructs a triangle mesh from the cleaned object cloud for printing or CAD workflows.
- **One-click export to STL** — exports the cloud, isolates the object, and writes STL from the GUI in one step.
- **Multiple scan modes** — handheld, turntable, and surface.

## Requirements

- ROS with RTAB-Map
- OpenNI2 and a compatible depth camera (PrimeSense / ASUS Xtion)
- Python 3 with [Open3D](https://www.open3d.org/)
- An API key in the environment: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`

## Quick Start

Bootstrap the local Python environment first:

```bash
./setup.sh
source ../venv/bin/activate
```

Launch the main GUI (auto-starts the ROS stack on the first scan):

```bash
./run_stack_control_app.sh
```

Or drive the ROS/RTAB-Map stack manually:

```bash
./run_prebuilt_stack.sh ros_all      # start camera node + RTAB-Map
./run_prebuilt_stack.sh ros_status   # check status
./run_prebuilt_stack.sh ros_stop     # stop the stack
```

Export and view a point cloud:

```bash
./run_prebuilt_stack.sh ros_export_cloud rtabmap.db output/scan.ply
python3 view_cloud.py output/scan.ply
```

Generate an STL from the cleaned object cloud:

```bash
./.venv/bin/python point_cloud_tools.py output/scan_cloud.ply --strategy tabletop_object --mesh-output output/scan.stl
```

Run an automated AI-guided scan (N seconds, then export):

```bash
./run_prebuilt_stack.sh ros_agent_scan 60 output/ai_scan.ply
```

## Components

| File | Purpose |
| --- | --- |
| `ros_scanner_app.py` | Main Tkinter GUI: telemetry, scan modes, isolation and preview controls. |
| `ai_scanner_agent.py` | Background agent thread: reads state, queries the LLM, parses and dispatches commands. |
| `point_cloud_tools.py` | Post-processing pipeline with five isolation strategies + `all_variants`. |
| `view_cloud.py` | Lightweight PLY viewer with downsampling and normal estimation. |
| `rgb_display_tools.py` | Optional live RGB/depth display and calibration helpers. |
| `run_prebuilt_stack.sh` | ROS/RTAB-Map launcher (camera node, full stack, RViz, export, watchers). |
| `run_stack_control_app.sh` | GUI launcher. |

## Agent Commands

The agent issues one command per line (verb first):

`CAPTURE`, `START_AUTO` / `STOP_AUTO`, `BUILD_MESH`, `EXPORT_LIVE`, `ISOLATE_LATEST`, `OPEN_LATEST`, `OPEN_BLENDER`, `SET_ISOLATION:x`, `SET_DEPTH_WINDOW:min:max`, `SET_AUTO_EXPORT:on|off`, `SET_MODE:<mode>`, `CLEAR_SESSION`, `SAY:<text>`, `WAIT`

## Isolation Strategies

1. **raw_clean** — light cleanup only; best when the raw cloud is already tight.
2. **largest_cluster** — keep the densest contiguous cluster.
3. **tabletop_object** — plane removal + main cluster.
4. **center_focus** — bias toward the center, trim edges.
5. **aggressive_hybrid** — plane removal + center crop + cluster filtering.
6. **all_variants** — save multiple strategies side by side.

## Documentation

See [`CLAUDE.md`](CLAUDE.md) for the full architecture breakdown, development workflow, feature roadmap, and troubleshooting guide.
