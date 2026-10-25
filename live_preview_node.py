#!/usr/bin/env python3
"""live_preview_node — runs INSIDE the ROS container and writes what the camera
sees into /scanner/.live/ as small JPEGs plus a status.json, so the host GUI
(which has no ROS) can show a live preview with zero extra dependencies.

    python3 live_preview_node.py [--ns camera[,camera2,...]] [--out /scanner/.live]
                                 [--hz 8] [--quality 80] [--max-width 480]

Per namespace it subscribes to
    /<ns>/rgb/image_raw                 rgb8 / bgr8 / mono8
    /<ns>/depth_registered/image_raw    16UC1 (mm) or 32FC1 (m)   registered depth
    /<ns>/depth/image                   same encodings            depth-only rigs
and writes <ns>_rgb.jpg, <ns>_depth.jpg (colour-mapped, red = near, 0.3-4 m)
and status.json:
    {"ts": <epoch>, "cameras": {"<ns>": {"rgb_frames", "depth_frames", "rgb_fps",
     "depth_fps", "width", "height", "depth_fill_pct", "median_depth_m",
     "rgb_encoding", "depth_encoding", "last_rgb_ts", "last_depth_ts"}}}

Files are written to a temp name and renamed, so the GUI never reads a torn
file. Subscribing also *starts* the openni2 driver's lazy streams, which is why
the launcher runs this node even in headless mapping mode.
"""
import argparse
import json
import os
import sys
import time
from collections import deque

import cv2
import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image

NEAR_M, FAR_M = 0.3, 4.0
# A 640x480 RGB frame is ~920 KB: too big for FastDDS shared memory, so it goes
# over fragmented UDP. A BEST_EFFORT subscriber then drops the whole frame
# whenever one fragment is late (measured: 10 Hz published -> ~4 Hz received).
# So we wait for the publisher and MATCH its reliability instead of guessing.
QOS_RELIABLE = QoSProfile(depth=3, reliability=ReliabilityPolicy.RELIABLE,
                          history=HistoryPolicy.KEEP_LAST)
QOS_BEST_EFFORT = QoSProfile(depth=3, reliability=ReliabilityPolicy.BEST_EFFORT,
                             history=HistoryPolicy.KEEP_LAST)
PUBLISHER_WAIT_S = 20.0   # after this, subscribe RELIABLE anyway


def atomic_write_bytes(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class Rate:
    """Sliding-window frame counter."""

    def __init__(self, window_s=2.0):
        self.window = window_s
        self.stamps = deque()
        self.total = 0

    def tick(self, now):
        self.total += 1
        self.stamps.append(now)
        while self.stamps and now - self.stamps[0] > self.window:
            self.stamps.popleft()

    def fps(self, now):
        while self.stamps and now - self.stamps[0] > self.window:
            self.stamps.popleft()
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return round((len(self.stamps) - 1) / span, 1) if span > 0 else 0.0


class CamState:
    def __init__(self):
        self.rgb = Rate()
        self.depth = Rate()
        self.width = 0
        self.height = 0
        self.fill_pct = 0.0
        self.median_m = 0.0
        self.rgb_encoding = ""
        self.depth_encoding = ""
        self.last_rgb_ts = 0.0
        self.last_depth_ts = 0.0
        self.last_rgb_write = 0.0
        self.last_depth_write = 0.0


class LivePreview(Node):
    def __init__(self, namespaces, out_dir, hz, quality, max_width):
        super().__init__("live_preview")
        self.out_dir = out_dir
        self.period = 1.0 / max(hz, 0.5)
        self.quality = int(quality)
        self.max_width = int(max_width)
        self.cams = {ns: CamState() for ns in namespaces}
        os.makedirs(out_dir, exist_ok=True)
        self.started = time.time()
        # topic -> (callback, ns); subscribed lazily once we can see the publisher's QoS
        self.pending = {}
        self.subs = []
        for ns in namespaces:
            self.pending[f"/{ns}/rgb/image_raw"] = (self.on_rgb, ns)
            self.pending[f"/{ns}/depth_registered/image_raw"] = (self.on_depth, ns)
            self.pending[f"/{ns}/depth/image"] = (self.on_depth, ns)
        self.create_timer(0.5, self.write_status)
        self.create_timer(0.5, self.ensure_subscriptions)
        self.ensure_subscriptions()
        self.get_logger().info(f"live preview -> {out_dir} for {namespaces} at {hz} Hz")

    def ensure_subscriptions(self):
        """Subscribe to each topic with the publisher's own reliability.
        Subscribing also kicks the openni2 driver's lazy streams into life."""
        if not self.pending:
            return
        waited_long = time.time() - self.started > PUBLISHER_WAIT_S
        for topic in list(self.pending):
            infos = self.get_publishers_info_by_topic(topic)
            if infos:
                rel = infos[0].qos_profile.reliability
                qos = QOS_BEST_EFFORT if rel == ReliabilityPolicy.BEST_EFFORT else QOS_RELIABLE
                label = "best-effort" if qos is QOS_BEST_EFFORT else "reliable"
            elif waited_long:
                qos, label = QOS_RELIABLE, "reliable (no publisher seen yet)"
            else:
                continue
            cb, ns = self.pending.pop(topic)
            self.subs.append(self.create_subscription(
                Image, topic, lambda m, c=cb, n=ns: c(m, n), qos))
            self.get_logger().info(f"subscribed {topic} [{label}]")

    # ---------------------------------------------------------------- helpers
    def _shrink(self, img):
        h, w = img.shape[:2]
        if w > self.max_width:
            s = self.max_width / w
            img = cv2.resize(img, (self.max_width, int(h * s)), interpolation=cv2.INTER_AREA)
        return img

    def _save_jpg(self, name, bgr):
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if ok:
            atomic_write_bytes(os.path.join(self.out_dir, name), buf.tobytes())

    # -------------------------------------------------------------- callbacks
    def on_rgb(self, msg, ns):
        now = time.time()
        st = self.cams[ns]
        st.rgb.tick(now)
        st.last_rgb_ts = now
        st.rgb_encoding = msg.encoding
        st.width, st.height = msg.width, msg.height
        if now - st.last_rgb_write < self.period:
            return
        st.last_rgb_write = now
        try:
            if msg.encoding in ("rgb8", "bgr8"):
                img = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width, 3)
                bgr = img[:, :, ::-1] if msg.encoding == "rgb8" else img
            elif msg.encoding in ("mono8", "8UC1"):
                img = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width)
                bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            elif msg.encoding in ("rgba8", "bgra8"):
                img = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width, 4)[:, :, :3]
                bgr = img[:, :, ::-1] if msg.encoding == "rgba8" else img
            else:
                return
            self._save_jpg(f"{ns}_rgb.jpg", self._shrink(np.ascontiguousarray(bgr)))
        except Exception as e:  # never let a bad frame kill the preview
            self.get_logger().warn(f"rgb frame skipped: {e}")

    def on_depth(self, msg, ns):
        now = time.time()
        st = self.cams[ns]
        st.depth.tick(now)
        st.last_depth_ts = now
        st.depth_encoding = msg.encoding
        if not st.width:
            st.width, st.height = msg.width, msg.height
        if now - st.last_depth_write < self.period:
            return
        st.last_depth_write = now
        try:
            if msg.encoding == "32FC1":
                z = np.frombuffer(msg.data, np.float32).reshape(msg.height, msg.width)
            elif msg.encoding in ("16UC1", "mono16"):
                z = np.frombuffer(msg.data, np.uint16).reshape(msg.height, msg.width) / 1000.0
            else:
                return
            valid = np.isfinite(z) & (z > 0)
            st.fill_pct = round(float(valid.mean() * 100.0), 1)
            st.median_m = round(float(np.median(z[valid])), 3) if valid.any() else 0.0
            # the real Xtion sends registered depth as 32FC1 with NaN holes
            zf = np.where(valid, z, 0.0)
            norm = np.clip((zf - NEAR_M) / (FAR_M - NEAR_M), 0.0, 1.0)
            idx = (255 - norm * 255).astype(np.uint8)          # near = red end of TURBO
            colour = cv2.applyColorMap(idx, cv2.COLORMAP_TURBO)
            colour[~valid] = 0
            self._save_jpg(f"{ns}_depth.jpg", self._shrink(colour))
        except Exception as e:
            self.get_logger().warn(f"depth frame skipped: {e}")

    def write_status(self):
        now = time.time()
        cams = {}
        for ns, st in self.cams.items():
            cams[ns] = {
                "rgb_frames": st.rgb.total, "depth_frames": st.depth.total,
                "rgb_fps": st.rgb.fps(now), "depth_fps": st.depth.fps(now),
                "width": st.width, "height": st.height,
                "depth_fill_pct": st.fill_pct, "median_depth_m": st.median_m,
                "rgb_encoding": st.rgb_encoding, "depth_encoding": st.depth_encoding,
                "last_rgb_ts": st.last_rgb_ts, "last_depth_ts": st.last_depth_ts,
            }
        payload = json.dumps({"ts": now, "pid": os.getpid(), "cameras": cams}).encode()
        atomic_write_bytes(os.path.join(self.out_dir, "status.json"), payload)


def summarize(status_path):
    """Print one RESULT line per camera from a status.json; exit 0 if frames flowed."""
    try:
        with open(status_path) as fh:
            cams = json.load(fh).get("cameras", {})
    except (OSError, ValueError) as e:
        print(f"RESULT: no status file ({e})")
        return 2
    ok = False
    for ns, s in cams.items():
        print(f"RESULT [{ns}]: rgb {s['rgb_fps']} fps ({s['rgb_frames']} frames), "
              f"depth {s['depth_fps']} fps ({s['depth_frames']} frames), "
              f"{s['width']}x{s['height']}, depth fill {s['depth_fill_pct']}%, "
              f"median {s['median_depth_m']} m")
        ok = ok or s["depth_frames"] > 0 or s["rgb_frames"] > 0
    if not ok:
        print("RESULT: no frames were received — the camera did not stream.")
    return 0 if ok else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ns", default="camera", help="comma-separated camera namespaces")
    ap.add_argument("--out", default="/scanner/.live")
    ap.add_argument("--hz", type=float, default=8.0, help="max JPEG write rate per stream")
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--max-width", type=int, default=480)
    ap.add_argument("--seconds", type=float, default=0.0, help="exit after N seconds (0 = run)")
    ap.add_argument("--summary", metavar="STATUS_JSON",
                    help="print a one-line summary of an existing status.json and exit")
    args, ros_args = ap.parse_known_args(argv)
    if args.summary:
        return summarize(args.summary)

    rclpy.init(args=ros_args)
    node = LivePreview([n for n in args.ns.split(",") if n], args.out, args.hz,
                       args.quality, args.max_width)
    deadline = time.time() + args.seconds if args.seconds > 0 else None
    try:
        while rclpy.ok() and (deadline is None or time.time() < deadline):
            rclpy.spin_once(node, timeout_sec=0.2)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass  # SIGINT from the launcher's clean shutdown
    finally:
        try:
            node.write_status()
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
