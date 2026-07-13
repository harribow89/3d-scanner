# Creality CR-Scan Ferret / Ferret SE on Linux

Short answer up front: **the Ferret does not work with this ROS 3D-scanner
project, and it has no official Linux support.** This folder documents the
realistic options and provides a best-effort setup for running Creality's own
software under Wine.

---

## 1. Why the Ferret can't drive this project

This project is built entirely around **OpenNI2 RGB-D cameras** — the ASUS
Xtion / PrimeSense (PS1080) family. Every camera path launches
`openni2_camera`, which publishes a standard depth stream into ROS
(`/camera/depth_registered/points`) that RTAB-Map and the tools consume.

The **CR-Scan Ferret SE is a fundamentally different device:**

| | This project expects | Ferret SE is |
|---|---|---|
| Sensor type | OpenNI2 RGB-D (PrimeSense) | Proprietary NIR structured-light |
| Driver | `openni2_camera` (ROS) | Creality Scan (closed source) |
| USB identity | PrimeSense / Orbbec vendor | Creality vendor `0xbcdc` |
| Linux support | Yes (OpenNI2) | **None official** (Win/Mac/Android only) |
| Output | Live depth stream → SLAM | Finished mesh/point cloud from Creality Scan |

The Ferret will **never enumerate as an OpenNI2 device**, so `openni2_camera`
and `NiViewer` will not see it, and nothing in this repo can capture from it.
Adapting the project to the Ferret would require reverse-engineering Creality's
proprietary USB protocol — nobody has published that.

**If you want the live AI-assisted SLAM workflow this repo is built for, use an
OpenNI2 camera** (ASUS Xtion PRO Live, Orbbec Astra, or PrimeSense Carmine).
Those plug in and work with the existing pipeline.

---

## 2. Option 1 — run Creality Scan under Wine (this folder)

This is a **community workaround, not a supported or guaranteed path.** Creality
Scan is Windows software; vanilla Wine generally cannot pass the scanner's
USB/camera stream through, so a **patched Wine build** is usually required.

### Files here

- `99-creality-ferret.rules` — udev rule granting non-root USB access to the
  scanner (vendor `bcdc`).
- `setup_ferret_linux.sh` — installs the udev rule, adds you to `plugdev`,
  checks whether the scanner is detected, and creates a Wine prefix with common
  runtime deps.

### Quick start (on your Ubuntu machine, not in a container)

```bash
# 1. Detect the scanner and see how it enumerates
./ferret_linux/setup_ferret_linux.sh --check

# 2. Run the full setup (installs udev rule, wine prefix, deps — will use sudo)
./ferret_linux/setup_ferret_linux.sh
```

Then unplug/replug the scanner and log out/in once (for the `plugdev` group).

### The patched Wine (the hard, experimental part)

Vanilla Wine lacks the USB passthrough + DirectShow/camera-control plumbing the
scanner needs. The known community patch set lives at
[poita66/wine](https://github.com/poita66/wine) (branch `test/all-prs`) and adds:
a libusb-win32 compatibility driver, USB device identity in DirectShow, a V4L2
`IAMCameraControl`, UVC extension-unit passthrough (`IKsControl`), and
`MFEnumDeviceSources` for video.

> ⚠️ **Caveat:** that patch set was developed and verified for the **CR-Scan
> Lizard's JM Studio**, *not* for the Ferret's **Creality Scan**. There is no
> confirmed, turnkey report of the Ferret working through it. Treat this as
> experimental — it may need extra work or may not work at all for your unit.

Rough build/run outline (see the Wine wiki for build deps):

```bash
sudo apt install -y git gcc-multilib libusb-1.0-0-dev   # + Wine build deps
git clone https://github.com/poita66/wine.git
cd wine && git checkout test/all-prs
mkdir build64 && cd build64
../configure --enable-archs=i386,x86_64
make -j"$(nproc)"

export WINEPREFIX=~/.wine-creality
# Download Creality Scan (Windows) from the Ferret SE download page first:
./wine ~/Downloads/CrealityScan*.exe            # install
./wine "C:\\Program Files\\Creality Scan\\CrealityScan.exe"   # launch
```

Download Creality Scan for the Ferret SE:
<https://www.creality.com/download/cr-scan-ferret-se>

---

## 3. Officially-supported fallbacks (more likely to just work)

If the Wine route stalls, these are supported by Creality and generally more
reliable:

1. **Android via USB (officially supported).** The Ferret works with the
   Creality Scan Android app over a direct USB-C connection. If you have an
   Android phone/tablet, this is the least-effort way to get real scans.
2. **Windows VM with USB passthrough.** Run Creality Scan in a Windows guest
   (VirtualBox/VMware/GNOME Boxes/KVM) and pass the `bcdc` USB device through.
   Needs a machine with decent USB 3.0 passthrough; a GPU helps for the live
   preview.
3. **Dual-boot / a spare Windows PC.** The most reliable option, since Creality
   Scan is built for Windows.

---

## 4. Using Ferret scans with this project

However you capture — Wine, Android, or a VM — Creality Scan exports meshes and
point clouds (`.ply` / `.obj`). You can still use this project's **post-
processing** tools on the exported `.ply`:

```bash
# View a Ferret export
python3 view_cloud.py /path/to/ferret_scan.ply

# Clean / isolate it with the project's pipeline
python3 point_cloud_tools.py /path/to/ferret_scan.ply
```

That's the one place the Ferret and this repo meaningfully connect: this project
consumes the *finished* Ferret cloud for cleanup/isolation, it just can't *drive*
the scanner.

---

## Sources

- [CR-Scan Ferret SE — Creality Official](https://www.creality.com/products/cr-scan-ferret-se)
- [Creality Scan with Linux? — Creality Community Forum](https://forum.creality.com/t/creality-scan-with-linux/35959)
- [Running Creality 3D Scanners on Linux with Wine (poita66 gist)](https://gist.github.com/poita66/cab19abacae3011454f1183d0e85aaeb)
- [poita66/wine — patched Wine branch](https://github.com/poita66/wine)
