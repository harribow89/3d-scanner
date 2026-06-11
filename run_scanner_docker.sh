#!/usr/bin/env bash
# Run the ROS 2 Jazzy + RTAB-Map + OpenNI2 scanner stack inside Docker.
#
# Kali is not a supported ROS apt target, so ROS lives in the `scanner-ros:jazzy`
# image (built from ./Dockerfile). The host just passes through USB + X11.
#
# Verified working 2026-06-11: a single Xtion streams /camera/depth_registered/points
# at ~30 Hz through this setup.
#
# Usage:
#   ./run_scanner_docker.sh build        # build the image
#   ./run_scanner_docker.sh camera       # launch camera + cloud, print topic rate
#   ./run_scanner_docker.sh snap [out.ply]   # capture ONE point-cloud frame to PLY
#   ./run_scanner_docker.sh shell        # interactive ROS shell in the container
#   ./run_scanner_docker.sh map          # camera + RTAB-Map SLAM (move camera slowly)
#   ./run_scanner_docker.sh export [db] [out.ply]   # rtabmap-export a cloud
set -euo pipefail

IMAGE=scanner-ros:jazzy
HERE="$(cd "$(dirname "$0")" && pwd)"
DOCKER="sudo docker"   # user 'jim' is not in the docker group; sudo is passwordless

# Common flags: privileged + USB passthrough for OpenNI2, host net for DDS,
# mount the project dir at /scanner so rtabmap.db / output/ persist on the host.
COMMON=(--rm --privileged --network host
        -v /dev/bus/usb:/dev/bus/usb
        -v "$HERE":/scanner
        -e DISPLAY="${DISPLAY:-}"
        -v /tmp/.X11-unix:/tmp/.X11-unix)

cmd="${1:-camera}"
case "$cmd" in
  build)
    $DOCKER build -t "$IMAGE" -f "$HERE/Dockerfile" "$HERE"
    ;;
  camera)
    $DOCKER run "${COMMON[@]}" "$IMAGE" bash -lc '
      source /opt/ros/jazzy/setup.bash
      ros2 launch openni2_camera camera_with_cloud.launch.py & sleep 16
      echo "=== topics ==="; ros2 topic list | grep -i camera
      echo "=== cloud rate ==="; timeout 8 ros2 topic hz /camera/depth_registered/points | head -4
      wait'
    ;;
  snap)
    out="${2:-/scanner/output/single_frame.ply}"
    $DOCKER run "${COMMON[@]}" "$IMAGE" bash -lc "
      source /opt/ros/jazzy/setup.bash
      mkdir -p /scanner/output
      ros2 launch openni2_camera camera_with_cloud.launch.py > /tmp/cam.log 2>&1 &
      sleep 16
      echo '>> capturing one cloud frame…'
      python3 /scanner/capture_cloud_ros.py '$out'
      pkill -INT -f camera_with_cloud 2>/dev/null; sleep 1"
    # container runs as root; hand ownership back to the host user
    sudo chown -R "$(id -u):$(id -g)" "$HERE/output" 2>/dev/null || true
    ;;
  shell)
    $DOCKER run -it "${COMMON[@]}" "$IMAGE" bash
    ;;
  map)
    $DOCKER run "${COMMON[@]}" "$IMAGE" bash -lc '
      source /opt/ros/jazzy/setup.bash
      ros2 launch openni2_camera camera_with_cloud.launch.py & sleep 12
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link \
        database_path:=/scanner/rtabmap.db'
    ;;
  export)
    db="${2:-/scanner/rtabmap.db}"; out="${3:-/scanner/output/scan.ply}"
    $DOCKER run "${COMMON[@]}" "$IMAGE" bash -lc "
      source /opt/ros/jazzy/setup.bash
      mkdir -p \$(dirname '$out')
      rtabmap-export --cloud --output '$out' '$db'"
    ;;
  *)
    echo "Unknown command: $cmd"; sed -n '2,20p' "$0"; exit 1 ;;
esac
