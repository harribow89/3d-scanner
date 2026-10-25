#!/usr/bin/env bash
# run_scanner_docker.sh — the ONE entry point for every scanner mode.
#
# ROS 2 Jazzy + RTAB-Map + OpenNI2 live inside the `scanner-ros:jazzy` image
# (built from ./Dockerfile); the host only needs Docker, USB and X11. The GUI
# (scanner_gui.py) calls this script for everything, so the CLI and the GUI
# always behave the same.
#
#   ./run_scanner_docker.sh doctor               # check docker / image / camera / venv
#   ./run_scanner_docker.sh build                # build (or rebuild) the image
#   ./run_scanner_docker.sh camera [seconds]     # 20 s camera test: prints the frame rate
#   ./run_scanner_docker.sh preview              # live RGB + depth preview (for the GUI), until Stop
#   ./run_scanner_docker.sh snap [out.ply]       # capture ONE point cloud frame
#   ./run_scanner_docker.sh map                  # headless RTAB-Map SLAM, until Stop (MAP_FRESH=0 to continue a map)
#   ./run_scanner_docker.sh gui                  # SLAM + the rtabmap_viz 3D window; close it to finish
#   ./run_scanner_docker.sh viz                  # camera + RTAB-Map + RViz live cloud
#   ./run_scanner_docker.sh export [db] [out.ply] [max_range_m] [voxel_m] [decimation]   # map -> output/<name>_cloud.ply
#   ./run_scanner_docker.sh multi                # all Xtions in cameras.json + RViz (calibrated)
#   ./run_scanner_docker.sh multi_slam           # EXPERIMENTAL 3-camera SLAM
#   ./run_scanner_docker.sh overlap              # multi-cam: check adjacent-camera overlap
#   ./run_scanner_docker.sh calibrate [--offline [dir]]   # multi-cam: solve extrinsics
#   ./run_scanner_docker.sh station capture|build|list|clear   # Matterport-style sweeps
#   ./run_scanner_docker.sh stop                 # stop every scanner container (frees the cameras)
#   ./run_scanner_docker.sh logs                 # show the latest ROS logs (ros_logs/)
#   ./run_scanner_docker.sh shell                # interactive ROS shell in the container
#
# Environment knobs:
#   SCANNER_FAKE_CAMERA=1   use the simulated RGB-D camera (tests/fake_camera.py) — no hardware
#   MAP_FRESH=0             map/gui: keep extending rtabmap.db instead of starting a new map
#   RTAB_TUNE="..."         override the RTAB-Map parameters below
#
# Design notes (the things that bit us before):
#   * Every container is named + labelled (scanner-ros=1) and this script traps
#     SIGINT/SIGTERM to `docker stop` it, so the GUI's Stop button — or Ctrl-C —
#     really frees the USB cameras instead of leaving a zombie container.
#   * Camera modes refuse to start (in < 1 s, with a clear message) when no
#     PrimeSense/Xtion is on the USB bus, instead of failing 20 s later in ROS.
#   * ROS logs go to ros_logs/ on the host (a --rm container's /tmp is lost).
#   * $HOME inside the container is .container_home/ so root-owned dotfiles
#     stop appearing in the project root; outputs are chown'ed back to you.
#   * Paths are always quoted: this project lives in a folder with a space.
set -euo pipefail

IMAGE=scanner-ros:jazzy
LABEL=scanner-ros
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

CHOME="$HERE/.container_home"; LIVE="$HERE/.live"; LOGS="$HERE/ros_logs"; OUT="$HERE/output"
mkdir -p "$CHOME" "$LIVE" "$LOGS" "$OUT"
# one-time migration of the rtabmap_viz settings the old layout left in ./.ros
if [ -f "$HERE/.ros/rtabmapGUI.ini" ] && [ ! -f "$CHOME/.ros/rtabmapGUI.ini" ]; then
  mkdir -p "$CHOME/.ros" && cp "$HERE/.ros/rtabmapGUI.ini" "$CHOME/.ros/" 2>/dev/null || true
fi

# docker: directly when this user may, else through non-interactive sudo
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo -n docker); fi

FAKE="${SCANNER_FAKE_CAMERA:-0}"
MAP_FRESH="${MAP_FRESH:-1}"

# RTAB-Map tuning for the ASUS Xtion (handheld). The Xtion has a narrow FOV and
# noisy depth, so defaults drift/lose tracking:
#   Vis/MaxDepth 3.5        - ignore features past ~3.5 m (Xtion depth is junk beyond that)
#   Vis/MinInliers 15       - keep tracking on lower-texture scenes (default 20)
#   Odom/ResetCountdown 5   - auto-recover after losing odometry (default 0 = never)
#   RGBD/Linear|AngularUpdate 0.05 - add keyframes on smaller motion (denser map)
#   Rtabmap/DetectionRate 2 - update the map at 2 Hz instead of 1
#   RGBD/NeighborLinkRefining true - refine consecutive links to reduce drift
#
# IMPORTANT: rtabmap.launch.py runs TWO nodes. `rtabmap_args` go to the SLAM node,
# `odom_args` to rgbd_odometry. The SLAM node does not declare Odom/* parameters —
# passing --Odom/ResetCountdown to it throws ParameterNotDeclaredException and the
# SLAM node ABORTS at startup (exit -6) while odometry keeps running. That was why
# every map/gui run since 2026-06 produced an empty rtabmap.db. Keep Odom/* here:
ODOM_TUNE="${ODOM_TUNE:---Vis/MaxDepth 3.5 --Vis/MinInliers 15 --Odom/ResetCountdown 5}"
RTAB_TUNE="${RTAB_TUNE:---Vis/MaxDepth 3.5 --Vis/MinInliers 15 --RGBD/LinearUpdate 0.05 --RGBD/AngularUpdate 0.05 --Rtabmap/DetectionRate 2 --RGBD/NeighborLinkRefining true --Mem/SaveDepth16Format true}"
# The GUI's Room / Object modes pass their own RTAB_TUNE / ODOM_TUNE
# (scanner_core.SCAN_MODES); these defaults are the Room values.

# Flags shared by every container. --init gives us a real PID 1 that forwards
# SIGTERM to our bash script so it can shut ROS down cleanly (RTAB-Map saves).
COMMON=(--rm --init --privileged --network host --label "$LABEL=1"
        -v /dev/bus/usb:/dev/bus/usb
        -v "$HERE":/scanner -w /scanner
        -e HOME=/scanner/.container_home
        -e DISPLAY="${DISPLAY:-}" -v /tmp/.X11-unix:/tmp/.X11-unix
        -e SCANNER_FAKE_CAMERA="$FAKE" -e MAP_FRESH="$MAP_FRESH"
        -e RTAB_TUNE="$RTAB_TUNE" -e ODOM_TUNE="$ODOM_TUNE"
        -e SCANNER_HEAD="${SCANNER_HEAD:-}" -e HEAD_FPS="${HEAD_FPS:-15}")

# ----------------------------------------------------------------- host helpers
fix_owner() {
  # the container runs as root; hand its files back to the host user
  local me; me="$(id -u):$(id -g)"
  sudo -n chown -R "$me" "$OUT" "$CHOME" "$LIVE" "$LOGS" 2>/dev/null || true
  [ -e "$HERE/rtabmap.db" ] && sudo -n chown "$me" "$HERE/rtabmap.db" 2>/dev/null || true
  return 0
}

NAME=""
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [ -n "$NAME" ]; then
    echo ">> stopping container $NAME …"
    "${DOCKER[@]}" stop -t 12 "$NAME" >/dev/null 2>&1 || true
  fi
  fix_owner
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_container() {
  # run_container <mode> [extra docker flags...] -- <bash script run inside>
  local mode=$1; shift
  local flags=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do flags+=("$1"); shift; done
  shift || true
  NAME="scanner-${mode}-$$"
  "${DOCKER[@]}" run --name "$NAME" "${COMMON[@]}" -e MODE="$mode" "${flags[@]}" \
      "$IMAGE" bash -c "$1" &
  local pid=$! rc=0
  wait "$pid" || rc=$?
  NAME=""
  return "$rc"
}

gl_flags() {
  # Prefer the NVIDIA GPU (needs nvidia-container-toolkit); else a DRI render
  # node (Intel/AMD). Without either, GL is software (llvmpipe) and the 3D
  # windows are laggy but still work.
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    echo --gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all
  elif [ -e /dev/dri ]; then
    echo --device /dev/dri:/dev/dri
  fi
}

allow_x11() { command -v xhost >/dev/null 2>&1 && xhost +local:root >/dev/null 2>&1 || true; }

xtion_count() {
  local n=0 f
  for f in /sys/bus/usb/devices/*/idVendor; do
    [ "$(cat "$f" 2>/dev/null)" = "1d27" ] && n=$((n + 1))
  done
  echo "$n"
}

detach_xtion_audio() {
  # The Xtion PRO LIVE has two USB microphones (interface class 01). When the
  # host's snd-usb-audio + PipeWire probe them (set sample rate on ep 0x84) the
  # sensor firmware hits a USB protocol error, drops off the bus and does not
  # come back until it is re-plugged (seen 2026-10-26, kernel 7.0). Detaching
  # the audio interfaces before streaming avoids that. Runtime only, best
  # effort; udev/56-xtion-no-audio.rules makes it permanent if installed.
  local i dev
  for i in /sys/bus/usb/devices/*:*; do
    [ -f "$i/bInterfaceClass" ] || continue
    [ "$(cat "$i/bInterfaceClass" 2>/dev/null)" = "01" ] || continue
    dev="/sys/bus/usb/devices/$(basename "${i%%:*}")"
    [ "$(cat "$dev/idVendor" 2>/dev/null)" = "1d27" ] || continue
    [ "$(cat "$i/authorized" 2>/dev/null)" = "1" ] || continue
    if echo 0 | sudo -n tee "$i/authorized" >/dev/null 2>&1; then
      echo ">> detached the USB-audio driver from the Xtion microphone ($(basename "$i"))"
    else
      echo "WARN: could not detach the Xtion microphone driver ($(basename "$i")). If the camera drops off USB, install udev/56-xtion-no-audio.rules" >&2
    fi
  done
  return 0
}

require_camera() {   # require_camera <min cameras>
  local need=${1:-1} n
  if [ "$FAKE" = "1" ]; then
    echo ">> SIMULATED camera (SCANNER_FAKE_CAMERA=1) — no hardware used"
    return 0
  fi
  if [ -n "${SCANNER_HEAD:-}" ]; then
    # cameras live on a network sensor head (the i.MX6 board or `head` mode)
    local h=${SCANNER_HEAD%%:*} p=${SCANNER_HEAD##*:}
    [ "$p" = "$SCANNER_HEAD" ] && p=5599
    if ! timeout 3 bash -c "exec 3<>/dev/tcp/$h/$p" 2>/dev/null; then
      echo "ERROR: sensor head $h:$p is not answering. Is the board on and on the network" >&2
      echo "       (or is './run_scanner_docker.sh head' running on this PC)?" >&2
      exit 3
    fi
    echo ">> using the sensor head at $h:$p"
    return 0
  fi
  detach_xtion_audio
  n=$(xtion_count)
  if [ "$n" -lt "$need" ]; then
    echo "ERROR: this mode needs $need depth camera(s); $n PrimeSense/Xtion device(s) found on USB." >&2
    echo "       Plug the Xtion in (\`lsusb\` should list 'ID 1d27:...') and retry," >&2
    echo "       or run with SCANNER_FAKE_CAMERA=1 to use the simulated camera." >&2
    exit 3
  fi
  echo ">> $n depth camera(s) detected"
}

require_image() {
  if ! "${DOCKER[@]}" image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: Docker image $IMAGE is not built. Run: $0 build" >&2
    exit 5
  fi
}

to_container_path() {
  # host path (absolute under $HERE, or relative) -> /scanner/... inside the container
  local p=$1
  case "$p" in
    "$HERE"/*) echo "/scanner/${p#"$HERE"/}" ;;
    /scanner/*) echo "$p" ;;
    /*) echo "ERROR: $p is outside the project folder; use a path under $HERE" >&2; exit 2 ;;
    *) echo "/scanner/$p" ;;
  esac
}

db_nodes() {   # db_nodes <host db path>  -> number of map nodes (0 if missing/unreadable)
  python3 - "$1" <<'PY'
import sqlite3, sys
p = sys.argv[1]
try:
    c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    print(c.execute("select count(*) from Node").fetchone()[0])
except Exception:
    print(0)
PY
}

# ------------------------------------------------------- in-container prelude
# Shared by every container script. Everything long-running is started in the
# background and `wait`ed on, so a SIGTERM from `docker stop` reaches the trap
# and ROS gets a clean SIGINT (RTAB-Map flushes its database).
PRELUDE='
source /opt/ros/jazzy/setup.bash
mkdir -p /scanner/ros_logs /scanner/.live
LOG="/scanner/ros_logs/${MODE}.log"
CAM_PID=""; MAIN_PID=""; EXTRA_PIDS=""
graceful() {
  trap - TERM INT
  echo ">> shutting down cleanly …"
  # SIGTERM, not SIGINT: bash starts the background jobs of a non-interactive
  # shell with SIGINT IGNORED (verified 2026-10-26: SigIgn bit 2 set on
  # ros2 launch). kill -INT did nothing, every Stop ended in docker SIGKILL and
  # RTAB-Map never closed its database. ros2 launch turns SIGTERM into a clean
  # shutdown (it SIGINTs its own nodes); rclpy nodes handle SIGTERM too.
  for p in $MAIN_PID $EXTRA_PIDS $CAM_PID; do kill -TERM "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  exit 0
}
trap graceful TERM INT
start_camera() {
  if [ "${SCANNER_FAKE_CAMERA:-0}" = "1" ]; then
    echo ">> starting SIMULATED camera (tests/fake_camera.py)"
    python3 /scanner/tests/fake_camera.py > "$LOG" 2>&1 &
  else
    echo ">> starting Xtion driver (openni2_camera)"
    ros2 launch openni2_camera camera_with_cloud.launch.py > "$LOG" 2>&1 &
  fi
  CAM_PID=$!
}
wait_topic() {   # wait_topic <topic> <seconds>
  local t=$1 n=${2:-40} i=0
  while [ $i -lt $n ]; do
    if ros2 topic list 2>/dev/null | grep -qx "$t"; then echo ">> $t is up"; return 0; fi
    if [ -n "$CAM_PID" ] && ! kill -0 "$CAM_PID" 2>/dev/null; then
      echo "ERROR: the camera process exited. Last log lines ($LOG):" >&2
      tail -n 25 "$LOG" >&2; return 1
    fi
    sleep 1; i=$((i + 1))
  done
  echo "ERROR: $t did not appear within $n s. Last log lines ($LOG):" >&2
  tail -n 25 "$LOG" >&2
  return 1
}
run_main() { "$@" & MAIN_PID=$!; wait "$MAIN_PID" || true; MAIN_PID=""; }
db_nodes() { python3 -c "import sqlite3;print(sqlite3.connect(\"file:/scanner/rtabmap.db?mode=ro&immutable=1\",uri=True).execute(\"select count(*) from Node\").fetchone()[0])" 2>/dev/null || echo 0; }
'

# ============================================================================
cmd="${1:-help}"
case "$cmd" in
  help|-h|--help)
    sed -n '2,45p' "$0"
    ;;

  doctor)
    shift || true
    python3 "$HERE/scanner_core.py" doctor "$@"
    ;;

  build)
    "${DOCKER[@]}" build -t "$IMAGE" -f "$HERE/Dockerfile" "$HERE"
    ;;

  head)
    # Run the network sensor head on THIS PC (all Xtions plugged in here),
    # then use any multi-camera mode with SCANNER_HEAD=127.0.0.1. The i.MX6
    # image runs the same program as a service. Extra args go to xtion_head.py.
    shift || true
    detach_xtion_audio
    PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    "$PY" -c "import primesense" 2>/dev/null || {
      echo "ERROR: the 'primesense' package is missing: $PY -m pip install primesense" >&2; exit 5; }
    exec "$PY" -u "$HERE/sensor_head/xtion_head.py" --config "$HERE/cameras.json" "$@"
    ;;

  stop)
    ids=$("${DOCKER[@]}" ps -q --filter "label=$LABEL=1"; "${DOCKER[@]}" ps -q --filter "ancestor=$IMAGE")
    ids=$(echo "$ids" | sort -u | tr '\n' ' ')
    if [ -z "${ids// /}" ]; then echo "no scanner containers running"; else
      # shellcheck disable=SC2086
      "${DOCKER[@]}" stop -t 12 $ids >/dev/null && echo "stopped: $ids"
    fi
    ;;

  logs)
    ls -t "$LOGS" 2>/dev/null | head -8 | sed 's/^/  /'
    latest=$(ls -t "$LOGS"/*.log 2>/dev/null | head -1 || true)
    [ -n "$latest" ] && { echo "--- $latest ---"; tail -n 40 "$latest"; }
    ;;

  shell)
    require_image
    "${DOCKER[@]}" run -it "${COMMON[@]}" --name "scanner-shell-$$" "$IMAGE" bash
    ;;

  camera)
    # Bounded camera test: start the driver, measure the frame rate for N seconds, exit.
    require_image; require_camera 1
    secs="${2:-20}"
    run_container camera -e SECS="$secs" -- "$PRELUDE"'
      start_camera
      wait_topic /camera/rgb/image_raw 40 || exit 2
      echo "=== camera topics ==="; ros2 topic list | grep -i /camera/ || true
      echo ">> measuring for ${SECS}s …"
      run_main python3 /scanner/live_preview_node.py --ns camera --out /scanner/.live --hz 4 --seconds "$SECS"
      python3 /scanner/live_preview_node.py --summary /scanner/.live/status.json
      rc=$?
      kill -TERM "$CAM_PID" 2>/dev/null; wait 2>/dev/null
      exit $rc'
    ;;

  preview)
    # Live RGB + depth preview for the GUI (writes .live/*.jpg). Runs until Stop.
    require_image; require_camera 1
    run_container preview -- "$PRELUDE"'
      start_camera
      wait_topic /camera/rgb/image_raw 40 || exit 2
      echo ">> camera streaming — live preview running. Press Stop (or Ctrl-C) to finish."
      run_main python3 /scanner/live_preview_node.py --ns camera --out /scanner/.live --hz 8'
    ;;

  snap)
    require_image; require_camera 1
    out_c=$(to_container_path "${2:-output/single_frame.ply}")
    run_container snap -e OUT="$out_c" -- "$PRELUDE"'
      start_camera
      wait_topic /camera/depth_registered/image_raw 40 || exit 2
      mkdir -p "$(dirname "$OUT")"
      echo ">> capturing one cloud frame …"
      run_main timeout 60 python3 /scanner/capture_cloud_ros.py "$OUT"
      [ -s "$OUT" ] && echo "SAVED $OUT" || { echo "ERROR: no frame captured" >&2; exit 2; }'
    echo "Saved: $HERE/${2:-output/single_frame.ply}"
    ;;

  map)
    # Headless SLAM. The map is written continuously to rtabmap.db; the GUI
    # polls it for telemetry and shows the live preview from .live/.
    require_image; require_camera 1
    run_container map -- "$PRELUDE"'
      start_camera
      wait_topic /camera/depth_registered/image_raw 40 || exit 2
      FRESH=""; [ "${MAP_FRESH:-1}" = "1" ] && FRESH="--delete_db_on_start"
      [ -n "$FRESH" ] && echo ">> starting a NEW map (rtabmap.db is reset)" || echo ">> CONTINUING the existing map"
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link \
        rtabmap_viz:=false rviz:=false \
        database_path:=/scanner/rtabmap.db \
        rtabmap_args:="$FRESH $RTAB_TUNE" odom_args:="$ODOM_TUNE" \
        > /scanner/ros_logs/rtabmap.log 2>&1 &
      EXTRA_PIDS="$!"
      echo ">> RTAB-Map SLAM running. Move the camera slowly; press Stop to finish."
      python3 /scanner/live_preview_node.py --ns camera --out /scanner/.live --hz 8 > /scanner/ros_logs/preview.log 2>&1 &
      MAIN_PID=$!
      while kill -0 "$MAIN_PID" 2>/dev/null; do
        sleep 10 & wait $!   # wait (not a foreground sleep) lets the TERM trap fire at once
        kill -0 "$EXTRA_PIDS" 2>/dev/null || { echo "ERROR: RTAB-Map exited — see ros_logs/rtabmap.log" >&2; tail -n 20 /scanner/ros_logs/rtabmap.log >&2; exit 2; }
        echo "   map: $(db_nodes) nodes"
      done'
    ;;

  gui)
    # SLAM + the rtabmap_viz window (global map, loop closures, File → Export).
    # Mapping starts automatically. Close the window to finish.
    require_image; require_camera 1; allow_x11
    # shellcheck disable=SC2046
    run_container gui $(gl_flags) -- "$PRELUDE"'
      start_camera
      wait_topic /camera/depth_registered/image_raw 40 || exit 2
      FRESH=""; [ "${MAP_FRESH:-1}" = "1" ] && FRESH="--delete_db_on_start"
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link \
        database_path:=/scanner/rtabmap.db \
        rtabmap_args:="$FRESH $RTAB_TUNE" odom_args:="$ODOM_TUNE" \
        rtabmap_viz:=true rviz:=false > /scanner/ros_logs/rtabmap.log 2>&1 &
      EXTRA_PIDS="$!"
      python3 /scanner/live_preview_node.py --ns camera --out /scanner/.live --hz 6 > /scanner/ros_logs/preview.log 2>&1 &
      EXTRA_PIDS="$EXTRA_PIDS $!"
      echo ">> RTAB-Map window starting — it maps automatically. Close the window to finish."
      i=0; while ! pgrep -f rtabmap_viz >/dev/null; do sleep 1; i=$((i+1)); [ $i -gt 60 ] && { echo "ERROR: rtabmap_viz did not start — see ros_logs/rtabmap.log" >&2; tail -n 20 /scanner/ros_logs/rtabmap.log >&2; exit 2; }; done
      while pgrep -f rtabmap_viz >/dev/null; do sleep 2; done
      echo ">> window closed — map saved to rtabmap.db ($(db_nodes) nodes)"
      graceful'
    ;;

  viz)
    require_image; require_camera 1; allow_x11
    # shellcheck disable=SC2046
    run_container viz $(gl_flags) -- "$PRELUDE"'
      start_camera
      wait_topic /camera/depth_registered/image_raw 40 || exit 2
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link rtabmap_viz:=false rviz:=false \
        database_path:=/scanner/rtabmap.db \
        rtabmap_args:="$RTAB_TUNE" odom_args:="$ODOM_TUNE" \
        > /scanner/ros_logs/rtabmap.log 2>&1 &
      EXTRA_PIDS="$!"; sleep 4
      echo ">> launching RViz (close the window to end)…"
      run_main rviz2 -d /scanner/scanner_cloud.rviz
      graceful'
    ;;

  export)
    # rtabmap-export takes a base NAME (+ --output_dir) and writes <dir>/<name>_cloud.ply.
    require_image
    db_h="${2:-rtabmap.db}"; out_h="${3:-output/scan.ply}"
    max_range="${4:-4.0}"; voxel="${5:-0.005}"
    # rtabmap-export's own default is --decimation 4: every 640x480 depth frame
    # becomes 160x120 points before voxelling, which is what made the clouds
    # blocky. 1 = full Xtion resolution (objects), 2 = rooms.
    decim="${6:-2}"
    db_host="$db_h"; [[ "$db_host" != /* ]] && db_host="$HERE/$db_host"
    if [ ! -s "$db_host" ]; then echo "ERROR: no map database at $db_host — run a scan first." >&2; exit 4; fi
    n=$(db_nodes "$db_host")
    if [ "$n" = "0" ]; then echo "ERROR: the map in $db_host is empty (0 nodes) — scan something first." >&2; exit 4; fi
    db_c=$(to_container_path "$db_h"); out_c=$(to_container_path "$out_h")
    out_dir="$(dirname "$out_c")"; out_name="$(basename "$out_c" .ply)"
    echo ">> exporting $n-node map: max range ${max_range} m, voxel ${voxel} m, depth decimation ${decim}"
    run_container export -e DB="$db_c" -e DIR="$out_dir" -e NM="$out_name" \
        -e MR="$max_range" -e VX="$voxel" -e DEC="$decim" -- '
      source /opt/ros/jazzy/setup.bash
      mkdir -p "$DIR"
      rtabmap-export --cloud --decimation "$DEC" --min_range 0.4 --max_range "$MR" --voxel "$VX" \
        --output "$NM" --output_dir "$DIR" "$DB"'
    out_host="${out_h%.ply}_cloud.ply"; [[ "$out_host" != /* ]] && out_host="$HERE/$out_host"
    if [ -s "$out_host" ]; then echo "Exported: $out_host"; else echo "ERROR: export produced no file at $out_host" >&2; exit 4; fi
    ;;

  multi)
    # Live view of ALL Xtions in cameras.json (multi_camera.launch.py) + RViz,
    # using the calibrated extrinsics (output/camera_extrinsics.json).
    # 3 Xtions on ONE USB-2 controller fit only depth-only @ QQVGA, opened
    # staggered (see multi_camera.launch.py). Overrides:
    #   MULTI_DEPTH_ONLY=0  MULTI_DMODE=QVGA_30Hz  MULTI_STAGGER=0
    require_image; require_camera 1; allow_x11
    NCAM=$(grep -c '"ns"' "$HERE/cameras.json" 2>/dev/null || echo 3)
    ST="${MULTI_STAGGER:-6}"
    # shellcheck disable=SC2046
    run_container multi $(gl_flags) -e PREVIEW_DEPTH_ONLY="${MULTI_DEPTH_ONLY:-1}" \
        -e PREVIEW_DMODE="${MULTI_DMODE:-QQVGA_30Hz}" -e PREVIEW_STAGGER="$ST" \
        -e WARM=$(( NCAM * ST + 8 )) -- "$PRELUDE"'
      ros2 launch /scanner/multi_camera.launch.py > "$LOG" 2>&1 & CAM_PID=$!
      echo ">> bringing up the cameras (staggered) — waiting ${WARM}s …"; sleep "$WARM"
      echo ">> camera cloud topics:"; ros2 topic list | grep -E "camera[0-9]+/depth_registered/points" || true
      echo ">> launching RViz (close the window to end)…"
      run_main rviz2 -d /scanner/scanner_multi.rviz
      graceful'
    ;;

  multi_slam)
    # EXPERIMENTAL: RTAB-Map SLAM fusing ALL 3 Xtions (RGB-D) at once.
    # Needs GOOD calibration first (overlap_check -> calibrate). Close the
    # rtabmap_viz window to finish. MULTI_SLAM_NOVIZ=1 runs headless for testing.
    require_image; require_camera 2; allow_x11
    NCAM=$(grep -c '"ns"' "$HERE/cameras.json" 2>/dev/null || echo 3)
    ST="${MULTI_STAGGER:-6}"
    # shellcheck disable=SC2046
    run_container multi_slam $(gl_flags) -e NCAM="$NCAM" -e ST="$ST" \
        -e NOVIZ="${MULTI_SLAM_NOVIZ:-0}" -- "$PRELUDE"'
      echo ">> bringing up $NCAM Xtions in RGB-D (QVGA), staggered…"
      PREVIEW_DEPTH_ONLY=0 PREVIEW_DMODE=QVGA_30Hz PREVIEW_STAGGER=$ST PREVIEW_DEVICE_TIME=0 \
        ros2 launch /scanner/multi_camera.launch.py > "$LOG" 2>&1 & CAM_PID=$!
      sleep $(( NCAM * ST ))
      up=0
      for t in $(seq 1 15); do
        up=$(ros2 topic list 2>/dev/null | grep -cE "camera[0-9]+/depth_registered/image_raw")
        echo "   RGB-D streams up: $up/$NCAM (check $t)"
        [ "$up" -ge "$NCAM" ] && break
        sleep 4
      done
      [ "$up" -ge "$NCAM" ] || echo "WARN: only $up/$NCAM RGB-D streams up — 3 on one USB-2 bus is marginal (see ros_logs/multi_slam.log)."
      echo ">> starting per-camera rgbd_sync…"
      for c in camera1 camera2 camera3; do
        ros2 run rtabmap_sync rgbd_sync --ros-args -r __ns:=/$c \
          -r rgb/image:=/$c/rgb/image_raw -r depth/image:=/$c/depth_registered/image_raw \
          -r rgb/camera_info:=/$c/rgb/camera_info -p approx_sync:=true \
          > /scanner/ros_logs/sync_$c.log 2>&1 &
        EXTRA_PIDS="$EXTRA_PIDS $!"
      done
      sleep 6
      echo ">> starting rgbd_odometry (3-camera)…"
      ros2 run rtabmap_odom rgbd_odometry --ros-args \
        -p subscribe_rgbd:=true -p rgbd_cameras:=3 -p approx_sync:=true \
        -p frame_id:=camera1_link -p wait_for_transform:=0.3 \
        -r rgbd_image0:=/camera1/rgbd_image -r rgbd_image1:=/camera2/rgbd_image \
        -r rgbd_image2:=/camera3/rgbd_image > /scanner/ros_logs/odom.log 2>&1 &
      EXTRA_PIDS="$EXTRA_PIDS $!"
      sleep 6
      echo ">> starting rtabmap (3-camera fusion, anchored at camera1_link)…"
      ros2 run rtabmap_slam rtabmap --delete_db_on_start --ros-args \
        -p subscribe_rgbd:=true -p rgbd_cameras:=3 -p approx_sync:=true \
        -p frame_id:=camera1_link -p database_path:=/scanner/rtabmap.db \
        -r rgbd_image0:=/camera1/rgbd_image -r rgbd_image1:=/camera2/rgbd_image \
        -r rgbd_image2:=/camera3/rgbd_image > /scanner/ros_logs/rtabmap.log 2>&1 &
      EXTRA_PIDS="$EXTRA_PIDS $!"
      sleep 10
      if [ "$NOVIZ" = "1" ]; then
        sleep 6
        echo ">> headless: odometry status…"; grep -iE "Odom:|odometry|Did not receive|lost" /scanner/ros_logs/odom.log | tail -5
        echo ">> headless: rtabmap status…"; grep -iE "Rtabmap.*processed|added to|Did not receive|Long-term" /scanner/ros_logs/rtabmap.log | tail -5
        ros2 node list | grep -E "rtabmap|rgbd_sync|rgbd_odometry" | head
      else
        echo ">> launching rtabmap_viz — close the window to finish."
        run_main ros2 run rtabmap_viz rtabmap_viz --ros-args \
          -p subscribe_rgbd:=true -p rgbd_cameras:=3 -p approx_sync:=true -p frame_id:=camera1_link \
          -r rgbd_image0:=/camera1/rgbd_image -r rgbd_image1:=/camera2/rgbd_image -r rgbd_image2:=/camera3/rgbd_image
      fi
      graceful'
    ;;

  station)
    # Matterport-style station/sweep scanning (host-side .venv + Open3D; it
    # starts the camera container itself per sweep). See MATTERPORT_REPLICATION_PLAN.md.
    require_image
    [ "${2:-capture}" = "capture" ] && require_camera 1
    PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    shift || true
    "$PY" -u "$HERE/station_scan.py" "${@:-capture}"
    ;;

  calibrate)
    require_image; require_camera 2
    PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    shift || true
    "$PY" -u "$HERE/calibrate_multi.py" "$@"
    ;;

  overlap)
    require_image; require_camera 2
    PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    shift || true
    "$PY" -u "$HERE/overlap_check.py" "$@"
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    sed -n '9,27p' "$0" >&2
    exit 1 ;;
esac
