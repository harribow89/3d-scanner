#!/usr/bin/env bash
# Emulator checks for the built image (QEMU "sabrelite" = Boundary's i.MX6Q
# SABRE Lite, the design the Matterport module derives from).
#
#   sensor_head/image/qemu_test.sh boot   IMG.img ROOTFS UBOOT_DIR [SECONDS]
#   sensor_head/image/qemu_test.sh script ROOTFS UBOOT_DIR
#
# boot   : boots the image's kernel + initrd straight from QEMU with the root
#          filesystem on the emulated SD card, root=PARTUUID like the real
#          boot script. QEMU drives no SD card-detect GPIO, so a test copy of
#          imx6q-sabrelite.dtb is made with `broken-cd` (needs U-Boot's dtc).
#          Expect: login prompt, xtion-head / ssh / avahi started, no FAILED.
# script : runs the compiled 6x_bootscript inside mainline Boundary U-Boot
#          (built with CONFIG_OF_EMBED=y, mx6qsabrelite_defconfig), preloaded
#          into RAM, once as factory U-Boot (dtype/disk/fs/cpu/board) and once
#          as distro boot (devtype/devnum/distro_bootpart/fdtfile). U-Boot's
#          SD controller does not work under QEMU, so the loads fail and the
#          script must reach its own error message: proves syntax and flow.
set -u
MODE=$1; shift
case "$MODE" in
  boot)
    IMG=$1 ROOT=$2 UB=$3 SECS=${4:-300}
    T=$(mktemp -d)
    "$UB/scripts/dtc/dtc" -q -I dtb -O dts -o "$T/s.dts" "$ROOT/boot/dtbs/imx6q-sabrelite.dtb"
    sed -i -E 's/^(\s*)cd-gpios = .*;/\1broken-cd;/; /^\s*wp-gpios = .*;/d' "$T/s.dts"
    "$UB/scripts/dtc/dtc" -q -I dts -O dtb -o "$T/s.dtb" "$T/s.dts"
    cp --sparse=always "$IMG" "$T/card.img"
    SIZE=$(stat -c %s "$T/card.img"); P=1; while [ $P -lt "$SIZE" ]; do P=$((P * 2)); done
    truncate -s "$P" "$T/card.img"             # QEMU wants power-of-two SD sizes
    KVER=$(ls "$ROOT/lib/modules" | sort -V | tail -n 1)
    timeout "$SECS" qemu-system-arm -M sabrelite -smp 4 -m 1G -display none \
      -serial null -serial "file:$T/console.log" \
      -kernel "$ROOT/boot/zImage" -initrd "$ROOT/boot/initrd.img-$KVER" -dtb "$T/s.dtb" \
      -append "console=ttymxc1,115200 root=PARTUUID=4d505232-02 rootwait rw consoleblank=0" \
      -drive if=none,id=card,format=raw,file="$T/card.img" -device sd-card,drive=card,bus=sd-bus \
      -nic user >/dev/null 2>&1
    sed 's/\x1b\[[0-9;]*m//g' "$T/console.log" | grep -a -E \
      "Booting Linux|mmcblk.*p2|EXT4-fs \(mmcblk.p2\): mounted|FAILED|xtion-head|ssh.service|avahi-daemon.service|login:|Kernel panic"
    echo "console log: $T/console.log"
    ;;
  script)
    ROOT=$1 UB=$2
    (sleep 6; printf ' \r'; sleep 2
     printf 'setenv dtype mmc; setenv disk 0; setenv cpu 6Q; setenv board matterport; setenv fs ext2\r'; sleep 2
     printf 'source 0x12000000\r'; sleep 8
     printf 'env default -a -f\r'; sleep 2
     printf 'setenv devtype mmc; setenv devnum 1; setenv distro_bootpart 1; setenv fdtfile imx6q-sabrelite.dtb\r'; sleep 2
     printf 'source 0x12000000\r'; sleep 8) | \
    timeout 50 qemu-system-arm -M sabrelite -smp 4 -m 1G -display none -serial null -serial stdio \
      -kernel "$UB/u-boot" -device loader,file="$ROOT/boot/6x_bootscript",addr=0x12000000,force-raw=on \
      -nic user 2>&1 | tr -d '\000' | grep -a -E "Executing script|Matterport sensor head|!!|Unknown command|syntax"
    ;;
  *) echo "usage: $0 boot IMG ROOTFS UBOOT_DIR [SECONDS] | script ROOTFS UBOOT_DIR" >&2; exit 2 ;;
esac
