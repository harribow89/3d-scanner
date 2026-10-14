#!/usr/bin/env bash
# Launch the scanner control panel (scanner_gui.py).
#
#   ./launch_gui.sh                 # the GUI
#   ./launch_gui.sh --smoke         # offscreen self-test (CI / "does it start?")
#
# Uses the project venv (.venv, created by ./setup.sh) when it has PySide6,
# otherwise falls back to the system python3 if that has PySide6.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

pick_python() {
  local cand
  for cand in "$HERE/.venv/bin/python" python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import PySide6" >/dev/null 2>&1; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  echo "ERROR: no Python with PySide6 found. Run ./setup.sh first (creates .venv)." >&2
  exit 1
}

# Qt on a Wayland session: force the X11 (XWayland) backend — it is what the
# container's windows use too, and it avoids the wayland plugin's quirks.
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] && [ -z "${QT_QPA_PLATFORM:-}" ]; then
  export QT_QPA_PLATFORM=xcb
fi

exec "$PY" "$HERE/scanner_gui.py" "$@"
