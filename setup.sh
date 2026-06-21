#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The GUI (launch_gui.sh) and scanner_gui.py invoke ./.venv/bin/python relative to
# this project dir, so the venv MUST live here, not in the parent.
VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

echo "== Scanner setup =="
echo "Project dir: $SCRIPT_DIR"
echo "Venv:        $VENV_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is not installed."
  exit 1
fi

# PySide6's "xcb" platform plugin (Qt 6.5+) needs libxcb-cursor0, or the GUI
# fails with: "Could not load the Qt platform plugin xcb". Install if missing.
if ! ldconfig -p 2>/dev/null | grep -q libxcb-cursor; then
  echo "Installing GUI system dependency: libxcb-cursor0"
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get install -y libxcb-cursor0 \
      || echo "WARNING: could not install libxcb-cursor0; the PySide6 GUI may not open."
  else
    echo "WARNING: libxcb-cursor0 missing and sudo unavailable; install it for the GUI."
  fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
else
  echo "Using existing virtualenv at $VENV_DIR"
fi

PY_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

"$PY_BIN" -m pip install --upgrade pip setuptools wheel

if [[ -f "$REQ_FILE" ]]; then
  echo "Installing core Python dependencies from $REQ_FILE"
  "$PIP_BIN" install -r "$REQ_FILE"
else
  echo "WARNING: requirements file missing at $REQ_FILE"
fi

echo "Installing optional package: open3d"
if ! "$PIP_BIN" install open3d; then
  echo "WARNING: open3d install failed."
  echo "This is usually due to Python version compatibility (often Python > 3.12)."
  echo "If you need point cloud isolation/viewer features, use Python 3.11 or 3.12 for the venv."
fi

echo ""
echo "Running Python smoke checks..."
"$PY_BIN" - <<'PY'
checks = [
    ("numpy", "numpy"),
    ("open3d", "open3d"),
    ("cv2", "opencv-python"),
    ("PIL", "pillow"),
    ("anthropic", "anthropic"),
    ("openai", "openai"),
]

ok = True
for mod, pkg in checks:
    try:
        __import__(mod)
        print(f"[ok] {mod}")
    except Exception as exc:
        ok = False
        print(f"[missing] {mod} (pip package: {pkg}) -> {type(exc).__name__}: {exc}")

if not ok:
    print("\\nOne or more optional modules are missing. The launcher can still start,")
    print("but some features (point cloud tools or AI providers) may be unavailable.")
PY

echo ""
echo "Runtime checks:"
if command -v ros2 >/dev/null 2>&1; then
  echo "[ok] ros2 CLI present"
else
  echo "[missing] ros2 CLI"
fi

if command -v NiViewer2 >/dev/null 2>&1; then
  echo "[ok] NiViewer2 present"
else
  echo "[missing] NiViewer2"
fi

if command -v docker >/dev/null 2>&1; then
  echo "[ok] docker present"
else
  echo "[missing] docker"
fi

echo ""
echo "Setup complete."
echo "Next steps (Docker-native — no host ROS needed):"
echo "  1) $SCRIPT_DIR/run_scanner_docker.sh build      # build the ROS image"
echo "  2) $SCRIPT_DIR/run_scanner_docker.sh camera     # verify a single Xtion streams"
echo "  3) $SCRIPT_DIR/run_scanner_docker.sh multi      # all 3 cameras live (after calibrate)"
echo "  4) $SCRIPT_DIR/launch_gui.sh                     # PySide6 control panel (optional)"