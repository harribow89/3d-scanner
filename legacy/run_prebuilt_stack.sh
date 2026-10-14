#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-help}"
ROOT_DIR="/home/jim/Desktop/scanner"
PID_FILE="/tmp/scanner_ros_stack.pids"
LOG_DIR="${ROOT_DIR}/ros_logs"
DEFAULT_DB_PATH="${ROOT_DIR}/rtabmap.db"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_ros_missing_help() {
  echo "ROS2 Jazzy is not installed on this host."
  echo "Expected: /opt/ros/jazzy/setup.bash"
  echo ""
  if command -v docker >/dev/null 2>&1; then
    echo "Docker fallback is available:"
    echo "  ${SCRIPT_DIR}/run_scanner_docker.sh build"
    echo "  ${SCRIPT_DIR}/run_scanner_docker.sh camera"
  else
    echo "Docker is also not available on this host."
    echo "Install either ROS2 Jazzy or Docker before running ROS stack modes."
  fi
}

print_help() {
  cat <<'EOF'
Usage:
  ./run_prebuilt_stack.sh easy
  ./run_prebuilt_stack.sh easy_watch [interval_seconds]
  ./run_prebuilt_stack.sh niviewer
  ./run_prebuilt_stack.sh ros_env
  ./run_prebuilt_stack.sh ros_camera
  ./run_prebuilt_stack.sh ros_all
  ./run_prebuilt_stack.sh ros_all_gui
  ./run_prebuilt_stack.sh ros_watch [interval_seconds] [with_rviz:0|1]
  ./run_prebuilt_stack.sh ros_agent_scan [seconds] [output_ply]
  ./run_prebuilt_stack.sh ros_stop
  ./run_prebuilt_stack.sh ros_status
  ./run_prebuilt_stack.sh ros_rviz
  ./run_prebuilt_stack.sh ros_rtabmap
  ./run_prebuilt_stack.sh ros_db
  ./run_prebuilt_stack.sh ros_export_cloud [db_path] [output_ply]

Most people should use one of these:
  1. ./run_prebuilt_stack.sh ros_all
     Starts the RTAB-Map mapping stack.
  2. ./run_prebuilt_stack.sh ros_stop
     Stops the RTAB-Map mapping stack.
  3. ./run_prebuilt_stack.sh ros_export_cloud
     Exports the current RTAB-Map database to a .ply cloud.

Modes:
  easy        Same as ros_all
  niviewer    Raw depth sanity check viewer
  ros_env     Print ROS setup command
  ros_camera  Launch only the OpenNI2 camera node
  ros_all     Start camera + RTAB-Map in the background
  ros_all_gui Start camera + RTAB-Map + RViz
  ros_watch   Keep the mapping stack alive with auto-restart
  ros_agent_scan Start camera + RTAB-Map, wait N seconds, then export a cloud
  ros_stop    Stop the mapping stack
  ros_status  Show current ROS/OpenNI process status
  ros_rviz    Launch RViz2
  ros_rtabmap Launch RTAB-Map only
  ros_db      Open the RTAB-Map database viewer
  ros_export_cloud Export the RTAB-Map database to a PLY cloud
EOF
}

ensure_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing command: $cmd"
    exit 1
  fi
}

ensure_openni_free() {
  local conflict
  conflict="$(pgrep -af 'scanner_gui.py|NiViewer2|openni2_camera' || true)"
  if [[ -n "$conflict" ]]; then
    echo "OpenNI camera(s) already in use by another process:"
    echo "$conflict"
    echo ""
    echo "Close other camera apps first, then retry."
    exit 1
  fi
}

source_ros() {
  if [[ -f /opt/ros/jazzy/setup.bash ]]; then
    # Avoid FastDDS shared-memory lock errors (fastrtps_portXXXX) on this host.
    export FASTDDS_BUILTIN_TRANSPORTS=UDPv4
    # shellcheck disable=SC1091
    set +u
    source /opt/ros/jazzy/setup.bash
    set -u
  else
    print_ros_missing_help
    exit 1
  fi
}

start_ros_bg() {
  local name="$1"
  shift
  local logfile="$LOG_DIR/${name}.log"
  mkdir -p "$LOG_DIR"

  # shellcheck disable=SC1091
  bash -lc "export FASTDDS_BUILTIN_TRANSPORTS=UDPv4; source /opt/ros/jazzy/setup.bash && $*" >"$logfile" 2>&1 &
  local pid=$!
  echo "$name:$pid" >> "$PID_FILE"
  echo "Started $name (pid $pid)"
  echo "  log: $logfile"
}

rtabmap_launch_args() {
  # Use camera_link for frame_id to match openni2_camera defaults and avoid base_link TF warnings.
  mkdir -p "$(dirname "$DEFAULT_DB_PATH")"
  printf '%s' "ros2 launch rtabmap_launch rtabmap.launch.py rgb_topic:=/camera/rgb/image_raw depth_topic:=/camera/depth_registered/image_raw camera_info_topic:=/camera/rgb/camera_info approx_sync:=true frame_id:=camera_link database_path:=${DEFAULT_DB_PATH}"
}

start_stack_stable() {
  stop_ros_bg
  rm -f "$PID_FILE"

  echo "Starting ROS mapping stack (stable mode: no RViz)..."
  start_ros_bg camera "ros2 launch openni2_camera camera_with_cloud.launch.py"
  sleep 2
  start_ros_bg rtabmap "$(rtabmap_launch_args)"

  echo ""
  echo "Stack started. Use './run_prebuilt_stack.sh ros_status' to check and './run_prebuilt_stack.sh ros_stop' to stop."
  echo "If you want visualization, run './run_prebuilt_stack.sh ros_rviz' in a separate terminal."
}

start_stack_with_rviz() {
  stop_ros_bg
  rm -f "$PID_FILE"

  echo "Starting full ROS mapping stack with RViz..."
  start_ros_bg camera "ros2 launch openni2_camera camera_with_cloud.launch.py"
  sleep 2
  start_ros_bg rtabmap "$(rtabmap_launch_args)"
  sleep 2
  start_ros_bg rviz "rviz2 -f /camera_link"

  echo ""
  echo "Stack started. Use './run_prebuilt_stack.sh ros_status' to check and './run_prebuilt_stack.sh ros_stop' to stop."
}

start_component_by_name() {
  local name="$1"
  case "$name" in
    camera)
      start_ros_bg camera "ros2 launch openni2_camera camera_with_cloud.launch.py"
      ;;
    rtabmap)
      start_ros_bg rtabmap "$(rtabmap_launch_args)"
      ;;
    rviz)
      start_ros_bg rviz "rviz2 -f /camera_link"
      ;;
    *)
      return 1
      ;;
  esac
}

watch_stack_loop() {
  local interval="$1"
  local with_rviz="$2"

  trap 'echo ""; echo "Stopping watcher..."; stop_ros_bg; exit 0' INT TERM

  echo "Watcher active. Interval=${interval}s, RViz=${with_rviz}. Press Ctrl+C to stop."
  while true; do
    local active_names=""

    if [[ -f "$PID_FILE" ]]; then
      while IFS=: read -r name pid; do
        if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
          active_names+="${name} "
        fi
      done < "$PID_FILE"
    fi

    local missing=""
    if [[ "$active_names" != *"camera "* ]]; then
      missing+="camera "
    fi
    if [[ "$active_names" != *"rtabmap "* ]]; then
      missing+="rtabmap "
    fi
    if [[ "$with_rviz" == "1" ]] && [[ "$active_names" != *"rviz "* ]]; then
      missing+="rviz "
    fi

    if [[ -n "$missing" ]]; then
      echo "[$(date +%H:%M:%S)] Restarting missing components: $missing"
      [[ "$missing" == *"camera "* ]] && start_component_by_name camera && sleep 2
      [[ "$missing" == *"rtabmap "* ]] && start_component_by_name rtabmap && sleep 2
      [[ "$missing" == *"rviz "* ]] && start_component_by_name rviz
    else
      echo "[$(date +%H:%M:%S)] Healthy: camera rtabmap$([[ "$with_rviz" == "1" ]] && printf ' rviz')"
    fi

    sleep "$interval"
  done
}

stop_ros_bg() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file found. Nothing to stop."
    return
  fi

  while IFS=: read -r name pid; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      echo "Stopped $name (pid $pid)"
    fi
  done < "$PID_FILE"

  rm -f "$PID_FILE"
}

show_status() {
  local has_live=0
  echo "OpenNI/ROS process status:"
  if pgrep -af 'scanner_gui.py|NiViewer2|openni2_camera|rtabmap.launch.py|rviz2' >/tmp/scanner_status_ps.txt; then
    cat /tmp/scanner_status_ps.txt
    has_live=1
  else
    echo "  none"
  fi

  if [[ -f "$PID_FILE" ]]; then
    local cleaned=""
    while IFS=: read -r name pid; do
      if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
        cleaned+="$name:$pid"$'\n'
      fi
    done < "$PID_FILE"

    if [[ -n "$cleaned" ]]; then
      printf '%s' "$cleaned" > "$PID_FILE"
    else
      rm -f "$PID_FILE"
    fi

    echo ""
    echo "Managed stack PIDs:"
    if [[ -f "$PID_FILE" ]]; then
      cat "$PID_FILE"
    else
      echo "none"
    fi
  else
    echo ""
    echo "Managed stack PIDs: none"
  fi

  rm -f /tmp/scanner_status_ps.txt
}

export_cloud_from_db() {
  local db_path="$1"
  local out_path="$2"
  # Optional quality flags: voxel noise_radius noise_k max_range min_range
  local voxel="${3:-0.003}"
  local noise_r="${4:-0.015}"
  local noise_k="${5:-5}"
  local max_range="${6:-1.5}"
  local min_range="${7:-0.10}"

  source_ros
  ensure_cmd rtabmap-export

  if [[ ! -f "$db_path" ]]; then
    echo "Database not found: $db_path"
    return 1
  fi

  local out_dir out_name
  out_dir="$(dirname "$out_path")"
  out_name="$(basename "$out_path" .ply)"
  mkdir -p "$out_dir"

  echo "Exporting cloud from: $db_path"
  echo "  voxel=${voxel}m  noise_r=${noise_r}m  noise_k=${noise_k}  range=${min_range}–${max_range}m"
  rtabmap-export \
    --cloud \
    --decimation 1 \
    --voxel "$voxel" \
    --noise_radius "$noise_r" \
    --noise_k "$noise_k" \
    --max_range "$max_range" \
    --min_range "$min_range" \
    --output "$out_name" \
    --output_dir "$out_dir" \
    "$db_path"
  echo "Cloud export finished."
  ls -1 "$out_dir" | grep -E "^${out_name}.*\\.(ply|pcd|las)$" || true
}

case "$MODE" in
  easy)
    MODE="ros_all"
    ;;

  easy_watch)
    ensure_openni_free
    source_ros
    ensure_cmd ros2

    WATCH_INTERVAL="${2:-5}"
    if ! [[ "$WATCH_INTERVAL" =~ ^[0-9]+$ ]] || (( WATCH_INTERVAL < 2 )); then
      echo "Invalid interval: $WATCH_INTERVAL (use integer >= 2)"
      exit 1
    fi

    start_stack_stable
    watch_stack_loop "$WATCH_INTERVAL" "0"
    ;;

  niviewer|ni|openni)
    ensure_openni_free
    ensure_cmd NiViewer2
    echo "Launching NiViewer2..."
    exec NiViewer2
    ;;

  ros_env)
    echo "Run this in each ROS terminal:"
    echo "  source /opt/ros/jazzy/setup.bash"
    ;;

  ros_camera|ros_camera1|camera)
    ensure_openni_free
    source_ros
    ensure_cmd ros2
    echo "Launching OpenNI2 camera node..."
    exec ros2 launch openni2_camera camera_with_cloud.launch.py
    ;;

  ros_all|all)
    ensure_openni_free
    source_ros
    ensure_cmd ros2
    start_stack_stable
    ;;

  ros_all_gui|all_gui)
    ensure_openni_free
    source_ros
    ensure_cmd ros2
    ensure_cmd rviz2
    start_stack_with_rviz
    ;;

  ros_watch|watch)
    ensure_openni_free
    source_ros
    ensure_cmd ros2

    WATCH_INTERVAL="${2:-5}"
    WITH_RVIZ="${3:-0}"

    if ! [[ "$WATCH_INTERVAL" =~ ^[0-9]+$ ]] || (( WATCH_INTERVAL < 2 )); then
      echo "Invalid interval: $WATCH_INTERVAL (use integer >= 2)"
      exit 1
    fi
    if [[ "$WITH_RVIZ" != "0" && "$WITH_RVIZ" != "1" ]]; then
      echo "Invalid with_rviz flag: $WITH_RVIZ (use 0 or 1)"
      exit 1
    fi
    if [[ "$WITH_RVIZ" == "1" ]]; then
      ensure_cmd rviz2
      start_stack_with_rviz
    else
      start_stack_stable
    fi

    watch_stack_loop "$WATCH_INTERVAL" "$WITH_RVIZ"
    ;;

  ros_agent_scan|agent_scan)
    ensure_openni_free
    source_ros
    ensure_cmd ros2

    SCAN_SECONDS="${2:-45}"
    OUT_PATH="${3:-${ROOT_DIR}/rtabmap_cloud_$(date +%Y%m%d_%H%M%S).ply}"
    DB_PATH="${DEFAULT_DB_PATH}"

    if ! [[ "$SCAN_SECONDS" =~ ^[0-9]+$ ]]; then
      echo "Invalid seconds: $SCAN_SECONDS"
      exit 1
    fi
    if (( SCAN_SECONDS < 5 )); then
      echo "Scan duration too short. Use at least 5 seconds."
      exit 1
    fi

    stop_ros_bg
    rm -f "$PID_FILE"

    echo "Starting agent scan for ${SCAN_SECONDS}s..."
    start_ros_bg camera "ros2 launch openni2_camera camera_with_cloud.launch.py"
    sleep 2
    start_ros_bg rtabmap "$(rtabmap_launch_args)"

    echo "Move around the object now..."
    sleep "$SCAN_SECONDS"

    stop_ros_bg
    pkill -f 'openni2_camera|rtabmap.launch.py' || true

    export_cloud_from_db "$DB_PATH" "$OUT_PATH"
    echo "Agent scan complete: $OUT_PATH"
    ;;

  ros_stop|stop)
    stop_ros_bg
    pkill -f 'openni2_camera|rtabmap.launch.py|rviz2' || true
    echo "Requested stop for ROS mapping stack."
    ;;

  ros_status|status)
    show_status
    ;;

  ros_rviz)
    source_ros
    ensure_cmd rviz2
    echo "Launching RViz2..."
    exec rviz2 -f /camera_link
    ;;

  ros_rtabmap)
    source_ros
    ensure_cmd ros2
    echo "Launching RTAB-Map (RGB-D + odometry + mapping)..."
    exec ros2 launch rtabmap_launch rtabmap.launch.py \
      rgb_topic:=/camera/rgb/image_raw \
      depth_topic:=/camera/depth_registered/image_raw \
      camera_info_topic:=/camera/rgb/camera_info \
      approx_sync:=true \
      frame_id:=camera_link
    ;;

  ros_db|db)
    source_ros
    ensure_cmd rtabmap-databaseViewer
    DB_PATH="${2:-$DEFAULT_DB_PATH}"
    if [[ ! -f "$DB_PATH" ]]; then
      echo "Database not found: $DB_PATH"
      exit 1
    fi
    echo "Opening RTAB-Map database viewer: $DB_PATH"
    exec rtabmap-databaseViewer "$DB_PATH"
    ;;

  ros_export_cloud|export_cloud)
    DB_PATH="${2:-$DEFAULT_DB_PATH}"
    OUT_PATH="${3:-${ROOT_DIR}/rtabmap_cloud_$(date +%Y%m%d_%H%M%S).ply}"
    export_cloud_from_db "$DB_PATH" "$OUT_PATH"
    ;;

  *)
    print_help
    ;;
esac
