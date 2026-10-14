#!/usr/bin/env bash
# One-time host setup: the Python venv for the GUI + point-cloud tools, and the
# Docker image with ROS/RTAB-Map/OpenNI2. Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HERE/.venv"
REQ_FILE="$HERE/requirements.txt"

echo "== 3D scanner setup =="
echo "Project dir: $HERE"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed." >&2
  exit 1
fi

# PySide6's "xcb" platform plugin (Qt 6.5+) needs libxcb-cursor0, or the GUI
# fails with: "Could not load the Qt platform plugin xcb".
if ! ldconfig -p 2>/dev/null | grep -q libxcb-cursor; then
  echo "Installing GUI system dependency: libxcb-cursor0"
  sudo apt-get install -y libxcb-cursor0 \
    || echo "WARNING: could not install libxcb-cursor0; the GUI may not open."
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
else
  echo "Using existing virtualenv at $VENV_DIR"
fi
PY="$VENV_DIR/bin/python"
"$PY" -m pip install --upgrade pip setuptools wheel
echo "Installing Python dependencies from $REQ_FILE"
"$PY" -m pip install -r "$REQ_FILE" || {
  echo "WARNING: pip install failed. Open3D needs Python 3.10–3.12; check: $PY --version" >&2
}

echo
echo "Python smoke checks:"
"$PY" - <<'PY'
ok = True
for mod in ("numpy", "scipy", "open3d", "cv2", "PIL", "PySide6"):
    try:
        __import__(mod)
        print(f"  [ok] {mod}")
    except Exception as exc:
        ok = False
        print(f"  [missing] {mod}: {type(exc).__name__}: {exc}")
raise SystemExit(0 if ok else 1)
PY

echo
if command -v docker >/dev/null 2>&1; then
  echo "[ok] docker present"
  if "$HERE/run_scanner_docker.sh" doctor --quick | grep -q "Image scanner-ros:jazzy.*not built"; then
    echo "Building the Docker image (ROS 2 Jazzy + RTAB-Map + OpenNI2, several minutes)…"
    "$HERE/run_scanner_docker.sh" build
  fi
else
  echo "[missing] docker — install it: sudo apt install docker.io" >&2
fi

echo
echo "Setup complete. Next:"
echo "  ./run_scanner_docker.sh doctor     # check camera / docker / image"
echo "  ./launch_gui.sh                    # the control panel"
echo "  ./run_scanner_docker.sh camera     # 20 s camera test from the terminal"
