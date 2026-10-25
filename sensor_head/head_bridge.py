#!/usr/bin/env python3
"""head_bridge — ROS 2 node (runs INSIDE the scanner container) that connects
to an xtion_head (the i.MX6 sensor head, or one running on this PC) and
publishes exactly what the openni2 driver + depth_image_proc would:

    /<ns>/rgb/image_raw                rgb8
    /<ns>/rgb/camera_info
    /<ns>/depth_registered/image_raw   16UC1 mm, registered to rgb
    /<ns>/depth_registered/camera_info
    /<ns>/depth_registered/points      XYZRGB (only built while someone subscribes)

with frame_id <ns>_rgb_optical_frame, so multi_camera.launch.py, RTAB-Map,
capture_station.py, calibrate_multi.py and station_scan.py work unchanged.
The link/optical TFs come from multi_camera.launch.py as before.

    python3 head_bridge.py --head HOST[:PORT] [--only camera1,camera2] [--fps 15]

Frames are stamped with the PC's clock on arrival (the board's clock may be
off; a few ms of network latency is far below what RTAB-Map's approx sync
tolerates). Reconnects forever if the head goes away.
"""
import argparse
import json
import socket
import sys
import threading
import time

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField

QOS = QoSProfile(depth=2, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST)


class Cam:
    def __init__(self, node, ns, info):
        self.ns, self.info = ns, info
        self.frame = f"{ns}_rgb_optical_frame"
        self.pub_rgb = node.create_publisher(Image, f"/{ns}/rgb/image_raw", QOS)
        self.pub_rgb_info = node.create_publisher(CameraInfo, f"/{ns}/rgb/camera_info", QOS)
        self.pub_depth = node.create_publisher(Image, f"/{ns}/depth_registered/image_raw", QOS)
        self.pub_depth_info = node.create_publisher(CameraInfo, f"/{ns}/depth_registered/camera_info", QOS)
        self.pub_points = node.create_publisher(PointCloud2, f"/{ns}/depth_registered/points", QOS)
        self.last_rgb = None          # (h, w, bytes)
        self._rays = None

    def camera_info(self, stamp, w, h):
        i = self.info
        fx, fy = i["fx"] * w / i["width"], i["fy"] * h / i["height"]
        cx, cy = i["cx"] * w / i["width"], i["cy"] * h / i["height"]
        ci = CameraInfo()
        ci.header.stamp, ci.header.frame_id = stamp, self.frame
        ci.width, ci.height = w, h
        ci.distortion_model = "plumb_bob"
        ci.d = [0.0] * 5
        ci.k = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
        ci.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        ci.p = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        return ci

    def image(self, stamp, w, h, enc, data, step):
        m = Image()
        m.header.stamp, m.header.frame_id = stamp, self.frame
        m.height, m.width, m.encoding, m.is_bigendian, m.step = h, w, enc, 0, step
        m.data = data
        return m

    def points(self, stamp, depth_mm, w, h):
        ci = self.camera_info(stamp, w, h)
        fx, fy, cx, cy = ci.k[0], ci.k[4], ci.k[2], ci.k[5]
        if self._rays is None or self._rays[0].shape != (h, w):
            us, vs = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
            self._rays = ((us - cx) / fx, (vs - cy) / fy)
        z = depth_mm.astype(np.float32) / 1000.0
        z[z == 0] = np.nan
        x, y = self._rays[0] * z, self._rays[1] * z
        rgb = np.zeros((h, w), np.uint32)
        if self.last_rgb and self.last_rgb[0] == h and self.last_rgb[1] == w:
            c = np.frombuffer(self.last_rgb[2], np.uint8).reshape(h, w, 3).astype(np.uint32)
            rgb = (c[:, :, 0] << 16) | (c[:, :, 1] << 8) | c[:, :, 2]
        cloud = np.empty((h, w), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<u4")])
        cloud["x"], cloud["y"], cloud["z"], cloud["rgb"] = x, y, z, rgb
        pc = PointCloud2()
        pc.header.stamp, pc.header.frame_id = stamp, self.frame
        pc.height, pc.width = h, w
        pc.fields = [PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
                     PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
                     PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
                     PointField(name="rgb", offset=12, datatype=PointField.FLOAT32, count=1)]
        pc.is_bigendian, pc.point_step, pc.row_step = False, 16, 16 * w
        pc.is_dense = False
        pc.data = cloud.tobytes()
        return pc


class Bridge(Node):
    def __init__(self, host, port, only, fps):
        super().__init__("head_bridge")
        self.host, self.port, self.only, self.fps = host, port, only, fps
        self.cams = {}
        self.status = {}
        threading.Thread(target=self.loop, daemon=True).start()

    # ---------------------------------------------------------------- network
    def loop(self):
        while rclpy.ok():
            try:
                self.session()
            except (OSError, ConnectionError, ValueError) as e:
                self.get_logger().warn(f"head {self.host}:{self.port}: {e} — retrying in 2 s")
            time.sleep(2.0)

    def session(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(15)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        sock.sendall((json.dumps({"fps": self.fps, "color": True}) + "\n").encode())
        f = sock.makefile("rb", buffering=1 << 20)
        hello = json.loads(f.readline())
        for c in hello["cameras"]:
            if self.only and c["ns"] not in self.only:
                continue
            if c["ns"] not in self.cams:
                self.cams[c["ns"]] = Cam(self, c["ns"], c)
            else:
                self.cams[c["ns"]].info = c
        self.get_logger().info(
            f"connected to head {hello['host']} ({self.host}): "
            + ", ".join(f"{c['ns']}@{c['port']}" for c in hello["cameras"])
            + (f"; bridging {sorted(self.cams)}" if self.only else ""))
        while rclpy.ok():
            line = f.readline()
            if not line:
                raise ConnectionError("head closed the connection")
            msg = json.loads(line)
            if msg.get("type") == "status":
                self.status = msg.get("cameras", {})
                for ns, st in self.status.items():
                    cam = self.cams.get(ns)
                    if cam is not None and st.get("fx"):
                        cam.info.update({k: st[k] for k in ("fx", "fy", "cx", "cy", "width", "height")})
                continue
            payload = f.read(msg["n"])
            if len(payload) != msg["n"]:
                raise ConnectionError("short frame")
            cam = self.cams.get(msg["ns"])
            if cam is None:
                continue
            if cam.info.get("fx", 0) <= 0:      # hello sent before the camera opened
                continue
            stamp = self.get_clock().now().to_msg()
            w, h = msg["w"], msg["h"]
            if msg["kind"] == "rgb":
                cam.last_rgb = (h, w, payload)
                cam.pub_rgb.publish(cam.image(stamp, w, h, "rgb8", payload, w * 3))
                cam.pub_rgb_info.publish(cam.camera_info(stamp, w, h))
            else:
                cam.pub_depth.publish(cam.image(stamp, w, h, "16UC1", payload, w * 2))
                cam.pub_depth_info.publish(cam.camera_info(stamp, w, h))
                if cam.pub_points.get_subscription_count() > 0:
                    d = np.frombuffer(payload, np.uint16).reshape(h, w)
                    cam.pub_points.publish(cam.points(stamp, d, w, h))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--head", required=True, help="HOST or HOST:PORT of the xtion_head")
    ap.add_argument("--only", default="", help="comma-separated camera namespaces to bridge")
    ap.add_argument("--fps", type=float, default=15.0)
    args, ros_args = ap.parse_known_args(argv)
    host, _, port = args.head.partition(":")
    rclpy.init(args=ros_args)
    node = Bridge(host, int(port or 5599), set(filter(None, args.only.split(","))), args.fps)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
