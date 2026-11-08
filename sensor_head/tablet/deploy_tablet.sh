#!/usr/bin/env bash
# deploy_tablet.sh — put the sensor head + capture page on a rooted Android
# tablet that runs a Linux chroot (Kali NetHunter), over adb (USB, Wi-Fi or
# Tailscale), and drive it from this PC.
#
#   sensor_head/tablet/deploy_tablet.sh install   # copy the code, install deps in the chroot, write the config
#   sensor_head/tablet/deploy_tablet.sh start     # start the capture page on the tablet (http://127.0.0.1:8080 there)
#   sensor_head/tablet/deploy_tablet.sh stop|status|logs [N]
#   sensor_head/tablet/deploy_tablet.sh list      # PrimeSense sensors the tablet sees on USB
#   sensor_head/tablet/deploy_tablet.sh pull      # copy recordings here with adb (no receiver needed)
#   sensor_head/tablet/deploy_tablet.sh shell     # root shell inside the chroot
#
# Env:  TABLET   adb target: host:port (Tailscale/Wi-Fi) or a USB serial   (default 100.114.120.67:5555)
#       CHROOT   the chroot's root on the tablet            (default /data/local/nhsystem/kali-arm64)
#       PC_HOST  address the tablet should send to          (default: this PC's Tailscale IP)
#       PC_PORT  the receiver's port                        (default 5600)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SENSOR_HEAD="$(dirname "$HERE")"
ROOT="$(dirname "$SENSOR_HEAD")"
TABLET="${TABLET:-100.114.120.67:5555}"
CHROOT="${CHROOT:-/data/local/nhsystem/kali-arm64}"
PC_PORT="${PC_PORT:-5600}"
# Staging and pulling avoid /sdcard on purpose: that path goes through
# Android's FUSE storage layer, which can wedge (every access then hangs).
# /data/local/tmp is plain storage the adb shell user may write; the
# recordings' raw backing path is read as root.
STAGE_REMOTE=/data/local/tmp/xtion-head-stage
SCANS_RAW=/data/media/0/xtion-scans      # what the chroot's /sdcard/xtion-scans is on disk
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
CHROOT_ENV="env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=xterm"

# every adb call has a deadline: a wedged adb-over-TCP session otherwise hangs forever
# (seen over Tailscale; `adb kill-server && adb connect …` clears it)
adbx() { timeout "${ADB_TIMEOUT:-900}" adb -s "$TABLET" "$@"; }
# one command string for the tablet's root shell: no single quotes inside, please
su_sh() { adbx shell "su -c '$*'"; }
# a bash script (one string, any quoting) run inside the chroot as root. It
# travels over adb's stdin, so Android's shell never interprets it.
in_chroot() { printf '%s\n' "$1" | adbx shell "su -c '$CHROOT_ENV chroot $CHROOT /bin/bash -s'"; }

connect() {
  case "$TABLET" in *:*) adb connect "$TABLET" >/dev/null 2>&1 || true ;; esac
  if ! adbx get-state >/dev/null 2>&1; then
    echo "ERROR: adb cannot reach $TABLET." >&2
    echo "       Over the network: the tablet must have adb on tcp/5555 ('adb tcpip 5555' once over USB," >&2
    echo "       or Developer options -> Wireless debugging). Or plug it in and set TABLET=<serial>." >&2
    exit 3
  fi
  su_sh id | grep -q "uid=0" || { echo "ERROR: 'su' on the tablet did not give root (Magisk: allow Shell)." >&2; exit 3; }
  su_sh "test -d $CHROOT/etc" >/dev/null 2>&1 || { echo "ERROR: no Linux chroot at $CHROOT (set CHROOT=...)." >&2; exit 3; }
}

ensure_mounts() {
  # NetHunter mounts these when its own terminal boots the chroot; a plain
  # chroot from adb does not. The head needs /dev (USB) and /sys (port names).
  # Existing mounts are read from /proc/mounts: `mountpoint` misses a bind
  # mount that lives on the same filesystem as its parent (the chroot's
  # /sdcard is one), and the chroot's /sdcard must be the RAW storage path,
  # never Android's FUSE view, which is what wedged the tablet once.
  su_sh "for d in dev proc sys dev/pts; do grep -qs \" $CHROOT/\$d \" /proc/mounts || mount -o bind /\$d $CHROOT/\$d; done; \
         mkdir -p $CHROOT/sdcard; grep -qs \" $CHROOT/sdcard \" /proc/mounts || mount -o bind /data/media/0 $CHROOT/sdcard; \
         grep -q nameserver $CHROOT/etc/resolv.conf 2>/dev/null || printf \"nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n\" > $CHROOT/etc/resolv.conf"
}

install() {
  connect
  ensure_mounts
  local pc_host token
  STAGE=$(mktemp -d)
  trap 'rm -rf "$STAGE"' EXIT
  local stage="$STAGE"
  echo ">> staging sensor_head/ (without the board image)"
  rsync -a --exclude __pycache__ --exclude '*.pyc' --exclude image "$SENSOR_HEAD"/ "$stage"/
  pc_host="${PC_HOST:-$(tailscale ip -4 2>/dev/null | head -1 || true)}"
  token=$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); import pc_receiver as p; print(p.load_token(sys.argv[2])[0])" \
          "$SENSOR_HEAD" "$ROOT/output/recordings")
  if [ -n "$pc_host" ]; then
    printf '{"pc_url": "http://%s:%s", "token": "%s"}\n' "$pc_host" "$PC_PORT" "$token" > "$stage/tablet/xtion-tablet.generated.json"
    echo ">> the tablet will send recordings to http://$pc_host:$PC_PORT (run: ./run_scanner_docker.sh receive)"
  else
    printf '{"token": "%s"}\n' "$token" > "$stage/tablet/xtion-tablet.generated.json"
    echo ">> no Tailscale address found; set PC_HOST=<ip> or edit pc_url in /etc/xtion-tablet.json on the tablet"
  fi
  echo ">> copying to the tablet ($TABLET)"
  adbx shell "rm -rf $STAGE_REMOTE" >/dev/null 2>&1 || true
  adbx push "$stage" "$STAGE_REMOTE" >/dev/null
  su_sh "rm -rf $CHROOT/opt/xtion-head && mkdir -p $CHROOT/opt && cp -r $STAGE_REMOTE $CHROOT/opt/xtion-head && \
         chmod -R u+rwX,go+rX $CHROOT/opt/xtion-head && chmod 755 $CHROOT/opt/xtion-head/tablet/xtion-tablet $CHROOT/opt/xtion-head/tablet/install_chroot.sh && \
         rm -rf $STAGE_REMOTE"
  echo ">> installing inside the chroot (apt: OpenNI2, numpy, Pillow)"
  in_chroot "bash /opt/xtion-head/tablet/install_chroot.sh"
  echo
  echo "Done. Next:"
  echo "  on this PC:     ./run_scanner_docker.sh receive            # keep running while scanning"
  echo "  from this PC:   $0 start                                     # or in NetHunter's terminal: xtion-tablet start"
  echo "  on the tablet:  open http://127.0.0.1:8080 in Chrome -> menu -> Add to Home screen"
  echo "  plug the Xtion into the USB-C port (powered hub for more than one), tap Start cameras."
}

case "${1:-}" in
  install) install ;;
  ping)
    # does adb + root + the chroot work, and can the chroot see USB?
    connect; ensure_mounts
    echo "adb: $TABLET  model $(adbx shell getprop ro.product.model | tr -d '\r')  Android $(adbx shell getprop ro.build.version.release | tr -d '\r')"
    in_chroot '
      echo "chroot: $(head -1 /etc/os-release 2>/dev/null) $(uname -m)"
      echo "usb devices visible in chroot: $(ls /sys/bus/usb/devices 2>/dev/null | wc -l)   /dev/bus entries: $(ls /dev/bus 2>/dev/null | wc -l)"
      echo "PrimeSense sensors on USB: $(grep -ls 1d27 /sys/bus/usb/devices/*/idVendor 2>/dev/null | wc -l)"
      echo "dns: $(grep -c nameserver /etc/resolv.conf 2>/dev/null || echo 0) nameserver line(s)"
      if [ -x /usr/local/bin/xtion-tablet ]; then echo "installed: yes"; else echo "installed: no"; fi
    '
    ;;
  start)   connect; ensure_mounts; in_chroot "xtion-tablet start" ;;
  stop)    connect; in_chroot "xtion-tablet stop" ;;
  status)  connect; in_chroot "xtion-tablet status" ;;
  logs)    connect; in_chroot "xtion-tablet logs ${2:-40}" ;;
  list)    connect; ensure_mounts; in_chroot "xtion-tablet list" ;;
  shell)   connect; ensure_mounts; adbx shell -t "su -c '$CHROOT_ENV chroot $CHROOT /bin/bash -l'" ;;
  pull)
    connect
    dest="$ROOT/output/recordings"; mkdir -p "$dest"
    names=$(su_sh "ls $SCANS_RAW 2>/dev/null" | tr -d '\r')
    [ -n "$names" ] || { echo "no recordings on the tablet ($SCANS_RAW)"; exit 0; }
    for name in $names; do
      case "$name" in .*) continue ;; esac
      if [ -d "$dest/$name" ]; then echo "   $name: already here"; continue; fi
      echo "   pulling $name"
      # a tar stream from root over adb: no FUSE, no permission trouble, one round trip
      adbx exec-out "su -c 'tar -cf - -C $SCANS_RAW $name'" | tar -xf - -C "$dest" \
        && [ -f "$dest/$name/meta.json" ] || { echo "   $name: pull FAILED"; rm -rf "${dest:?}/$name"; }
    done
    echo "recordings in $dest"
    ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
