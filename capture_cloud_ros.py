#!/usr/bin/env python3
"""Capture ONE point cloud from a fixed Xtion and save a PLY.

Robust path: subscribe directly to the registered depth image + camera_info and
deproject with the pinhole model. Subscribing to the driver's own depth topic
forces the stream to start (the openni2 driver publishes lazily), so this does
not depend on the flaky depth_image_proc point-cloud chain.

Usage (inside the ROS container):
    python3 capture_cloud_ros.py <out.ply>
"""
import sys

import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, CameraInfo

DEPTH_TOPIC = "/camera/depth_registered/image_raw"  # 16UC1 (mm) or 32FC1 (m)
RGB_TOPIC = "/camera/rgb/image_raw"                  # rgb8
INFO_TOPIC = "/camera/rgb/camera_info"


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


class GrabOne(Node):
    def __init__(self, out_path):
        super().__init__("grab_one_cloud")
        self.out_path = out_path
        self.K = None
        self.rgb = None
        self.done = False
        self.create_subscription(CameraInfo, INFO_TOPIC, self._info, 10)
        self.create_subscription(Image, RGB_TOPIC, self._rgb, 10)
        self.create_subscription(Image, DEPTH_TOPIC, self._depth, 10)
        self.get_logger().info(f"Waiting for depth on {DEPTH_TOPIC} …")

    def _info(self, msg):
        self.K = (msg.k[0], msg.k[4], msg.k[2], msg.k[5])  # fx, fy, cx, cy

    def _rgb(self, msg):
        if msg.encoding in ("rgb8", "bgr8"):
            img = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width, 3)
            self.rgb = img[:, :, ::-1] if msg.encoding == "bgr8" else img

    def _depth(self, msg):
        if self.done or self.K is None:
            return
        fx, fy, cx, cy = self.K
        # Registered depth can arrive as 16UC1 (millimetres) or 32FC1 (metres)
        # depending on whether depth_image_proc emits raw or float-registered.
        if msg.encoding == "32FC1":
            z = np.frombuffer(msg.data, np.float32).reshape(msg.height, msg.width).copy()
        elif msg.encoding in ("16UC1", "mono16"):
            depth = np.frombuffer(msg.data, np.uint16).reshape(msg.height, msg.width).astype(np.float32)
            z = depth / 1000.0  # mm -> m
        else:
            self.get_logger().warn(f"unexpected depth encoding {msg.encoding!r}; skipping")
            return
        z[~np.isfinite(z)] = 0.0  # NaN/inf (no return) -> drop
        vs, us = np.where(z > 0)
        if us.size == 0:
            self.get_logger().warn("depth frame all-zero; waiting…")
            return
        zz = z[vs, us]
        xx = (us - cx) * zz / fx
        yy = (vs - cy) * zz / fy
        xyz = np.stack([xx, yy, zz], axis=1)
        if self.rgb is not None and self.rgb.shape[:2] == z.shape:
            rgb = self.rgb[vs, us]
        else:
            rgb = np.full((us.size, 3), 200, np.uint8)
        write_ply(self.out_path, xyz, rgb)
        self.get_logger().info(
            f"SAVED {us.size} points -> {self.out_path} | "
            f"depth {zz.min():.3f}..{zz.max():.3f} m, median {np.median(zz):.3f} m | "
            f"fill {100*us.size/(z.size):.1f}%"
        )
        self.done = True


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/scanner/output/single_frame.ply"
    rclpy.init()
    node = GrabOne(out)
    try:
        while rclpy.ok() and not node.done:
            rclpy.spin_once(node, timeout_sec=1.0)
    finally:
        node.destroy_node()
        rclpy.shutdown()
    sys.exit(0 if node.done else 2)


if __name__ == "__main__":
    main()
