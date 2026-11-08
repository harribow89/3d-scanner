# Capture on the tablet, process on the PC

The scanner's cameras do not need the PC next to them. A rooted Android
tablet with a Linux chroot (here: the Lenovo Legion Tab, Snapdragon 8 Gen 3,
Kali NetHunter) runs the same **sensor head** the i.MX6 board runs, records
what the cameras see, and sends the recording to the PC over Tailscale or
Wi-Fi. The PC then replays the recording into the unchanged pipeline:
RTAB-Map, calibration, station sweeps.

```
 tablet (Linux chroot)                                    PC (this repo)
 ┌────────────────────────────────────────────┐           ┌──────────────────────────────────────┐
 │ Xtion(s) ─USB-C/OTG─> xtion_head.py        │  tailnet  │ pc_receiver.py  -> output/recordings/│
 │                        │ tcp/5599          │ ────────> │        │                             │
 │ tablet_ui.py  ──────> head_record.py ──> /sdcard/xtion-scans/<rec>/   │ head_replay.py -> tcp/5599 -> head_bridge (ROS) │
 │ (touch page, :8080)      sweep | continuous│           │        │       SCANNER_HEAD=127.0.0.1 any mode     │
 └────────────────────────────────────────────┘           │ station_scan.py import <rec> -> sweep (no Docker) │
                                                          └──────────────────────────────────────┘
```

Why not stream live? Over Tailscale the link is often a relay or a phone
hotspot (we measured 70–280 ms round trips). Three cameras at 15 fps are
17 MB/s. Recording locally and sending afterwards keeps capture quality
independent of the network; a sweep is a few hundred KB, a minute of
continuous capture a few tens of MB.

## What you need

- A rooted Android tablet with a Linux chroot that has `/dev` and `/sys`
  available (Kali NetHunter does), `adb` reachable from the PC (USB, or
  tcp/5555 over Tailscale/Wi-Fi), and Magisk root granted to Shell. The
  chroot must be Debian-family arm64: `libopenni2-0` comes from apt.
- The cameras on the tablet's USB-C port. One Xtion draws up to 500 mA, so a
  single camera can run straight off the port, but **use a powered USB-C hub
  for more than one** (and to keep the tablet charging). The sensors'
  microphones must be kept away from the kernel's USB-audio driver; the head
  does that itself when it runs as root (it does, in the chroot).
- Tailscale on both devices (or the same Wi-Fi). The PC's receiver binds to
  the PC's Tailscale address by default.

Any other Linux box works the same way — a Raspberry Pi, a laptop: run
`xtion_head.py` + `head_record.py` there, or the tablet page itself.

## Install (from the PC)

```bash
./run_scanner_docker.sh receive                # 1. start the receiver (keep it running)
sensor_head/tablet/deploy_tablet.sh ping       # 2. adb + root + chroot + USB visibility
sensor_head/tablet/deploy_tablet.sh install    # 3. copy the code, apt/pip install in the chroot, write the config
sensor_head/tablet/deploy_tablet.sh start      # 4. start the capture page on the tablet
```

`install` generates `/etc/xtion-tablet.json` on the tablet with this PC's
Tailscale address and the receiver's token (`output/recordings/.token`,
created on the receiver's first start). `TABLET=host:port` (or a USB serial),
`CHROOT=`, `PC_HOST=`, `PC_PORT=` override the defaults at the top of the
script. Re-run `install` after changing any code under `sensor_head/`.

On the tablet, open **http://127.0.0.1:8080** in Chrome and use the menu's
*Add to Home screen*: that is the app icon. The page can also be started from
NetHunter's terminal with `xtion-tablet start` (`stop`, `status`, `logs`,
`list`). To start it at boot, add that command to NetHunter's *Custom
Commands* or a Termux:Boot script.

## Scanning

1. Plug the camera(s) in, tap **Start cameras** (or let the page start them).
   Each camera shows its state, frame rate and a live depth thumbnail.
2. **Capture sweep**: hold the rig still; N depth+colour frames per camera
   are stacked (12 by default, the Matterport-style station capture). Give it
   a label so the recordings are easy to tell apart. Move the tripod, repeat.
3. **Start recording** / **Stop recording**: continuous capture for handheld
   SLAM on the PC. Move slowly, as with the live scanner.
4. Each recording appears in the list with its size; **Send** uploads it to
   the PC (a progress bar shows the transfer; ✓ marks what has been sent).
   Recordings live in `/sdcard/xtion-scans/`, so the Android Files app sees
   them too, and `deploy_tablet.sh pull` copies them with adb when the
   receiver is not running.

## Processing on the PC

```bash
# a sweep -> output/sweeps/sweep_NN.ply, then the usual global registration
.venv/bin/python station_scan.py import output/recordings/<rec> [more recs…]
.venv/bin/python station_scan.py build

# anything else: replay the recording as the sensor head, then run a mode
./run_scanner_docker.sh replay output/recordings/<rec>        # in one terminal
SCANNER_HEAD=127.0.0.1 ./run_scanner_docker.sh multi          # in another: live view
SCANNER_HEAD=127.0.0.1 ./run_scanner_docker.sh station capture
.venv/bin/python calibrate_head.py --head 127.0.0.1           # extrinsics from a recording
.venv/bin/python sensor_head/head_probe.py 127.0.0.1          # is the replay healthy?
```

Sweep recordings loop by default so a station capture finds a steady
stream; continuous recordings play once (`--loop`, `--once`, `--speed`
override). The GUI's *Sensor head* field takes `127.0.0.1` while a replay runs.

Recording on the PC works the same way for testing:
`./run_scanner_docker.sh head` then `./run_scanner_docker.sh record --sweep 12 --label desk`.

## The recording format

`output/recordings/<YYYYmmdd-HHMMSS>_<label>/` holds `meta.json` (the head's
hello: cameras, USB ports, intrinsics; mode; frame counts) and `frames.bin`
(each frame's wire header plus zlib-compressed depth or JPEG colour). See
`sensor_head/head_recording.py`. Measured on one Xtion at 320×240, 15 fps:
a 12-frame sweep is 0.4 MB; continuous capture is about 0.5 MB/s per camera
(roughly four times that at 640×480).

## Troubleshooting

| Symptom | What to do |
|---|---|
| Page shows "no cameras found yet" | `deploy_tablet.sh list` (or `xtion-tablet list` on the tablet) shows what the chroot sees on USB. Nothing: the tablet is not in USB host mode (try another cable/hub; a powered hub for several cameras). Listed but not streaming: look at the Cameras card's error text and `deploy_tablet.sh logs`. |
| Camera streams for a second, then disappears | Power. One camera per port or a powered hub. |
| `/sys/bus/usb` not visible in the chroot | `deploy_tablet.sh start/ping` bind-mount `/dev`, `/proc`, `/sys`, `/sdcard` into the chroot; NetHunter's own terminal does the same when it boots the chroot. |
| "PC unreachable" on the page | The receiver is not running (`./run_scanner_docker.sh receive`), Tailscale is down on one side, or the address in `/etc/xtion-tablet.json` (`pc_url`) is wrong. The page's *check* button retries. |
| Upload fails with HTTP 401 | Token mismatch: re-run `deploy_tablet.sh install` (it copies the PC's current token). |
| Upload fails with HTTP 409 | A recording of that name is already on the PC; the page sends `replace=1`, so this only happens with an old page build. |
| A recording is marked incomplete | The sweep stopped before every camera delivered N frames (a camera dropped out) or a continuous recording was cut by the size limit. It still imports/replays; frame counts are in `meta.json`. |

## Security notes

The page listens on the tablet's loopback only (`listen` in the config),
so only the tablet's own browser can drive it. The PC receiver listens on
the Tailscale address only, requires the shared token for every API call,
rejects uploads that are not a recording, and never follows `..` or symlinks
in an upload.

## Why not an existing app

There is no ready-made Android app for PrimeSense/Xtion sensors. DotProduct's
Dot3D (Android, Snapdragon required) supports RealSense and Orbbec cameras,
and PrimeSense only as the Carmine 1.08/1.09 with a per-sensor calibration
file and a paid licence; the RTAB-Map app uses the phone's own ARCore/Tango
depth; OpenNI2 on Android otherwise means cross-compiling it yourself. The
chroot route uses Debian's packaged OpenNI2 for arm64 and the head code that
already exists here, which is why the "app" is a few hundred lines of Python.
