#!/bin/bash
# Launch the Scanner GUI with the proper environment
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv/bin"

# Run from the project dir: scanner_gui.py shells out to ./.venv/bin/python and
# ./run_scanner_docker.sh with paths relative to HERE.
cd "$HERE"

# Activate venv and launch GUI
. "$VENV/activate"
exec python "$HERE/scanner_gui.py" "$@"
