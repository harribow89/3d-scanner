#!/usr/bin/env bash
# Integration test: fake camera -> live_preview_node inside the ROS container.
# Needs Docker + the scanner-ros:jazzy image; no real camera.
#   tests/test_preview_docker.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=scanner-ros:jazzy
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker); fi

LIVE="$HERE/.live"
mkdir -p "$LIVE"
rm -f "$LIVE"/camera_rgb.jpg "$LIVE"/camera_depth.jpg "$LIVE"/status.json

"${DOCKER[@]}" run --rm --network host --label scanner-ros=1 \
  --name "scanner-test-preview-$$" -v "$HERE":/scanner -w /scanner \
  "$IMAGE" bash -lc '
    source /opt/ros/jazzy/setup.bash
    python3 /scanner/tests/fake_camera.py --hz 10 --seconds 9 > /tmp/fake.log 2>&1 &
    sleep 2
    python3 /scanner/live_preview_node.py --out /scanner/.live --hz 8 --seconds 6
    wait
    echo "--- fake camera log ---"; tail -3 /tmp/fake.log'

sudo -n chown -R "$(id -u):$(id -g)" "$LIVE" 2>/dev/null || true
echo "--- .live ---"
ls -la "$LIVE"
python3 - "$LIVE" <<'PY'
import json, sys, os
live = sys.argv[1]
s = json.load(open(os.path.join(live, "status.json")))
cam = s["cameras"]["camera"]
print(json.dumps(cam, indent=1))
assert cam["rgb_frames"] > 20, "too few rgb frames"
assert cam["depth_frames"] > 20, "too few depth frames"
assert cam["rgb_fps"] > 5, "rgb fps too low"
assert cam["depth_fill_pct"] > 90, "depth fill too low"
assert 1.0 < cam["median_depth_m"] < 2.5, "median depth implausible"
for n in ("camera_rgb.jpg", "camera_depth.jpg"):
    p = os.path.join(live, n)
    assert os.path.getsize(p) > 2000, f"{n} too small"
print("PREVIEW TEST PASSED")
PY
