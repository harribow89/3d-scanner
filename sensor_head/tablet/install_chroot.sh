#!/bin/bash
# install_chroot.sh — run INSIDE the tablet's Linux chroot (Kali NetHunter;
# any Debian/Ubuntu arm64 works) as root. Installs what xtion_head.py and the
# tablet UI need, with the files already copied to /opt/xtion-head by
# deploy_tablet.sh. Safe to re-run.
set -eu
export DEBIAN_FRONTEND=noninteractive
APP=/opt/xtion-head

echo ">> apt packages (OpenNI2 runtime + Python deps)"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  libopenni2-0 openni2-utils python3 python3-numpy python3-pil \
  ca-certificates usbutils procps
# the OpenNI2 Python binding is vendored (sensor_head/vendor/primesense): no pip needed

echo ">> launcher + config"
install -m 755 "$APP/tablet/xtion-tablet" /usr/local/bin/xtion-tablet
[ -f /etc/xtion-head.json ] || echo '{}' > /etc/xtion-head.json
if [ -f "$APP/tablet/xtion-tablet.generated.json" ]; then
  # keep local edits; refresh only the PC address + token the deploy script generated
  python3 "$APP/tablet/merge_config.py" /etc/xtion-tablet.json "$APP/tablet/xtion-tablet.generated.json"
elif [ ! -f /etc/xtion-tablet.json ]; then
  install -m 644 "$APP/tablet/xtion-tablet.example.json" /etc/xtion-tablet.json
fi
mkdir -p "$(python3 -c "import json;print(json.load(open('/etc/xtion-tablet.json')).get('recordings','/sdcard/xtion-scans'))")" 2>/dev/null || true

echo ">> check"
python3 -c "import numpy, PIL; print('   python deps OK')"
ls /usr/lib/*/libOpenNI2.so.0 >/dev/null 2>&1 && echo "   libOpenNI2 OK" || { echo "   libOpenNI2.so.0 MISSING"; exit 1; }
ls /usr/lib/*/OpenNI2/Drivers/libPS1080.so* >/dev/null 2>&1 && echo "   PS1080 (PrimeSense/Xtion) driver OK" || echo "   WARNING: PS1080 driver not found"
python3 "$APP/xtion_head.py" --list >/dev/null 2>&1 && echo "   OpenNI2 loads from Python OK" || { echo "   ERROR: xtion_head.py --list failed:"; python3 "$APP/xtion_head.py" --list 2>&1 | tail -3; exit 1; }
if [ -d /sys/bus/usb/devices ]; then
  n=$(grep -ls '^1d27$' /sys/bus/usb/devices/*/idVendor 2>/dev/null | wc -l)
  echo "   $n PrimeSense sensor(s) on USB right now"
else
  echo "   /sys/bus/usb not visible in this chroot — the head needs /sys and /dev mounted"
fi
echo
echo "Installed. Start the capture page with:   xtion-tablet start"
echo "then open  http://127.0.0.1:8080  in the tablet's browser (menu -> Add to Home screen)."
