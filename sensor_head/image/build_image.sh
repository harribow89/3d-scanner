#!/usr/bin/env bash
# Build the SD-card image for the Matterport Pro2 i.MX6 board (Boundary Devices
# module): Debian 12 armhf + the Xtion network sensor head as a service.
#
#   sudo sensor_head/image/build_image.sh [WORKDIR]      (default /media/jim/storage1/imx6-sensor-head)
#
# Output: WORKDIR/matterport-head-YYYYMMDD.img.xz  — write it with
#   xz -dc matterport-head-*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
#
# Needs on the build PC: debootstrap, qemu-user-static (binfmt), parted,
# e2fsprogs, xz. Runs as root (loop devices, chroot). Safe to re-run: the
# debootstrap stage is skipped when WORKDIR/rootfs already exists.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SENSOR_HEAD="$(dirname "$HERE")"
WORK="${1:-/media/jim/storage1/imx6-sensor-head}"
ROOT="$WORK/rootfs"
STAMP="$(date +%Y%m%d)"
IMG="$WORK/matterport-head-$STAMP.img"
IMG_MB=${IMG_MB:-2048}
BOOT_MB=384
MIRROR=${MIRROR:-http://deb.debian.org/debian}
# fixed MBR disk id -> stable PARTUUIDs (<id>-01 boot, <id>-02 root)
DISKID=${DISKID:-4d505232}          # "MPR2"
ROOT_PARTUUID="${DISKID}-02"
BOOT_PARTUUID="${DISKID}-01"

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
echo "== work dir: $WORK"
mkdir -p "$WORK"

# ------------------------------------------------------------------ 1. base
if [ ! -x "$ROOT/bin/bash" ]; then
  echo "== debootstrap bookworm armhf"
  debootstrap --arch=armhf --variant=minbase \
    --include=systemd,systemd-sysv,udev,dbus,ca-certificates,apt bookworm "$ROOT" "$MIRROR"
fi

MOUNTS=()
cleanup() {
  for ((i=${#MOUNTS[@]}-1; i>=0; i--)); do umount -l "${MOUNTS[$i]}" 2>/dev/null || true; done
  [ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null || true
}
trap cleanup EXIT
bindm() { mount --bind "$1" "$2"; MOUNTS+=("$2"); }
bindm /proc "$ROOT/proc"; bindm /sys "$ROOT/sys"; bindm /dev "$ROOT/dev"; bindm /dev/pts "$ROOT/dev/pts"
rm -f "$ROOT/etc/resolv.conf"; cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
in_root() { LC_ALL=C DEBIAN_FRONTEND=noninteractive chroot "$ROOT" "$@"; }

# ------------------------------------------------------------- 2. packages
cat > "$ROOT/etc/apt/sources.list" <<EOF
deb $MIRROR bookworm main contrib non-free-firmware
deb $MIRROR bookworm-updates main contrib non-free-firmware
deb http://security.debian.org/debian-security bookworm-security main contrib non-free-firmware
EOF
# no services may start inside the chroot
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"; chmod +x "$ROOT/usr/sbin/policy-rc.d"

# initramfs must carry the i.MX SD/eMMC drivers; "most" does
mkdir -p "$ROOT/etc/initramfs-tools/conf.d"
echo "MODULES=most" > "$ROOT/etc/initramfs-tools/conf.d/modules"

echo "== apt: kernel + userland"
in_root apt-get update
in_root apt-get install -y --no-install-recommends \
  linux-image-armmp u-boot-tools initramfs-tools \
  systemd-timesyncd systemd-resolved iproute2 iputils-ping ifupdown- \
  openssh-server sudo avahi-daemon libnss-mdns \
  wpasupplicant iw firmware-ti-connectivity wireless-regdb \
  libopenni2-0 openni2-utils usbutils \
  python3 python3-numpy python3-pip python3-pil \
  cloud-guest-utils e2fsprogs parted kmod \
  nano less htop curl locales tzdata
# installing systemd-resolved turns /etc/resolv.conf into a link that only
# works on a booted system; give the chroot working DNS again
rm -f "$ROOT/etc/resolv.conf"; cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
# primesense is pure Python: fetch it on the build PC, install from the file
mkdir -p "$WORK/wheels"
if ! ls "$WORK/wheels"/primesense-* >/dev/null 2>&1; then
  python3 -m pip download --no-deps --dest "$WORK/wheels" primesense==2.2.0.30.post5
fi
mkdir -p "$ROOT/tmp/wheels"; cp "$WORK/wheels"/primesense-* "$ROOT/tmp/wheels/"
in_root sh -c 'pip3 install --break-system-packages --no-cache-dir --no-index --no-build-isolation /tmp/wheels/primesense-*'
rm -rf "$ROOT/tmp/wheels"

# --------------------------------------------------------- 3. our software
echo "== overlay + sensor head"
cp -a "$HERE/overlay/." "$ROOT/"
chmod +x "$ROOT/usr/local/sbin/"* "$ROOT/etc/kernel/postinst.d/zz-matterport-boot" \
         "$ROOT/etc/initramfs/post-update.d/zz-matterport-boot"
mkdir -p "$ROOT/opt/xtion-head"
cp "$SENSOR_HEAD/xtion_head.py" "$SENSOR_HEAD/head_probe.py" "$ROOT/opt/xtion-head/"
cp "$HERE/README-board.txt" "$ROOT/opt/xtion-head/README.txt"
cp "$SENSOR_HEAD/../udev/56-xtion-no-audio.rules" "$ROOT/etc/udev/rules.d/"
cp "$HERE/README-board.txt" "$ROOT/etc/motd"

echo "matterport-head" > "$ROOT/etc/hostname"
cat > "$ROOT/etc/hosts" <<'EOF'
127.0.0.1	localhost
127.0.1.1	matterport-head
::1		localhost ip6-localhost ip6-loopback
EOF
cat > "$ROOT/etc/fstab" <<EOF
PARTUUID=$ROOT_PARTUUID  /      ext4  defaults,noatime,commit=30  0 1
PARTUUID=$BOOT_PARTUUID  /boot  ext2  defaults,noatime            0 2
tmpfs                    /tmp   tmpfs defaults,nosuid,size=64M    0 0
EOF

# user 'scanner' / password 'scanner' (change it: passwd), sudo, SSH
if ! in_root id scanner >/dev/null 2>&1; then
  in_root useradd -m -s /bin/bash -G sudo,plugdev,video,dialout,netdev scanner || \
    in_root useradd -m -s /bin/bash -G sudo,plugdev,video,dialout scanner
fi
echo "scanner:scanner" | in_root chpasswd
in_root passwd -l root
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' "$ROOT/etc/ssh/sshd_config"
rm -f "$ROOT"/etc/ssh/ssh_host_*            # each card makes its own on first boot
: > "$ROOT/etc/machine-id"                     # fresh machine-id per card

sed -i 's/^# *en_AU.UTF-8/en_AU.UTF-8/; s/^# *en_US.UTF-8/en_US.UTF-8/' "$ROOT/etc/locale.gen"
in_root locale-gen >/dev/null
echo "LANG=en_AU.UTF-8" > "$ROOT/etc/default/locale"

in_root systemctl enable systemd-networkd systemd-resolved systemd-timesyncd ssh avahi-daemon \
  head-apply-config head-firstboot xtion-head usb-gadget-net
in_root systemctl enable serial-getty@ttymxc1.service
ln -sf /run/systemd/resolve/stub-resolv.conf "$ROOT/etc/resolv.conf.final"

# ------------------------------------------------------ 4. boot partition
echo "== boot files"
KVER=$(ls "$ROOT/lib/modules" | sort -V | tail -n 1)
in_root update-initramfs -c -k "$KVER" 2>/dev/null || in_root update-initramfs -u -k "$KVER"
in_root /etc/kernel/postinst.d/zz-matterport-boot "$KVER"
sed "s/@ROOT_PARTUUID@/$ROOT_PARTUUID/" "$HERE/6x_bootscript.txt" > "$ROOT/boot/6x_bootscript.txt"
in_root mkimage -A arm -O linux -T script -C none -n "matterport head" \
  -d /boot/6x_bootscript.txt /boot/6x_bootscript >/dev/null
cp "$ROOT/boot/6x_bootscript" "$ROOT/boot/boot.scr"
ls -la "$ROOT/boot" "$ROOT/boot/dtbs" | sed 's/^/   /'

rm -f "$ROOT/usr/sbin/policy-rc.d"
in_root apt-get clean
rm -rf "$ROOT/var/lib/apt/lists/"*
mv "$ROOT/etc/resolv.conf.final" "$ROOT/etc/resolv.conf"
cleanup; MOUNTS=()

# ------------------------------------------------------------ 5. the image
echo "== image $IMG (${IMG_MB} MB)"
rm -f "$IMG" "$IMG.xz"
truncate -s "${IMG_MB}M" "$IMG"
parted -s "$IMG" mklabel msdos \
  mkpart primary ext2 4MiB "$((4 + BOOT_MB))MiB" \
  mkpart primary ext4 "$((4 + BOOT_MB))MiB" 100% \
  set 1 boot on
# fixed disk signature (MBR offset 440) -> fixed PARTUUIDs
printf "\\x${DISKID:6:2}\\x${DISKID:4:2}\\x${DISKID:2:2}\\x${DISKID:0:2}" | \
  dd of="$IMG" bs=1 seek=440 conv=notrunc status=none
LOOP=$(losetup -f --show -P "$IMG")
# ext2 with 128-byte inodes: the oldest U-Boot ext2 readers handle it
mkfs.ext2 -q -I 128 -L BOOT "${LOOP}p1"
mkfs.ext4 -q -L rootfs -O ^metadata_csum_seed "${LOOP}p2"
MNT="$WORK/mnt"; mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"; MOUNTS+=("$MNT")
mkdir -p "$MNT/boot"
mount "${LOOP}p1" "$MNT/boot"; MOUNTS+=("$MNT/boot")
cp -a "$ROOT/." "$MNT/"
sync
blkid "${LOOP}p1" "${LOOP}p2" | sed 's/^/   /'
cleanup; MOUNTS=(); LOOP=""

echo "== compress"
xz -T0 -6 -k "$IMG"
ls -la "$IMG" "$IMG.xz"
sha256sum "$IMG.xz" | tee "$IMG.xz.sha256"
echo "DONE: $IMG.xz"
