#!/usr/bin/env bash
#
# setup_ferret_linux.sh — best-effort setup for the Creality CR-Scan Ferret /
# Ferret SE on Ubuntu/Linux.
#
# IMPORTANT: The Ferret is a proprietary NIR structured-light scanner (USB
# vendor 0xbcdc). It is NOT an OpenNI2/RGB-D camera and does NOT work with this
# ROS/RTAB-Map scanner project. This script only sets up Creality's own
# "Creality Scan" software under Wine (option 1 in ferret_linux/README.md),
# which is a community workaround, not an officially supported path.
#
# What this script DOES reliably:
#   - installs a udev rule so a non-root user can access the scanner (vendor bcdc)
#   - adds you to the `plugdev` group
#   - checks whether the scanner is currently detected
#   - creates a dedicated Wine prefix and installs common runtime deps
#
# What it CANNOT guarantee:
#   - that Creality Scan actually streams from the Ferret under Wine. Vanilla
#     Wine lacks the USB/DirectShow camera plumbing; you likely need the patched
#     Wine build referenced in the README. This script points you there but does
#     not build Wine for you (that is a long, machine-specific compile).
#
# Usage:
#   ./ferret_linux/setup_ferret_linux.sh            # run the setup steps
#   ./ferret_linux/setup_ferret_linux.sh --check    # just detect the scanner
#
set -euo pipefail

FERRET_VID="bcdc"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE_SRC="${SCRIPT_DIR}/99-creality-ferret.rules"
RULE_DST="/etc/udev/rules.d/99-creality-ferret.rules"
WINEPREFIX_DIR="${WINEPREFIX:-$HOME/.wine-creality}"

c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
hr()       { printf '%s\n' "------------------------------------------------------------"; }

detect_scanner() {
  hr; echo "Looking for the Creality scanner (USB vendor ${FERRET_VID})..."
  if ! command -v lsusb >/dev/null 2>&1; then
    c_yellow "lsusb not installed — run: sudo apt install usbutils"
    return 2
  fi
  if lsusb | grep -qi "${FERRET_VID}:"; then
    c_green "Found it:"
    lsusb | grep -i "${FERRET_VID}:"
    return 0
  fi
  c_yellow "No device with vendor ${FERRET_VID} found."
  echo "  - Make sure the Ferret is plugged into a USB 3.0 (blue) port."
  echo "  - Use the supplied Y-cable so it gets enough power."
  echo "  - Try 'lsusb' before and after plugging in to spot the new device."
  return 1
}

install_udev_rule() {
  hr; echo "Installing udev rule -> ${RULE_DST}"
  if [[ ! -f "${RULE_SRC}" ]]; then
    c_red "Missing ${RULE_SRC}"; exit 1
  fi
  sudo cp "${RULE_SRC}" "${RULE_DST}"
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  c_green "udev rule installed and reloaded."

  if ! getent group plugdev >/dev/null 2>&1; then
    sudo groupadd plugdev || true
  fi
  if id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
    echo "You are already in the 'plugdev' group."
  else
    sudo usermod -aG plugdev "$USER"
    c_yellow "Added $USER to 'plugdev'. Log out and back in for it to take effect."
  fi
  c_yellow "Unplug and replug the scanner after this step."
}

install_wine_deps() {
  hr; echo "Setting up a Wine prefix for Creality Scan at: ${WINEPREFIX_DIR}"
  if ! command -v wine >/dev/null 2>&1; then
    c_yellow "wine is not installed. Install it first, e.g.:"
    echo "    sudo dpkg --add-architecture i386"
    echo "    sudo apt update && sudo apt install -y wine64 wine32 winetricks"
    echo "  NOTE: vanilla Wine usually cannot pass the scanner's USB/camera"
    echo "  stream through. See ferret_linux/README.md for the patched Wine build."
    return 1
  fi
  export WINEPREFIX="${WINEPREFIX_DIR}"
  export WINEARCH=win64
  wineboot --init || true
  if command -v winetricks >/dev/null 2>&1; then
    echo "Installing common runtime deps (vcrun, corefonts)..."
    winetricks -q vcrun2019 corefonts || c_yellow "winetricks step failed — continue manually."
  else
    c_yellow "winetricks not found; skipping runtime deps. Install with: sudo apt install winetricks"
  fi
  c_green "Wine prefix ready at ${WINEPREFIX_DIR}"
  echo "Next: download 'Creality Scan' for Windows from"
  echo "  https://www.creality.com/download/cr-scan-ferret-se"
  echo "then install it into this prefix with:"
  echo "  WINEPREFIX=${WINEPREFIX_DIR} wine ~/Downloads/CrealityScan*.exe"
}

main() {
  if [[ "${1:-}" == "--check" ]]; then
    detect_scanner || true
    exit 0
  fi

  cat <<'BANNER'
============================================================
 Creality CR-Scan Ferret / Ferret SE — Linux setup
============================================================
 This sets up Creality Scan under Wine (option 1). The Ferret
 does NOT work with the ROS scanner in this repo. Read
 ferret_linux/README.md for the full picture and caveats.
============================================================
BANNER

  install_udev_rule
  detect_scanner || true
  install_wine_deps || true

  hr
  c_green "Setup steps complete."
  echo "See ferret_linux/README.md for: the patched Wine build, downloading"
  echo "Creality Scan, officially-supported fallbacks (Android USB, Windows VM),"
  echo "and how to post-process exported .ply meshes with this project."
}

main "$@"
