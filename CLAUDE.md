# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A 3D scanner for PrimeSense / ASUS Xtion depth cameras. ROS 2 Jazzy + RTAB-Map +
OpenNI2 run inside the `scanner-ros:jazzy` Docker image (built from `Dockerfile`);
the host runs a PySide6 control panel and Open3D post-processing from `.venv`.
No ROS on the host. The project folder path contains a space — every command
must be an argv list or a quoted path.

## Architecture

- **run_scanner_docker.sh** — the single launcher. Every mode (`doctor`, `camera`,
  `preview`, `snap`, `map`, `gui`, `viz`, `export`, `multi`, `multi_slam`,
  `overlap`, `calibrate`, `station`, `stop`, `logs`, `shell`, `build`). Containers
  are named `scanner-<mode>-<pid>` and labelled `scanner-ros=1`; an EXIT trap
  `docker stop`s them on SIGINT/SIGTERM. Camera modes call `require_camera N`
  (sysfs vendor `1d27`) and exit 3 fast when nothing is plugged in. ROS logs go to
  `ros_logs/<mode>.log`; the container's `$HOME` is `.container_home/`. The
  in-container scripts share `PRELUDE` (`start_camera`, `wait_topic`, `run_main`,
  `graceful`) so `docker stop` gives ROS a clean SIGINT and RTAB-Map saves.
  `SCANNER_FAKE_CAMERA=1` swaps the openni2 driver for `tests/fake_camera.py`.
- **scanner_core.py** — GUI-free core imported by the GUI and the tools: paths,
  `detect_xtions()` (sysfs), Docker helpers, `run_doctor()`, `read_telemetry()`
  (read-only `immutable=1` SQLite so RTAB-Map is never blocked),
  `read_live_status()`, and `cmd_*()` builders returning `Command(argv, title,
  long_running, needs_camera, env)`. Unit tests: `tests/test_core.py`.
- **scanner_gui.py** — PySide6 window. `Runner` wraps ONE `QProcess` (merged
  channels, streamed lines, no timeout; `stop()` = SIGTERM, kill after 25 s).
  `TelemetryThread` polls the DB + USB every 1.5 s; a 150 ms timer refreshes the
  preview panes from `.live/`. Tabs: Scan, Export & Clean, Multi-Camera, Setup
  (doctor table, simulated-camera switch), Log. `--smoke [--screenshot DIR]` builds
  it offscreen for tests.
- **live_preview_node.py** (container) — subscribes to `/<ns>/rgb/image_raw`,
  `/<ns>/depth_registered/image_raw` (or `/<ns>/depth/image`), matching the
  publisher's reliability (best-effort drops large frames), writes
  `<ns>_rgb.jpg`, `<ns>_depth.jpg`, `status.json` atomically. `--summary` prints a
  RESULT line from a status file (used by `camera`).
- **tests/fake_camera.py** (container) — synthetic RGB-D publisher + static TF,
  QVGA by default (VGA frames exceed what FastDDS moves without fragmentation
  loss; RTAB-Map then reports "Did not receive data").
- **capture_cloud_ros.py** (container) — one depth frame → PLY, deprojecting
  with the pinhole model itself (does not depend on depth_image_proc).
- **point_cloud_tools.py** — `ISOLATION_PROFILES` and the CLI (`--strategy`,
  `--voxel`, `--mesh-output`). `scanner_core.ISOLATION_STRATEGIES` must match its
  keys (a test checks).
- **Multi-camera**: `multi_camera.launch.py` + `cameras.json` (roster pinned by USB
  port path, any `1d27:*` product), `calibrate_multi.py` / `overlap_check.py` /
  `fuse_check.py` (FPFH+RANSAC→ICP, chained pairs), `station_scan.py` +
  `capture_station.py` (sweeps + pose-graph fusion). See `CALIBRATION_GUIDE.md`
  and `MATTERPORT_REPLICATION_PLAN.md`.
- **legacy/** — `run_prebuilt_stack.sh` (host-ROS launcher, hard-coded old paths)
  and `ai_scanner_agent.py` (LLM agent for the deleted Tkinter app; not wired to
  anything). Keep out of the main app unless re-integrated deliberately.

## Commands

```
./setup.sh                                   # venv + image
./run_scanner_docker.sh doctor               # checklist
./launch_gui.sh                              # GUI
./launch_gui.sh --smoke --screenshot /tmp/s  # offscreen GUI test
./.venv/bin/python -m unittest discover -s tests -v
tests/test_preview_docker.sh                 # fake camera → preview node (Docker)
SCANNER_FAKE_CAMERA=1 ./run_scanner_docker.sh camera 8     # launcher end to end
SCANNER_FAKE_CAMERA=1 timeout -s TERM 80 ./run_scanner_docker.sh map   # simulated SLAM
./run_scanner_docker.sh export rtabmap.db output/scan.ply 4.0 0.012
```

Two simulated cameras must not run at once (`--network host`, one DDS domain).

## Conventions

- Add a GUI action by adding a `cmd_*` builder in `scanner_core.py` (argv list,
  `needs_camera`, `long_running`) and a launcher mode; the GUI only calls `_run()`.
- Container scripts: start everything in the background, `wait`, and let
  `graceful` handle SIGTERM. Never `sleep N` for a camera — use `wait_topic`.
- Paths handed to the container go through `to_container_path` (host
  `$HERE/...` ↔ `/scanner/...`).
- Keep `RTAB_TUNE` (Xtion-specific RTAB-Map params) in the launcher; override per
  run with the env var rather than editing.
- Isolation tuning: edit `ISOLATION_PROFILES` / the `_profile` functions in
  `point_cloud_tools.py` (voxel, statistical/radius outlier, plane removal,
  DBSCAN cluster size).

## Verified state (2026-10-14, no Xtion plugged in)

With the simulated camera, end to end: unit tests (17), `tests/test_preview_docker.sh`
(10/10 fps), `camera 8`, `snap` (301k points), `map` for 80 s (142 nodes, 54 loop
closures, clean SIGTERM stop, 0 containers left), `export` (95k points),
`point_cloud_tools.py --mesh-output` (218k-triangle STL), GUI `--smoke`, and the
GUI on the real display via `--script` (preview → stop → scan → stop). The
real-Xtion ROS command lines (`openni2_camera camera_with_cloud.launch.py`,
`rtabmap.launch.py` arguments, `RTAB_TUNE`) are the ones that streamed in
2026-06, kept verbatim apart from moving `Odom/*` into `odom_args`; they were
not re-tested with hardware in this revamp. First check with a camera:
`./run_scanner_docker.sh camera 20`, then a short `map`.

## Unified workflow (2026-10-26)

One window, one path: Scan tab → pick **Room / space** or **Object** (sets the
RTAB-Map tuning via `scanner_core.SCAN_MODES` → `RTAB_TUNE`/`ODOM_TUNE` env, and
the export preset) → live preview → Start scan (F6) → **Finish (F9)** = stop →
export → isolate (`raw_clean` for rooms, `tabletop_object` for objects) → STL →
viewer. Multi-camera / station tools live in the "Multi-Camera (advanced)" tab;
`cameras.json` still pins ports 1-3/1-5/1-8 from the old 3-camera rig.
The simulated-camera switch is NOT persisted (a saved "on" once hid the real Xtion).

## Verified on the real Xtion (2026-10-26, one Xtion PRO LIVE on USB port 1-7)

`camera` 30 fps RGB + depth, 640x480, registered depth arrives as **32FC1 with
NaN holes**; `snap` 123k points; `map` 82 nodes in 45 s, Stop clean in 8 s;
GUI-driven Room scan reached 377 nodes / 46 loop closures; Finish → 49 MB cloud,
cleaned cloud, STL; Object export + tabletop isolation + STL. Real handheld scan
quality (moving the camera around a room/object) is up to the operator.

Two hardware-only bugs found that day:
1. **The Xtion's USB microphones knock it off the bus.** snd-usb-audio + PipeWire
   probe them, the firmware errors (-71) and the device disappears until
   re-plugged. The launcher's `detach_xtion_audio` de-authorizes the class-01
   interfaces before every camera mode; `udev/56-xtion-no-audio.rules` makes it
   permanent (user installs it: `sudo cp udev/56-xtion-no-audio.rules
   /etc/udev/rules.d/ && sudo udevadm control --reload-rules`).
2. **Stop never reached ROS.** Background jobs of a non-interactive bash start
   with SIGINT ignored (SigIgn bit 2 on `ros2 launch`), so every `kill -INT`
   was a no-op and docker SIGKILLed after 12 s. `graceful` now sends SIGTERM.
   (The fake camera hid this.) The map loop uses `sleep & wait` so the trap fires
   at once.

## Troubleshooting

- No camera: `lsusb | grep 1d27`; the launcher's exit code 3 means none found.
- Stuck camera: `./run_scanner_docker.sh stop`.
- Empty export (exit 4): `rtabmap.db` has 0 nodes — the scan never tracked.
- Window modes need `DISPLAY` and `xhost`; the launcher prefers the NVIDIA GPU,
  then `/dev/dri`, then software GL.
- DB corruption / segfault on export: delete `rtabmap.db` (Setup → *Clear the map
  database*) and rescan.
