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
#   ./run_scanner_docker.sh viz          # live view: camera + RTAB-Map + RViz on $DISPLAY
#   ./run_scanner_docker.sh gui          # full RTAB-Map SLAM GUI (global map + export)
#   ./run_scanner_docker.sh multi        # ALL Xtions in cameras.json + RViz (calibrated)
#   ./run_scanner_docker.sh station capture|build|list   # station/sweep scan (Matterport-style)
#   ./run_scanner_docker.sh calibrate [--offline [dir]]  # solve multi-cam extrinsics (sequential+chained)
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

# RTAB-Map tuning for the ASUS Xtion (handheld). The Xtion has a narrow FOV and
# noisy depth, so defaults drift/lose tracking ("captures all over the place"):
#   Vis/MaxDepth 3.5        - ignore features past ~3.5 m (Xtion depth is junk beyond that)
#   Vis/MinInliers 15       - keep tracking on lower-texture scenes (default 20)
#   Odom/ResetCountdown 5   - auto-recover after losing odometry (default 0=never -> garbage poses)
#   RGBD/Linear|AngularUpdate 0.05 - add keyframes on smaller motion (denser, fewer gaps)
#   Rtabmap/DetectionRate 2 - update the map at 2 Hz instead of 1
#   RGBD/NeighborLinkRefining true - refine consecutive links to reduce drift
# Override at call time: RTAB_TUNE="..." ./run_scanner_docker.sh map
RTAB_TUNE="${RTAB_TUNE:---Vis/MaxDepth 3.5 --Vis/MinInliers 15 --Odom/ResetCountdown 5 --RGBD/LinearUpdate 0.05 --RGBD/AngularUpdate 0.05 --Rtabmap/DetectionRate 2 --RGBD/NeighborLinkRefining true}"

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
    $DOCKER run "${COMMON[@]}" -e RTAB_TUNE="$RTAB_TUNE" "$IMAGE" bash -lc '
      source /opt/ros/jazzy/setup.bash
      ros2 launch openni2_camera camera_with_cloud.launch.py & sleep 12
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link \
        database_path:=/scanner/rtabmap.db rtabmap_args:="$RTAB_TUNE"'
    ;;
  gui)
    # Hands-off RTAB-Map SLAM GUI: camera + RTAB-Map node + rtabmap_viz. The ROS
    # rtabmap node maps AUTOMATICALLY as frames arrive (no "press Start") — the
    # window opens already building a global 3D map. Walk around slowly; loop
    # closures correct drift. Save via File -> Export 3D clouds (to /scanner/output).
    # Fresh map each run (--delete_db_on_start); map persists at /scanner/rtabmap.db.
    # Close the window to finish.
    command -v xhost >/dev/null 2>&1 && xhost +local:root >/dev/null 2>&1 || true
    # Prefer the NVIDIA GPU (needs nvidia-container-toolkit); else fall back to a
    # DRI render node (Intel/AMD). Without either, GL is software (llvmpipe) and
    # rtabmap_viz/RViz are laggy.
    GL=()
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      GL=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all)
    elif [ -e /dev/dri ]; then
      GL=(--device /dev/dri:/dev/dri)
    fi
    $DOCKER run "${COMMON[@]}" "${GL[@]}" -e RTAB_TUNE="$RTAB_TUNE" -e HOME=/scanner -w /scanner "$IMAGE" bash -lc '
      source /opt/ros/jazzy/setup.bash
      ros2 launch openni2_camera camera_with_cloud.launch.py > /tmp/cam.log 2>&1 & sleep 12
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link \
        database_path:=/scanner/rtabmap.db rtabmap_args:="--delete_db_on_start $RTAB_TUNE" \
        rtabmap_viz:=true rviz:=false > /tmp/rtabmap.log 2>&1 & LP=$!
      echo ">> RTAB-Map UI starting — it begins mapping automatically. Close the window to finish."
      sleep 10
      while ! pgrep -f rtabmap_viz >/dev/null; do sleep 1; done   # wait for GUI
      while pgrep -f rtabmap_viz >/dev/null; do sleep 2; done     # until window closed
      kill $LP 2>/dev/null; sleep 1'
    sudo chown -R "$(id -u):$(id -g)" "$HERE/.ros" "$HERE/output" "$HERE/rtabmap.db" 2>/dev/null || true
    ;;
  multi)
    # Live view of ALL Xtions in cameras.json (multi_camera.launch.py) + RViz,
    # using the markerless-calibrated extrinsics (output/camera_extrinsics.json).
    # Edit cameras.json to add/remove cameras; run calibrate_multi.py to align.
    #
    # Fitting 3 Xtions on ONE USB-2 controller: validated 3/3 concurrent streams
    # with this combination (anything heavier drops the 3rd with "Failed to set
    # USB interface!"):
    #   - DEPTH-ONLY   (no colour isochronous endpoint to reserve)
    #   - QQVGA_30Hz   (160x120; QVGA only fits 2 cameras on one bus)
    #   - STAGGERED open (cam1@0s, cam2@6s, cam3@12s) so the drivers don't all
    #     race to reserve their USB interface at once
    # These are the defaults below. Overrides:
    #   MULTI_DEPTH_ONLY=0     -> colour clouds (only with <=2 cams on one bus)
    #   MULTI_DMODE=QVGA_30Hz  -> higher-res depth (only with <=2 cams on one bus)
    #   MULTI_STAGGER=0        -> open all cameras at once (1-2 cam rigs)
    command -v xhost >/dev/null 2>&1 && xhost +local:root >/dev/null 2>&1 || true
    # Prefer the NVIDIA GPU (needs nvidia-container-toolkit); else fall back to a
    # DRI render node (Intel/AMD). Without either, GL is software (llvmpipe) and
    # rtabmap_viz/RViz are laggy.
    GL=()
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      GL=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all)
    elif [ -e /dev/dri ]; then
      GL=(--device /dev/dri:/dev/dri)
    fi
    DO="${MULTI_DEPTH_ONLY:-1}"
    DM="${MULTI_DMODE:-QQVGA_30Hz}"
    ST="${MULTI_STAGGER:-6}"
    ENVS=(-e PREVIEW_DEPTH_ONLY="$DO" -e PREVIEW_DMODE="$DM" -e PREVIEW_STAGGER="$ST")
    # Wait long enough for the last staggered camera (n*stagger) to come up before RViz.
    NCAM=$(grep -c '"ns"' "$HERE/cameras.json" 2>/dev/null || echo 3)
    WARM=$(( NCAM * ST + 8 ))
    $DOCKER run "${COMMON[@]}" "${GL[@]}" "${ENVS[@]}" "$IMAGE" bash -lc "
      source /opt/ros/jazzy/setup.bash
      ros2 launch /scanner/multi_camera.launch.py > /tmp/multi.log 2>&1 & sleep $WARM
      echo '>> camera cloud topics:'; ros2 topic list | grep -E 'camera[0-9]+/depth_registered/points' || true
      echo '>> launching RViz (close the window to end)…'
      rviz2 -d /scanner/scanner_multi.rviz"
    ;;
  multi_slam)
    # EXPERIMENTAL: live RTAB-Map SLAM fusing ALL 3 Xtions (RGB-D) at once, for a
    # much wider field of view than single-camera SLAM.
    #
    # Bandwidth: TESTED OK — 3x RGB-D @ QVGA_30Hz hold ~29 Hz on this one USB-2
    # bus (staggered open), so 3-cam SLAM is feasible here (the old "depth-only
    # QQVGA" limit in the `multi` notes was too conservative).
    #
    # REQUIRES GOOD CALIBRATION: the 3 cameras are fused through the TF tree from
    # output/camera_extrinsics.json. With the default zero-baseline guess the map
    # will be misaligned/unusable. Aim the cameras for overlap (overlap_check.py
    # -> GOOD), run './run_scanner_docker.sh calibrate', THEN this.
    #
    # Close the rtabmap_viz window to finish. Fresh map each run.
    command -v xhost >/dev/null 2>&1 && xhost +local:root >/dev/null 2>&1 || true
    GL=()
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      GL=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all)
    elif [ -e /dev/dri ]; then
      GL=(--device /dev/dri:/dev/dri)
    fi
    ST="${MULTI_STAGGER:-6}"
    NCAM=$(grep -c '"ns"' "$HERE/cameras.json" 2>/dev/null || echo 3)
    WARM=$(( NCAM * ST + 14 ))
    NOVIZ="${MULTI_SLAM_NOVIZ:-0}"   # 1 = headless (no rtabmap_viz), for testing
    $DOCKER run "${COMMON[@]}" "${GL[@]}" -e HOME=/scanner -w /scanner "$IMAGE" bash -lc "
      source /opt/ros/jazzy/setup.bash
      echo '>> bringing up 3 Xtions in RGB-D (QVGA), staggered…'
      PREVIEW_DEPTH_ONLY=0 PREVIEW_DMODE=QVGA_30Hz PREVIEW_STAGGER=$ST PREVIEW_DEVICE_TIME=0 \
        ros2 launch /scanner/multi_camera.launch.py > /tmp/multi.log 2>&1 &
      sleep $(( NCAM * ST ))
      echo '>> waiting for all $NCAM RGB-D streams to come up…'
      up=0
      for t in \$(seq 1 15); do
        up=\$(ros2 topic list 2>/dev/null | grep -cE 'camera[0-9]+/depth_registered/image_raw')
        echo \"   RGB-D streams up: \$up/$NCAM (check \$t)\"
        [ \"\$up\" -ge $NCAM ] && break
        sleep 4
      done
      [ \"\$up\" -ge $NCAM ] || echo 'WARN: only '\$up'/$NCAM RGB-D streams up — 3 on one USB-2 bus is marginal; the map may use fewer cameras (see /tmp/multi.log).'
      echo '>> starting per-camera rgbd_sync…'
      for c in camera1 camera2 camera3; do
        ros2 run rtabmap_sync rgbd_sync --ros-args -r __ns:=/\$c \
          -r rgb/image:=/\$c/rgb/image_raw \
          -r depth/image:=/\$c/depth_registered/image_raw \
          -r rgb/camera_info:=/\$c/rgb/camera_info \
          -p approx_sync:=true > /tmp/sync_\$c.log 2>&1 &
      done
      sleep 6
      echo '>> starting rgbd_odometry (3-camera) — the odom source rtabmap needs…'
      ros2 run rtabmap_odom rgbd_odometry --ros-args \
        -p subscribe_rgbd:=true -p rgbd_cameras:=3 -p approx_sync:=true \
        -p frame_id:=camera1_link -p wait_for_transform:=0.3 \
        -r rgbd_image0:=/camera1/rgbd_image \
        -r rgbd_image1:=/camera2/rgbd_image \
        -r rgbd_image2:=/camera3/rgbd_image > /tmp/odom.log 2>&1 &
      sleep 6
      echo '>> starting rtabmap (3-camera fusion, anchored at camera1_link)…'
      # NOTE: RTAB-Map core params (Rtabmap/*, Vis/*) are string-typed — passing
      # them as -p name:=1.5 throws InvalidParameterTypeException. Tune via a
      # params file if needed; defaults are fine here.
      ros2 run rtabmap_slam rtabmap --delete_db_on_start --ros-args \
        -p subscribe_rgbd:=true -p rgbd_cameras:=3 -p approx_sync:=true \
        -p frame_id:=camera1_link -p database_path:=/scanner/rtabmap.db \
        -r rgbd_image0:=/camera1/rgbd_image \
        -r rgbd_image1:=/camera2/rgbd_image \
        -r rgbd_image2:=/camera3/rgbd_image > /tmp/rtabmap.log 2>&1 &
      sleep 10
      if [ '$NOVIZ' = '1' ]; then
        sleep 6
        echo '>> headless: rgbd_odometry status…'
        grep -iE 'Odom:|odometry|Did not receive|lost' /tmp/odom.log | tail -5
        echo '>> headless: rtabmap status…'
        grep -iE 'Rtabmap.*processed|added to|Did not receive|Long-term' /tmp/rtabmap.log | tail -5
        echo '--- node graph ---'; ros2 node list | grep -E 'rtabmap|rgbd_sync|rgbd_odometry' | head
        sleep 2
      else
        echo '>> launching rtabmap_viz — close the window to finish.'
        ros2 run rtabmap_viz rtabmap_viz --ros-args \
          -p subscribe_rgbd:=true -p rgbd_cameras:=3 -p approx_sync:=true \
          -p frame_id:=camera1_link \
          -r rgbd_image0:=/camera1/rgbd_image \
          -r rgbd_image1:=/camera2/rgbd_image \
          -r rgbd_image2:=/camera3/rgbd_image
      fi"
    sudo chown -R "$(id -u):$(id -g)" "$HERE/rtabmap.db" "$HERE/.ros" 2>/dev/null || true
    ;;
  station)
    # Matterport-style station/sweep scanning (Stage 1, see MATTERPORT_REPLICATION_PLAN.md).
    # Runs host-side (.venv + Open3D); station_scan.py spins up the camera container
    # itself per sweep. Subcommands: capture | build | list | clear.
    #   ./run_scanner_docker.sh station capture     # at each tripod position
    #   ./run_scanner_docker.sh station build        # stitch all sweeps -> output/room.ply
    PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    shift || true
    "$PY" "$HERE/station_scan.py" "${@:-capture}"
    ;;
  calibrate)
    # Solve multi-camera extrinsics (output/camera_extrinsics.json). Runs host-side
    # (.venv + Open3D); calibrate_multi.py captures one cloud per camera SEQUENTIALLY
    # (bandwidth-safe) and chains adjacent-pair registration (cam2->cam1, cam3->cam2).
    # Aim adjacent cameras at a SHARED structured scene ~1-1.5 m away first.
    #   ./run_scanner_docker.sh calibrate              # live: sequential capture + solve
    #   ./run_scanner_docker.sh calibrate --offline    # solve from saved sweep clouds
    # Eyeball the result:  .venv/bin/python fuse_check.py && python3 view_cloud.py output/alignment_check.ply
    PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    shift || true
    "$PY" "$HERE/calibrate_multi.py" "$@"
    ;;
  viz)
    # Live view: camera + RTAB-Map + RViz showing /camera/depth_registered/points.
    # Move the camera slowly; the live cloud renders on your $DISPLAY.
    command -v xhost >/dev/null 2>&1 && xhost +local:root >/dev/null 2>&1 || true
    # Prefer the NVIDIA GPU (needs nvidia-container-toolkit); else fall back to a
    # DRI render node (Intel/AMD). Without either, GL is software (llvmpipe) and
    # rtabmap_viz/RViz are laggy.
    GL=()
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
      GL=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all)
    elif [ -e /dev/dri ]; then
      GL=(--device /dev/dri:/dev/dri)
    fi
    $DOCKER run "${COMMON[@]}" "${GL[@]}" \
      -e LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-0}" \
      "$IMAGE" bash -lc '
      source /opt/ros/jazzy/setup.bash
      ros2 launch openni2_camera camera_with_cloud.launch.py > /tmp/cam.log 2>&1 & sleep 12
      ros2 launch rtabmap_launch rtabmap.launch.py \
        rgb_topic:=/camera/rgb/image_raw \
        depth_topic:=/camera/depth_registered/image_raw \
        camera_info_topic:=/camera/rgb/camera_info \
        approx_sync:=true frame_id:=camera_link \
        database_path:=/scanner/rtabmap.db rviz:=false > /tmp/rtabmap.log 2>&1 & sleep 4
      echo ">> launching RViz (close the window to end the scan)…"
      rviz2 -d /scanner/scanner_cloud.rviz'
    # rtabmap.db is written by root in the container; hand it back to the host user
    sudo chown "$(id -u):$(id -g)" "$HERE/rtabmap.db" 2>/dev/null || true
    ;;
  export)
    # NOTE: rtabmap-export takes a base NAME (+ --output_dir) and writes
    # <dir>/<name>_cloud.ply — passing an absolute path to --output makes it
    # concatenate onto output_dir. So split dir/name explicitly here.
    db="${2:-/scanner/rtabmap.db}"; out="${3:-/scanner/output/scan.ply}"
    out_dir="$(dirname "$out")"; out_name="$(basename "$out" .ply)"
    # Room-scan tuning: keep far walls (default range is only 4 m) and
    # voxel-downsample to a 2 cm grid so a whole-room cloud stays manageable.
    # Override per-run: ./run_scanner_docker.sh export <db> <out> <max_range_m> <voxel_m>
    max_range="${4:-6.0}"; voxel="${5:-0.02}"
    $DOCKER run "${COMMON[@]}" "$IMAGE" bash -lc "
      source /opt/ros/jazzy/setup.bash
      mkdir -p '$out_dir'
      rtabmap-export --cloud --max_range '$max_range' --voxel '$voxel' --output '$out_name' --output_dir '$out_dir' '$db'"
    sudo chown -R "$(id -u):$(id -g)" "$HERE/output" 2>/dev/null || true
    echo "Exported: ${out_dir}/${out_name}_cloud.ply"
    ;;
  *)
    echo "Unknown command: $cmd"; sed -n '2,20p' "$0"; exit 1 ;;
esac
