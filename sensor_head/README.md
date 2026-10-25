# Sensor head: three Xtions as one network device

The three PrimeSense sensors from the Matterport Pro2 run as a **sensor head**:
one service (`xtion_head.py`) streams all of them over TCP, and the PC runs
mapping and calibration. The head runs either on the Pro2's own i.MX6 board
(SD-card image below) or on this PC.

```
 cameras ──USB──> xtion_head.py ──tcp/5599──> head_bridge.py (ROS 2, in Docker) ──> RTAB-Map, calibration, sweeps
                  (board or PC)                calibrate_head.py / head_probe.py (no ROS)
```

## Use it from the PC

GUI: **Multi-Camera** tab → *Sensor head*. Type the board's address
(`matterport-head.local`), or press **Run head on this PC** when the cameras
are plugged in here. Then **Check head**, **Calibrate cameras**, and the
live / sweep buttons all go through the head.

Terminal:

```
./run_scanner_docker.sh head                          # head on this PC
SCANNER_HEAD=127.0.0.1 ./run_scanner_docker.sh multi   # any multi-camera mode via a head
.venv/bin/python sensor_head/head_probe.py 127.0.0.1   # rates per camera, exit 0 = healthy
.venv/bin/python calibrate_head.py --head 127.0.0.1    # extrinsics -> output/camera_extrinsics.json
```

## What makes three cameras reliable

| Problem seen | Fix |
|---|---|
| Hub resets, cameras fail to re-enumerate | Each camera draws up to 500 mA. Use one port per camera, or a hub with its own power supply. |
| Camera drops off USB right after plugging in | The sensor's microphones: snd-usb-audio probing them crashes its firmware. The head and the launcher detach those interfaces. The image ships `56-xtion-no-audio.rules`. |
| "Failed to set USB interface" with several cameras on one link | Isochronous bandwidth reservation. The head uses **bulk** transfers (`--usb bulk`, default; verified: alt setting 0, 30 fps per camera). |
| Cameras renamed after a re-plug | Cameras are named by USB port in natural order (`1-7 < 1-9 < 1-10`), or by `cameras.json`. |
| One camera stalls | A per-camera watchdog reopens it after 4 s without frames. |
| Stop left cameras running | Background jobs ignore SIGINT; every stop now sends SIGTERM. |

Measured on this PC (one port per camera): all three at 30 fps in the
cameras, 15 fps of depth and colour delivered per camera (17 MB/s), 0 restarts.

## Calibration

`calibrate_head.py` matches SIFT features between neighbouring cameras'
colour images, lifts them to 3-D with each camera's depth, fits the rigid
transform with RANSAC, then refines it with ICP on just the overlapping
points. Shape-only matching (`calibrate_multi.py`) scored 0.05–0.31 on this
fan, because neighbouring views only partly overlap.

On the current rig it found a fan with 40° steps, camera1 mounted upside
down. Seam check: camera1/camera2 median depth disagreement 6 mm (83 % of
points within 2 cm). camera2/camera3 disagreed by 20 mm at about 3 m, which
is the Xtion's own noise at that range.

Use 640×480 when calibrating (`./run_scanner_docker.sh head --width 640 --height 480`),
keep the rig still, and point it at a cluttered, textured scene 1–2 m away.
The tool keeps an earlier good calibration unless every camera links.

## The i.MX6 image

Build it with `sudo sensor_head/image/build_image.sh`. The output goes to
`/media/jim/storage1/imx6-sensor-head/matterport-head-YYYYMMDD.img.xz`.

What is on it:

- **System:** Debian 12 armhf and the `linux-image-armmp` kernel, which includes the
  Boundary Nitrogen6x, SOM2 and SABRE Lite device trees.
- **Camera service:** `xtion-head.service` runs as root on tcp/5599. Logs go to
  `journalctl -u xtion-head`.
- **Settings:** `/boot/head-config.txt` holds the hostname, Wi-Fi and camera
  arguments. You can edit it on the card from a PC.
- **Network:**
  - DHCP on Ethernet and Wi-Fi, with the mDNS name `matterport-head.local`
  - a USB cable to the OTG port gives the board 192.168.7.2
- **Login:** `scanner` / `scanner` over SSH or the serial console
  (ttymxc1, 115200 8N1). Change the password.
- **First boot:** the root partition grows to fill the card and SSH host keys
  are created.

### Boot path

Boundary's U-Boot lives in the module's SPI flash and looks for
`6x_bootscript` on partition 1 of `sata 0`, `mmc 0` and `mmc 1`. Partition 1
of the image is a small ext2 `/boot` holding:

- `6x_bootscript`, plus `boot.scr` for mainline distro boot
- `zImage` and `uImage`
- `uInitrd`
- `dtbs/`

The script supports both old and new Boundary U-Boot builds:

- **Load command:** it uses `${fs}load`, so builds with only `ext2load` work.
- **Kernel format:** it tries `bootz` first and falls back to `bootm` with the `uImage`.
- **Device tree:** it picks the tree from U-Boot's `cpu` and `board` variables,
  then falls back to `nitrogen6x`, `nitrogen6_som2`, `nitrogen6_max` and `sabrelite`.

The root partition is found by PARTUUID `4d505232-02`. The board's own eMMC
(the Matterport system) is never touched.

### Flash and first boot

```
xz -dc matterport-head-*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
```

1. Put the card in the board's microSD slot (J18). Plug the three sensors in.
   Connect the serial adapter (3.3 V) to watch the boot.
2. If the Matterport system still boots from eMMC, stop U-Boot at its
   prompt and type `run bootcmd`. If the board does not search SD by itself,
   load the script by hand:
   `ext2load mmc 0 0x10008000 /6x_bootscript ; source 0x10008000`
   (use `mmc 1` if the slot is the second controller).
3. If no device tree matches the carrier, the script prints the board name.
   Pick a tree at the prompt and boot:
   `setenv dtbname imx6q-nitrogen6x.dtb ; boot`
4. On the PC, run `SCANNER_HEAD=matterport-head.local` with any multi-camera
   mode, or use the GUI field.

### What was verified, and how

These checks were run on 2026-10-26 with `sensor_head/image/qemu_test.sh`.
QEMU's `sabrelite` machine emulates Boundary's i.MX6Q SABRE Lite.

**Operating system boot (`boot`):** the image's kernel and ramdisk booted,
and the root filesystem was found by PARTUUID on the emulated SD card. The
following then came up with no failed units:

- hostname `matterport-head` and the `/boot` partition
- networkd, avahi, timesyncd and resolved
- the first-boot service, which grew the partition and made SSH keys
- ssh and `xtion-head`
- a login prompt on ttymxc1

**Boot script (`script`):** the compiled `6x_bootscript` ran inside mainline
Boundary U-Boot, both as the factory loader runs it and as distro boot runs
it. It produced no syntax errors and walked through its device-tree
fallbacks. Mainline U-Boot reports `board=nitrogen6x`, which matches a
device tree in the image.

**Cameras with the image's ARM software:** I ran the image's armhf Python,
numpy, primesense and OpenNI2 under qemu-user against the three real
sensors. They loaded, listed all three cameras by port, then opened and
configured them. Streaming stalled even with one camera: the emulator's USB
passthrough drops the bulk transfers. The same code streams at 30 fps on
this PC, so streaming on ARM is unproven until it runs on the board.

### Not verified, because it needs the real board

- whether the factory U-Boot is locked, and which device tree matches the
  Matterport carrier (the script prints the board name if none matches)
- camera streaming on the i.MX6 itself: USB bandwidth with three sensors
  behind the board's hub, and CPU headroom at 15 fps × 3 × 2 streams
  (lower it with `HEAD_ARGS=--fps 10` or `--no-color` if needed)
- whether the TiWi-BLE Wi-Fi comes up, and whether the board powers three sensors
