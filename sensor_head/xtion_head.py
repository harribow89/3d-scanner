#!/usr/bin/env python3
"""xtion_head — turn a box with up to 6 PrimeSense / ASUS Xtion sensors into a
network "sensor head" (written for the i.MX6 board out of a Matterport Pro2,
also runs on any Linux PC for testing).

    python3 xtion_head.py [--port 5599] [--fps 15] [--width 320 --height 240]
                          [--no-color] [--config /etc/xtion-head.json]
    python3 xtion_head.py --list          # what is plugged in, then exit

Why a head and not SLAM on the board: the i.MX6 is far too slow for RTAB-Map.
The board does what it is good at — keeping three USB depth cameras streaming
— and the PC runs mapping/calibration (sensor_head/head_bridge.py turns the
stream back into the ROS topics the scanner already uses).

Robustness (the things that broke the 3-camera rig on the desktop):
  * cameras are named by their physical USB PORT (camera1..N in port order,
    or the order in the config file), so names survive re-plugs;
  * cameras are opened STAGGERED (inrush current + USB setup traffic);
  * the Xtion's USB-audio interfaces are de-authorized first — snd-usb-audio
    probing them knocks the sensor off the bus;
  * a per-camera watchdog closes and re-opens a camera whose frames stop
    (USB flap / re-enumeration), re-resolving it by port;
  * every client gets the LATEST frame per stream: a slow network drops
    frames, it never back-pressures the cameras.

Wire protocol (TCP, default port 5599), all lines UTF-8 JSON + "\\n":
  server -> client  {"proto":1,"type":"hello","host":..,"cameras":[{ns,port,
                     uri,serial,width,height,fps,fx,fy,cx,cy,color}]}
  client -> server  (optional, any time) {"fps":10,"color":true}
  server -> client  {"type":"frame","ns":"camera1","kind":"depth"|"rgb",
                     "seq":N,"stamp":unix_s,"w":320,"h":240,
                     "enc":"16UC1"|"rgb8","n":<bytes>}  followed by n raw bytes
                    {"type":"status", "cameras":{ns:{state,fps_depth,fps_rgb,
                     restarts,last_error}}}  once a second
Depth is registered to the colour camera, millimetres, 0 = no data.
"""
import argparse
import glob
import json
import math
import os
import socket
import sys
import threading
import time

import numpy as np

try:
    from primesense import openni2
    from primesense import _openni2 as c_api
except ImportError:  # pragma: no cover
    sys.exit("xtion_head: the 'primesense' Python package is missing (pip install primesense)")

VENDOR = "1d27"
DEFAULT_PORT = 5599
WATCHDOG_S = 4.0          # no frame for this long -> reopen the camera
STAGGER_S = 2.5           # between opening cameras
SYSFS = "/sys/bus/usb/devices"

log_lock = threading.Lock()


def log(*a):
    with log_lock:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ USB helpers
def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def port_key(port):
    """Natural sort for USB port paths: 1-7 < 1-9 < 1-10 < 1-10.2."""
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in port.replace("-", ".").split(".")]


def xtion_ports():
    """{usb port path: (bus, dev)} for every PrimeSense device on the bus."""
    out = {}
    for d in sorted(glob.glob(f"{SYSFS}/*")):
        name = os.path.basename(d)
        if ":" in name or name.startswith("usb"):
            continue
        if _read(f"{d}/idVendor").lower() != VENDOR:
            continue
        try:
            out[name] = (int(_read(f"{d}/busnum")), int(_read(f"{d}/devnum")))
        except ValueError:
            continue
    return out


def detach_audio():
    """De-authorize the Xtions' USB-audio interfaces (class 01). Needs root;
    harmless (and silent) without it — the image also ships a udev rule."""
    n = 0
    for i in glob.glob(f"{SYSFS}/*:*"):
        if _read(f"{i}/bInterfaceClass") != "01":
            continue
        dev = f"{SYSFS}/{os.path.basename(i).split(':')[0]}"
        if _read(f"{dev}/idVendor").lower() != VENDOR or _read(f"{i}/authorized") != "1":
            continue
        try:
            with open(f"{i}/authorized", "w") as fh:
                fh.write("0")
            n += 1
        except OSError:
            pass
    if n:
        log(f"detached the USB-audio driver from {n} Xtion microphone interface(s)")


def uri_for_port(port):
    """OpenNI2 URI ('1d27/0601@BUS/DEV') of the Xtion on this USB port, or None."""
    bd = xtion_ports().get(port)
    if not bd:
        return None
    for uri in openni2.Device.enumerate_uris():
        u = uri.decode() if isinstance(uri, bytes) else uri
        if u.endswith(f"@{bd[0]}/{bd[1]}"):
            return u
    return None


def prepare_openni(lib_dir, usb_mode):
    """OpenNI2 reads the PS1080 driver's settings from Drivers/PS1080.ini next
    to the driver. To choose the USB transfer mode without touching system
    files, build a private copy of the layout (symlinks) plus our own ini.

    bulk (default): no isochronous bandwidth reservation, so 3 sensors share
    one USB-2 link / hub (the i.MX6 board) without 'Failed to set USB
    interface'. Verified 2026-10-26: the Matterport sensors stream at 30 fps
    in bulk mode (interface 0, alt setting 0 = bulk endpoints)."""
    # Always a private layout: Debian's runtime package ships only
    # libOpenNI2.so.0, but the bindings load "libOpenNI2.so" (normally from -dev).
    drivers = os.path.join(lib_dir, "OpenNI2", "Drivers")
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    priv = os.path.join(base, f"xtion-head-openni-{os.getuid()}")
    pdrv = os.path.join(priv, "OpenNI2", "Drivers")
    os.makedirs(pdrv, exist_ok=True)

    def link(src, dst):
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(src, dst)

    lib = os.path.join(lib_dir, "libOpenNI2.so")
    if not os.path.exists(lib):
        lib = os.path.join(lib_dir, "libOpenNI2.so.0")
    link(lib, os.path.join(priv, "libOpenNI2.so"))
    for name in os.listdir(drivers):
        if not name.endswith(".ini"):
            link(os.path.join(drivers, name), os.path.join(pdrv, name))
    ini = os.path.join(pdrv, "PS1080.ini")
    if usb_mode == "default":
        if os.path.lexists(ini):
            os.remove(ini)
        if os.path.exists(os.path.join(drivers, "PS1080.ini")):
            link(os.path.join(drivers, "PS1080.ini"), ini)
    else:
        iface = {"bulk": 2, "iso": 1}[usb_mode]
        if os.path.lexists(ini):
            os.remove(ini)
        with open(ini, "w") as fh:
            fh.write(f"[Device]\n; 0 = driver default, 1 = isochronous, 2 = bulk\n"
                     f"UsbInterface={iface}\n")
    return priv


# --------------------------------------------------------------- frame storage
class Latest:
    """Latest frame per (ns, kind) + a condition for senders."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frames = {}      # (ns, kind) -> (seq, header_bytes, payload)
        self.gen = 0

    def put(self, ns, kind, header, payload):
        with self.cond:
            key = (ns, kind)
            seq = self.frames.get(key, (0,))[0] + 1
            header["seq"] = seq
            self.frames[key] = (seq, (json.dumps(header) + "\n").encode(), payload)
            self.gen += 1
            self.cond.notify_all()


# -------------------------------------------------------------------- cameras
class Camera(threading.Thread):
    def __init__(self, ns, port, args, latest):
        super().__init__(daemon=True, name=ns)
        self.ns, self.port, self.args, self.latest = ns, port, args, latest
        self.state = "waiting"
        self.uri = None
        self.serial = ""
        self.restarts = 0
        self.last_error = ""
        self.intr = None           # fx, fy, cx, cy
        self.fps = {"depth": 0.0, "rgb": 0.0}
        self._stamps = {"depth": [], "rgb": []}
        self.want_color = not args.no_color
        self.min_period = 1.0 / max(args.fps, 0.5)
        self._stop = False

    # -- info for the hello message
    def info(self):
        fx, fy, cx, cy = self.intr or (0, 0, 0, 0)
        return {"ns": self.ns, "port": self.port, "uri": self.uri, "serial": self.serial,
                "width": self.args.width, "height": self.args.height, "fps": self.args.fps,
                "fx": fx, "fy": fy, "cx": cx, "cy": cy, "color": self.want_color,
                "state": self.state}

    def status(self):
        now = time.time()
        for k, st in self._stamps.items():
            st[:] = [t for t in st if now - t < 2.0]
            self.fps[k] = round(len(st) / 2.0, 1)
        st = {"state": self.state, "fps_depth": self.fps["depth"], "fps_rgb": self.fps["rgb"],
              "restarts": self.restarts, "last_error": self.last_error, "uri": self.uri}
        if self.intr:   # lets a client that connected before this camera opened catch up
            st.update(fx=self.intr[0], fy=self.intr[1], cx=self.intr[2], cy=self.intr[3],
                      width=self.args.width, height=self.args.height)
        return st

    def _open(self):
        uri = uri_for_port(self.port)
        if not uri:
            raise RuntimeError(f"no Xtion on USB port {self.port}")
        dev = openni2.Device(uri.encode())   # the ctypes binding wants bytes
        try:
            info = dev.get_device_info()
            self.serial = getattr(info, "uri", b"").decode(errors="replace") if hasattr(info, "uri") else ""
        except Exception:
            pass
        w, h, f = self.args.width, self.args.height, max(int(round(self.args.fps)), 1)
        # the PS1080 only does 30 Hz at QVGA/VGA (and 60 at QVGA); ask for 30 and
        # throttle in software, which also keeps the USB schedule fixed
        hw_fps = 30
        depth = dev.create_depth_stream()
        depth.configure_mode(w, h, hw_fps, c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM)
        color = None
        if self.want_color and dev.has_sensor(openni2.SENSOR_COLOR):
            color = dev.create_color_stream()
            color.configure_mode(w, h, hw_fps, c_api.OniPixelFormat.ONI_PIXEL_FORMAT_RGB888)
            try:
                dev.set_image_registration_mode(openni2.IMAGE_REGISTRATION_DEPTH_TO_COLOR)
            except Exception as e:
                log(f"{self.ns}: registration not available ({e})")
            try:
                dev.set_depth_color_sync_enabled(True)
            except Exception:
                pass
        ref = color or depth
        hfov = ref.get_horizontal_fov()
        vfov = ref.get_vertical_fov()
        fx = (w / 2.0) / math.tan(hfov / 2.0)
        fy = (h / 2.0) / math.tan(vfov / 2.0)
        self.intr = (round(fx, 3), round(fy, 3), (w - 1) / 2.0, (h - 1) / 2.0)
        depth.start()
        if color:
            color.start()
        self.uri = uri
        return dev, depth, color

    @staticmethod
    def _close(dev, depth, color):
        for s in (color, depth):
            try:
                if s is not None:
                    s.stop()
                    s.close()
            except Exception:
                pass
        try:
            if dev is not None:
                dev.close()
        except Exception:
            pass

    def run(self):
        backoff = 1.0
        while not self._stop:
            dev = depth = color = None
            try:
                self.state = "opening"
                dev, depth, color = self._open()
                self.state = "streaming"
                log(f"{self.ns}: streaming from USB port {self.port} ({self.uri}) "
                    f"{self.args.width}x{self.args.height}, fx={self.intr[0]:.1f}"
                    + ("" if color else ", depth only"))
                backoff = 1.0
                self._stream(depth, color)
            except Exception as e:  # noqa: BLE001 — any driver error -> reopen
                self.last_error = str(e)[:200]
                if self.state != "waiting":
                    log(f"{self.ns}: {self.last_error} — reopening")
            finally:
                self._close(dev, depth, color)
            if self._stop:
                break
            self.restarts += 1
            self.state = "waiting"
            time.sleep(backoff)
            backoff = min(backoff * 1.6, 10.0)

    def _stream(self, depth, color):
        streams = [s for s in (depth, color) if s is not None]
        last_frame = time.time()
        last_sent = {"depth": 0.0, "rgb": 0.0}
        while not self._stop:
            try:
                ready = openni2.wait_for_any_stream(streams, timeout=1.0)
            except Exception as e:
                raise RuntimeError(f"wait failed: {e}")
            now = time.time()
            if ready is None:
                if now - last_frame > WATCHDOG_S:
                    raise RuntimeError(f"no frames for {WATCHDOG_S:.0f} s")
                continue
            frame = ready.read_frame()
            last_frame = now
            kind = "depth" if ready is depth else "rgb"
            self._stamps[kind].append(now)
            if now - last_sent[kind] < self.min_period * 0.97:
                continue
            last_sent[kind] = now
            w, h = frame.width, frame.height
            if kind == "depth":
                buf = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16).copy()
                enc = "16UC1"
            else:
                buf = np.frombuffer(frame.get_buffer_as_uint8(), dtype=np.uint8).copy()
                enc = "rgb8"
            self.latest.put(self.ns, kind, {"type": "frame", "ns": self.ns, "kind": kind,
                                            "stamp": now, "w": w, "h": h, "enc": enc,
                                            "n": buf.nbytes}, buf.tobytes())

    def stop(self):
        self._stop = True


# ---------------------------------------------------------------------- server
class Client(threading.Thread):
    def __init__(self, sock, addr, head):
        super().__init__(daemon=True)
        self.sock, self.addr, self.head = sock, addr, head
        self.alive = True
        self.color = True
        self.min_period = 0.0

    def _recv_controls(self):
        buf = b""
        self.sock.settimeout(None)
        try:
            while self.alive:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "fps" in msg:
                        self.min_period = 1.0 / max(float(msg["fps"]), 0.5)
                    if "color" in msg:
                        self.color = bool(msg["color"])
        except OSError:
            pass
        self.alive = False

    def run(self):
        log(f"client {self.addr[0]} connected")
        threading.Thread(target=self._recv_controls, daemon=True).start()
        latest = self.head.latest
        sent_seq = {}
        last_sent = {}
        last_status = 0.0
        try:
            self.sock.sendall((json.dumps(self.head.hello()) + "\n").encode())
            while self.alive:
                with latest.cond:
                    latest.cond.wait(timeout=0.5)
                    snapshot = dict(latest.frames)
                now = time.time()
                for key, (seq, hdr, payload) in snapshot.items():
                    if sent_seq.get(key) == seq:
                        continue
                    if key[1] == "rgb" and not self.color:
                        continue
                    if now - last_sent.get(key, 0.0) < self.min_period:
                        continue
                    self.sock.sendall(hdr)
                    self.sock.sendall(payload)
                    sent_seq[key] = seq
                    last_sent[key] = now
                if now - last_status > 1.0:
                    last_status = now
                    st = {"type": "status", "stamp": now,
                          "cameras": {c.ns: c.status() for c in self.head.cameras}}
                    self.sock.sendall((json.dumps(st) + "\n").encode())
        except OSError:
            pass
        finally:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            log(f"client {self.addr[0]} disconnected")


class Head:
    def __init__(self, args):
        self.args = args
        self.latest = Latest()
        self.cameras = []

    def hello(self):
        return {"proto": 1, "type": "hello", "host": socket.gethostname(),
                "cameras": [c.info() for c in self.cameras]}

    def plan_cameras(self):
        cfg = {}
        if self.args.config and os.path.exists(self.args.config):
            with open(self.args.config) as fh:
                cfg = json.load(fh)
        if cfg.get("cameras"):   # the scanner's cameras.json roster format
            cfg = {"order": [c["port"] for c in cfg["cameras"] if c.get("port")],
                   "names": {c["port"]: c["ns"] for c in cfg["cameras"] if c.get("port")}}
        order = [p for p in (cfg.get("order") or []) if p in xtion_ports()] or []
        present = sorted(xtion_ports(), key=port_key)
        ports = [p for p in order] + [p for p in present if p not in order]
        if not order:
            # wait briefly for all sensors to enumerate (they come up seconds apart)
            deadline = time.time() + self.args.settle
            while time.time() < deadline:
                now = sorted(xtion_ports())
                if len(now) >= self.args.expect:
                    break
                time.sleep(0.5)
            ports = sorted(xtion_ports(), key=port_key)
        names = cfg.get("names") or {}
        return [(names.get(p, f"camera{i + 1}"), p) for i, p in enumerate(ports[: self.args.max])]

    def start(self):
        detach_audio()
        plan = self.plan_cameras()
        if not plan:
            log("no Xtion found yet; waiting for one to be plugged in")
        for ns, port in plan:
            cam = Camera(ns, port, self.args, self.latest)
            self.cameras.append(cam)
        for cam in self.cameras:
            cam.start()
            time.sleep(STAGGER_S)
        log("cameras: " + ", ".join(f"{c.ns}=port {c.port}" for c in self.cameras))

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.args.port))
        srv.listen(4)
        log(f"listening on tcp/{self.args.port}")
        while True:
            sock, addr = srv.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            Client(sock, addr, self).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description="network sensor head for PrimeSense / Xtion cameras")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--fps", type=float, default=15.0, help="frames per stream sent (camera runs at 30)")
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--height", type=int, default=240)
    ap.add_argument("--no-color", action="store_true", help="depth only (halves USB load)")
    ap.add_argument("--expect", type=int, default=3, help="cameras to wait for at start")
    ap.add_argument("--settle", type=float, default=20.0, help="max seconds to wait for them")
    ap.add_argument("--max", type=int, default=6)
    ap.add_argument("--config", default="/etc/xtion-head.json",
                    help='optional {"order": ["1-1.2", ...], "names": {"1-1.2": "camera1"}}')
    ap.add_argument("--openni-dir", default=None, help="directory holding libOpenNI2.so")
    ap.add_argument("--usb", choices=("bulk", "iso", "default"), default="bulk",
                    help="USB transfer mode: bulk (default) shares one USB-2 link/hub safely; "
                         "iso reserves bandwidth per camera; default = the driver's own choice")
    ap.add_argument("--list", action="store_true", help="list the sensors and exit")
    args = ap.parse_args(argv)

    dirs = [d for d in (args.openni_dir, "/usr/lib/arm-linux-gnueabihf",
                        "/usr/lib/aarch64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/usr/lib")
            if d and (os.path.exists(os.path.join(d, "libOpenNI2.so"))
                      or os.path.exists(os.path.join(d, "libOpenNI2.so.0")))]
    if not dirs:
        sys.exit("xtion_head: libOpenNI2.so not found (apt install libopenni2-0)")
    use = prepare_openni(dirs[0], args.usb) if not args.openni_dir else dirs[0]
    openni2.initialize(use)
    log(f"OpenNI2 from {dirs[0]}, USB {args.usb} transfers")

    if args.list:
        ports = xtion_ports()
        print(f"{len(ports)} PrimeSense sensor(s):")
        for p, (b, d) in sorted(ports.items(), key=lambda kv: port_key(kv[0])):
            print(f"  USB port {p}: bus {b} dev {d}  uri={uri_for_port(p)}")
        return 0

    head = Head(args)
    head.start()
    head.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
