#!/usr/bin/env python3
"""Capture ONE PointCloud2 message from the live stream and save a PLY.

Subscribes to the already-assembled cloud topic (default
/camera/depth_registered/points) instead of re-deprojecting the depth image.
This avoids depending on /camera/rgb/camera_info, which the OpenNI2 driver
leaves unpublished when no RGB calibration file is loaded.

Usage (inside the ROS container, with the camera already running):
    python3 capture_points_ros.py <out.ply> [cloud_topic]
"""
import sys
import struct

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import PointCloud2


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


class GrabCloud(Node):
    def __init__(self, out_path, topic):
        super().__init__("grab_one_pointcloud")
        self.out_path = out_path
        self.done = False
        qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                         history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(PointCloud2, topic, self._cloud, qos)
        self.get_logger().info(f"Waiting for a cloud on {topic} …")

    def _offsets(self, msg):
        off = {f.name: f.offset for f in msg.fields}
        return off

    def _cloud(self, msg):
        if self.done:
            return
        off = self._offsets(msg)
        if not all(k in off for k in ("x", "y", "z")):
            self.get_logger().warn(f"cloud missing xyz fields: {list(off)}")
            return
        n = msg.width * msg.height
        buf = np.frombuffer(bytes(msg.data), dtype=np.uint8).reshape(n, msg.point_step)
        xyz = np.zeros((n, 3), np.float32)
        for i, k in enumerate(("x", "y", "z")):
            xyz[:, i] = buf[:, off[k]:off[k] + 4].copy().view("<f4").ravel()
        # rgb packed as float32/uint32 (0x00RRGGBB) when present
        if "rgb" in off:
            packed = buf[:, off["rgb"]:off["rgb"] + 4].copy().view("<u4").ravel()
            rgb = np.empty((n, 3), np.uint8)
            rgb[:, 0] = (packed >> 16) & 0xFF
            rgb[:, 1] = (packed >> 8) & 0xFF
            rgb[:, 2] = packed & 0xFF
        else:
            rgb = np.full((n, 3), 200, np.uint8)
        # drop NaN / zero points
        good = np.isfinite(xyz).all(axis=1) & (np.abs(xyz).sum(axis=1) > 0)
        xyz, rgb = xyz[good], rgb[good]
        if xyz.shape[0] == 0:
            self.get_logger().warn("cloud all-invalid; waiting…")
            return
        write_ply(self.out_path, xyz, rgb)
        z = xyz[:, 2]
        self.get_logger().info(
            f"SAVED {xyz.shape[0]} points -> {self.out_path} | "
            f"z {z.min():.3f}..{z.max():.3f} m, median {np.median(z):.3f} m"
        )
        self.done = True


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/scanner/output/object_capture.ply"
    topic = sys.argv[2] if len(sys.argv) > 2 else "/camera/depth_registered/points"
    rclpy.init()
    node = GrabCloud(out, topic)
    try:
        ticks = 0
        while rclpy.ok() and not node.done and ticks < 100:
            rclpy.spin_once(node, timeout_sec=0.2)
            ticks += 1
    finally:
        node.destroy_node()
        rclpy.shutdown()
    sys.exit(0 if node.done else 2)


if __name__ == "__main__":
    main()
