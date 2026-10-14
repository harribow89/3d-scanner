# 3D Scanner — ASUS Xtion + RTAB-Map, in Docker

Point-cloud scanning with PrimeSense / ASUS Xtion depth cameras. ROS 2 Jazzy,
RTAB-Map and OpenNI2 run inside one Docker image; the host only needs Docker,
Python and an X11 display. A PySide6 control panel drives everything and shows a
live camera preview, the growing map, and the output of every command.

```
./setup.sh                      # once: Python venv + Docker image
./run_scanner_docker.sh doctor  # is docker / the image / the camera / the venv OK?
./launch_gui.sh                 # the control panel
```

## Requirements

| Need | Notes |
| --- | --- |
| Docker | the user must be in the `docker` group, or `sudo -n docker` must work (passwordless sudo) |
| ASUS Xtion / PrimeSense camera | USB vendor id `1d27` — `lsusb` must list it. Up to 3 for the multi-camera modes |
| Python 3.10 – 3.12 | for the venv (Open3D wheels stop at 3.12) |
| X11 display | for the RTAB-Map / RViz windows and the Open3D viewer. On Wayland the launcher forces XWayland |
| NVIDIA container toolkit *(optional)* | the 3D windows use the GPU; otherwise `/dev/dri` or software GL |

Tested host: Ubuntu 24.04. The Dockerfile is plain `ros:jazzy-ros-base` + apt
packages, so any Linux with Docker works.

## Workflow (GUI)

1. **Setup tab** — the checklist must be green for Docker, the image, the camera
   and the venv. Tick **simulated camera** to try everything without hardware.
2. **Scan tab → Start live preview** — RGB and depth appear in the panes with the
   frame rate and depth fill. Stop when happy.
3. **Scan tab → Start scan** — headless RTAB-Map SLAM with the live preview.
   *Open the RTAB-Map window* does the same with rtabmap_viz showing the 3D map
   (close that window to finish). Keyframes / loop closures / database size
   update live from `rtabmap.db`. Press **Stop** to finish; the map is saved
   continuously.
4. **Export & Clean tab → Export cloud** — writes `output/<name>_cloud.ply`.
   Pick a preset (room, tabletop object, small object) or set max range and voxel.
5. **Apply isolation / Make STL** — Open3D cleanup (`raw_clean`,
   `largest_cluster`, `tabletop_object`, `center_focus`, `aggressive_hybrid`,
   `all_variants`) and Poisson/ball-pivot meshing. **Export + clean + STL** chains
   all three. Double-click any file to open it in the 3D viewer.

The **Stop** button sends SIGTERM to the launcher, whose trap `docker stop`s the
container by name, so the USB cameras are always released. If something still
seems stuck: Setup tab → *Stop all scanner containers*.

## Command line

Everything the GUI does goes through `run_scanner_docker.sh`, so it can be
scripted or debugged from a terminal:

```
./run_scanner_docker.sh doctor               # checklist (exit 1 if a required check fails)
./run_scanner_docker.sh camera [seconds]     # start the driver, print the frame rate, stop
./run_scanner_docker.sh preview              # live preview into .live/ until Ctrl-C
./run_scanner_docker.sh snap [out.ply]       # one point-cloud frame
./run_scanner_docker.sh map                  # headless SLAM until Ctrl-C   (MAP_FRESH=0 continues the map)
./run_scanner_docker.sh gui                  # SLAM + rtabmap_viz window
./run_scanner_docker.sh viz                  # camera + RTAB-Map + RViz
./run_scanner_docker.sh export [db] [out.ply] [max_range_m] [voxel_m]
./run_scanner_docker.sh stop | logs | shell | build
# multi-camera rigs (see CALIBRATION_GUIDE.md)
./run_scanner_docker.sh overlap | calibrate | multi | multi_slam
./run_scanner_docker.sh station capture|build|list|clear      # MATTERPORT_REPLICATION_PLAN.md
# host-side tools
./.venv/bin/python point_cloud_tools.py output/scan_cloud.ply --strategy tabletop_object --mesh-output output/scan.stl
./.venv/bin/python view_cloud.py output/scan_cloud.ply
```

`SCANNER_FAKE_CAMERA=1` in front of any camera mode uses `tests/fake_camera.py`,
a synthetic RGB-D scene (textured wall + box, slowly panning camera), so the
whole chain — preview, snap, SLAM, export, cleanup, STL — runs with no hardware.

## Layout

| File | Role |
| --- | --- |
| `run_scanner_docker.sh` | the launcher: every mode, container naming/cleanup, camera check, host-side logs |
| `scanner_gui.py` | PySide6 control panel (5 tabs) |
| `scanner_core.py` | GUI-free core: paths, Xtion detection, Docker helpers, doctor, telemetry, command builders |
| `live_preview_node.py` | runs in the container; writes `.live/*.jpg` + `status.json` for the preview |
| `capture_cloud_ros.py` | runs in the container; one depth frame → PLY (`snap`) |
| `point_cloud_tools.py`, `view_cloud.py` | Open3D cleanup / meshing, and the viewer (host venv) |
| `multi_camera.launch.py`, `cameras.json` | multi-Xtion bring-up, roster pinned by USB port |
| `calibrate_multi.py`, `overlap_check.py`, `fuse_check.py` | markerless multi-camera calibration |
| `station_scan.py`, `capture_station.py` | Matterport-style sweep capture and pose-graph fusion |
| `tests/` | `test_core.py` (unit), `test_preview_docker.sh` (container integration), `fake_camera.py` |
| `legacy/` | the old host-ROS launcher and the unwired AI agent; not used by the app |
| `Dockerfile`, `setup.sh`, `launch_gui.sh` | image, host setup, GUI launcher |

Runtime folders (git-ignored): `output/` (clouds, meshes, calibration),
`ros_logs/` (every ROS launch log), `.live/` (preview frames),
`.container_home/` (the container's `$HOME`, holds rtabmap_viz settings),
`rtabmap.db` (the map).

## Tests

```
./.venv/bin/python -m unittest discover -s tests -v   # no Docker needed
tests/test_preview_docker.sh                           # fake camera → preview node, in Docker
./launch_gui.sh --smoke --screenshot /tmp/shots        # GUI builds offscreen, one PNG per tab
SCANNER_FAKE_CAMERA=1 ./run_scanner_docker.sh camera 8 # the launcher end to end
```

## Troubleshooting

- **"No Xtion detected"** — `lsusb | grep 1d27`. Use a USB 2/3 port on the
  machine itself (not a hub shared with other heavy devices). The header of the
  GUI updates within two seconds of plugging in.
- **A button does nothing / fails** — the Log tab shows the exact command and its
  output; `ros_logs/<mode>.log` has the ROS side. `./run_scanner_docker.sh logs`
  prints the latest one.
- **Camera busy / stuck** — a previous container still holds it:
  `./run_scanner_docker.sh stop` (or Setup → *Stop all scanner containers*).
- **Export says the map is empty** — the scan produced no keyframes. Check the
  preview shows depth, move slower, and keep a textured scene 0.5–3 m away.
- **The RTAB-Map / RViz window does not open** — `DISPLAY` must be set and
  `xhost` present (the launcher runs `xhost +local:root`). Laggy = no GPU in the
  container; install the NVIDIA container toolkit.
- **Files owned by root** — the launcher chowns `output/`, `ros_logs/`, `.live/`
  and `rtabmap.db` back to you after every run (needs passwordless sudo).
- **Open3D viewer fails on Wayland** — `view_cloud.py` forces X11; run from the
  GUI or with `QT_QPA_PLATFORM=xcb`.
- **Multi-camera map "all over the place"** — calibration. Read
  `CALIBRATION_GUIDE.md`: check overlap first, calibrate only when every pair is
  GOOD.

## Why the 2026-10 revamp

The previous GUI never worked from this folder: the project path contains a
space (`3d scanning`) and every button ran `cd /home/jim/Desktop/3d scanning/...`
unquoted, so each command died at `cd`. On top of that, commands were killed
after 10 minutes, their output appeared only when they finished, the status
line was overwritten by the database poll, the export wrote inside the
container, and killed scripts left containers holding the camera. All commands
are now argv lists, output streams live, nothing times out, Stop frees the
cameras, and the launcher refuses camera modes up front when no Xtion is
plugged in. A simulated camera lets the whole pipeline be tested without
hardware.
