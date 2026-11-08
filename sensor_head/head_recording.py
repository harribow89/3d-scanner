#!/usr/bin/env python3
"""head_recording — the on-disk format for an xtion_head stream.

The tablet records (sensor_head/head_record.py), the PC replays
(sensor_head/head_replay.py) the same frames back over the head's wire
protocol, so RTAB-Map, calibration and station sweeps run on a recording
exactly as they would on live cameras. station_scan.py can also turn a sweep
recording straight into a sweep cloud without Docker (`station import`).

Layout of a recording directory:

    meta.json    {"format": "xtion-head-recording", "version": 1,
                  "hello": <the head's hello message>, "mode": "sweep"|"continuous",
                  "request": {"fps": .., "color": ..}, "label": .., "created": unix_s,
                  "host": .., "frames": {ns: {"depth": n, "rgb": n}},
                  "duration_s": .., "bytes": .., "complete": bool}
    frames.bin   records, each a UTF-8 JSON line followed by its payload:
                   frame:  the wire header {"type":"frame","ns","kind","w","h","enc",
                           "stamp"} plus "t" (seconds since the recording began),
                           "comp" ("zlib" | "jpeg" | "none"), "n" (stored bytes) and
                           "raw" (bytes once decoded)
                   status: the head's status line plus "t", no payload

Depth (16UC1, millimetres) is zlib-compressed: lossless, about 3-5x smaller.
Colour (rgb8) is JPEG when Pillow is available, else zlib. A 320x240 sweep of
12 frames per camera is a few MB; continuous recording at 15 fps runs at
roughly 1-2 MB/s per camera.
"""
import json
import os
import socket
import time
import zlib

import numpy as np

try:
    from PIL import Image
    import io
    HAVE_PIL = True
except ImportError:  # pragma: no cover - Pillow is optional
    HAVE_PIL = False

FORMAT = "xtion-head-recording"
VERSION = 1
META = "meta.json"
FRAMES = "frames.bin"
DEFAULT_PORT = 5599


# ------------------------------------------------------------------ wire helpers
def read_line(sock, buf):
    """Return (line, rest) from a socket, pulling until a newline arrives."""
    while b"\n" not in buf:
        chunk = sock.recv(1 << 16)
        if not chunk:
            raise ConnectionError("head closed the connection")
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return line, buf


def read_exact(sock, n, buf):
    while len(buf) < n:
        chunk = sock.recv(max(1 << 16, n - len(buf)))
        if not chunk:
            raise ConnectionError("head closed the connection")
        buf += chunk
    return buf[:n], buf[n:]


def connect(host, port=DEFAULT_PORT, fps=15.0, color=True, timeout=30):
    """Open a client connection to a head; return (sock, hello, buf)."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall((json.dumps({"fps": fps, "color": bool(color)}) + "\n").encode())
    line, buf = read_line(sock, b"")
    hello = json.loads(line)
    if hello.get("type") != "hello":
        raise ConnectionError(f"expected a hello, got {hello.get('type')!r}")
    return sock, hello, buf


def split_host(spec, default_port=DEFAULT_PORT):
    host, _, port = spec.strip().partition(":")
    return host, (int(port) if port.isdigit() else default_port)


# ------------------------------------------------------------------- codecs
def encode_payload(kind, payload, w, h, jpeg_quality=90):
    """Compress one frame payload. Returns (comp, data)."""
    if kind == "rgb" and HAVE_PIL and len(payload) == w * h * 3:
        img = Image.frombuffer("RGB", (w, h), bytes(payload), "raw", "RGB", 0, 1)
        bio = io.BytesIO()
        img.save(bio, format="JPEG", quality=jpeg_quality)
        return "jpeg", bio.getvalue()
    return "zlib", zlib.compress(bytes(payload), 1)


def decode_payload(comp, data, w, h, enc):
    """Inverse of encode_payload: the raw wire bytes."""
    if comp == "none":
        return bytes(data)
    if comp == "zlib":
        return zlib.decompress(data)
    if comp == "jpeg":
        if not HAVE_PIL:
            raise RuntimeError("this recording holds JPEG colour frames: pip install pillow")
        img = Image.open(io.BytesIO(bytes(data))).convert("RGB")
        if img.size != (w, h):
            img = img.resize((w, h))
        return img.tobytes()
    raise ValueError(f"unknown compression {comp!r}")


# ------------------------------------------------------------------- writer
class Writer:
    """Append frames and status lines to a new recording directory."""

    def __init__(self, directory, hello, request, mode="continuous", label="", jpeg_quality=90):
        os.makedirs(directory, exist_ok=True)
        self.dir = directory
        self.t0 = time.time()
        self.hello = hello
        self.jpeg_quality = jpeg_quality
        self.counts = {}
        self.bytes = 0
        self.last_t = 0.0
        self.meta = {"format": FORMAT, "version": VERSION, "created": self.t0,
                     "host": hello.get("host", ""), "hello": hello, "mode": mode,
                     "request": request, "label": label, "frames": self.counts,
                     "duration_s": 0.0, "bytes": 0, "complete": False}
        self._fh = open(os.path.join(directory, FRAMES), "wb")
        self._write_meta()

    def _write_meta(self):
        self.meta["frames"] = self.counts
        self.meta["duration_s"] = round(self.last_t, 3)
        self.meta["bytes"] = self.bytes
        tmp = os.path.join(self.dir, META + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(self.meta, fh, indent=1)
        os.replace(tmp, os.path.join(self.dir, META))

    def frame(self, header, payload, now=None):
        now = time.time() if now is None else now
        t = max(0.0, now - self.t0)
        comp, data = encode_payload(header["kind"], payload, header["w"], header["h"],
                                    self.jpeg_quality)
        rec = dict(header)
        rec.update(t=round(t, 4), comp=comp, n=len(data), raw=len(payload))
        rec.pop("seq", None)
        line = (json.dumps(rec) + "\n").encode()
        self._fh.write(line)
        self._fh.write(data)
        self.bytes += len(line) + len(data)
        self.last_t = t
        c = self.counts.setdefault(header["ns"], {"depth": 0, "rgb": 0})
        c[header["kind"]] = c.get(header["kind"], 0) + 1
        return c[header["kind"]]

    def status(self, msg, now=None):
        now = time.time() if now is None else now
        rec = dict(msg)
        rec["t"] = round(max(0.0, now - self.t0), 4)
        rec.pop("n", None)
        line = (json.dumps(rec) + "\n").encode()
        self._fh.write(line)
        self.bytes += len(line)
        self.last_t = rec["t"]

    def count(self, ns, kind):
        return self.counts.get(ns, {}).get(kind, 0)

    def close(self, complete=True, **extra):
        if self._fh:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
        self.meta["complete"] = bool(complete)
        self.meta.update(extra)
        self._write_meta()
        return self.meta


# ------------------------------------------------------------------- reader
class Reader:
    """Iterate a recording. `records()` yields (header, payload) with the
    payload decoded back to raw wire bytes (None for status lines)."""

    def __init__(self, directory):
        self.dir = directory
        with open(os.path.join(directory, META)) as fh:
            self.meta = json.load(fh)
        if self.meta.get("format") != FORMAT:
            raise ValueError(f"{directory} is not an xtion-head recording")
        self.hello = self.meta["hello"]
        self.frames_path = os.path.join(directory, FRAMES)

    @property
    def cameras(self):
        return [c["ns"] for c in self.hello.get("cameras", [])]

    def camera(self, ns):
        for c in self.hello.get("cameras", []):
            if c["ns"] == ns:
                return c
        return None

    def records(self, decode=True, kinds=None, cameras=None):
        with open(self.frames_path, "rb") as fh:
            while True:
                line = fh.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                header = json.loads(line)
                if header.get("type") != "frame":
                    if not kinds and not cameras:   # filters select frames only
                        yield header, None
                    continue
                data = fh.read(header["n"])
                if len(data) < header["n"]:
                    return  # truncated tail (recorder was killed) - stop cleanly
                if (kinds and header["kind"] not in kinds) or \
                        (cameras and header["ns"] not in cameras):
                    continue
                if decode:
                    data = decode_payload(header["comp"], data, header["w"], header["h"],
                                          header["enc"])
                yield header, data

    def summary(self):
        counts = self.meta.get("frames") or {}
        return {"cameras": self.cameras, "frames": counts,
                "duration_s": self.meta.get("duration_s", 0.0),
                "bytes": self.meta.get("bytes", 0), "mode": self.meta.get("mode"),
                "complete": self.meta.get("complete", False),
                "label": self.meta.get("label", "")}


def is_recording(directory):
    return os.path.isfile(os.path.join(directory, META)) and \
        os.path.isfile(os.path.join(directory, FRAMES))


def list_recordings(root):
    """Recording directories under root, oldest first."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if is_recording(d):
            out.append(d)
    return out


# ------------------------------------------------------------- geometry helpers
def depth_to_points(depth_mm, rgb, fx, fy, cx, cy, min_m=0.3, max_m=6.0):
    """Back-project a registered depth image (uint16 mm, shape h x w) into the
    camera's optical frame (x right, y down, z forward), the frame the ROS
    depth_registered/points cloud uses. Returns (xyz float32 Nx3, rgb uint8 Nx3)."""
    h, w = depth_mm.shape
    z = depth_mm.astype(np.float32) / 1000.0
    ok = (z >= min_m) & (z <= max_m)
    v, u = np.nonzero(ok)
    z = z[ok]
    x = (u.astype(np.float32) - cx) * z / fx
    y = (v.astype(np.float32) - cy) * z / fy
    xyz = np.stack([x, y, z], axis=1).astype(np.float32)
    if rgb is not None and rgb.shape[:2] == (h, w):
        col = rgb[ok]
    else:
        col = np.full((xyz.shape[0], 3), 200, np.uint8)
    return xyz, col


def write_ply(path, xyz, rgb):
    n = xyz.shape[0]
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    ).encode()
    verts = np.empty(n, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                               ("r", "u1"), ("g", "u1"), ("b", "u1")])
    verts["x"], verts["y"], verts["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    verts["r"], verts["g"], verts["b"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(verts.tobytes())
    return n


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
