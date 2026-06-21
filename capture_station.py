#!/usr/bin/env python3
"""Station capture: grab N point-cloud frames per camera and ACCUMULATE them.

This is the Matterport-"sweep" primitive (see MATTERPORT_REPLICATION_PLAN.md):
at a fixed tripod position we don't trust a single structured-light frame — we
stack N frames per camera so the host can voxel-average away the per-pixel depth
jitter (≈ sqrt(N) noise reduction on the static scene). One PLY per camera is
written, still in that camera's native optical frame; station_scan.py fuses them
into a single sweep using the calibrated extrinsics.

Runs INSIDE the ROS container, after multi_camera.launch.py is up. Usage:
    python3 capture_station.py <frames> out1.ply /camera1/depth_registered/points \
                                        [out2.ply /camera2/depth_registered/points ...]
"""
import sys

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


def parse_cloud(msg):
    off = {f.name: f.offset for f in msg.fields}
    n = msg.width * msg.height
    buf = np.frombuffer(bytes(msg.data), dtype=np.uint8).reshape(n, msg.point_step)
    xyz = np.zeros((n, 3), np.float32)
    for i, k in enumerate(("x", "y", "z")):
        xyz[:, i] = buf[:, off[k]:off[k] + 4].copy().view("<f4").ravel()
    if "rgb" in off:
        packed = buf[:, off["rgb"]:off["rgb"] + 4].copy().view("<u4").ravel()
        rgb = np.empty((n, 3), np.uint8)
        rgb[:, 0] = (packed >> 16) & 0xFF
        rgb[:, 1] = (packed >> 8) & 0xFF
        rgb[:, 2] = packed & 0xFF
    else:
        rgb = np.full((n, 3), 200, np.uint8)
    good = np.isfinite(xyz).all(axis=1) & (np.abs(xyz).sum(axis=1) > 0)
    return xyz[good], rgb[good]


class GrabN(Node):
    """Accumulate `frames` clouds per topic, then write one stacked PLY each."""

    def __init__(self, jobs, frames):
        super().__init__("grab_station")
        self.out = {topic: path for path, topic in jobs}
        self.frames = frames
        self.xyz = {t: [] for t in self.out}
        self.rgb = {t: [] for t in self.out}
        qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                         history=HistoryPolicy.KEEP_LAST)
        for topic in self.out:
            self.create_subscription(
                PointCloud2, topic, lambda m, t=topic: self._cb(m, t), qos)
        self.get_logger().info(
            f"Accumulating {frames} frame(s) each on {list(self.out)} …")

    def _cb(self, msg, topic):
        if len(self.xyz[topic]) >= self.frames:
            return
        xyz, rgb = parse_cloud(msg)
        if xyz.shape[0] == 0:
            return
        self.xyz[topic].append(xyz)
        self.rgb[topic].append(rgb)
        if len(self.xyz[topic]) == self.frames:
            self.get_logger().info(f"  {topic}: collected {self.frames} frames")

    def complete(self):
        return all(len(v) >= self.frames for v in self.xyz.values())

    def flush(self):
        ok = 0
        for topic, path in self.out.items():
            if not self.xyz[topic]:
                self.get_logger().warn(f"  {topic}: NO frames — skipped")
                continue
            xyz = np.concatenate(self.xyz[topic], axis=0)
            rgb = np.concatenate(self.rgb[topic], axis=0)
            write_ply(path, xyz, rgb)
            self.get_logger().info(
                f"  SAVED {xyz.shape[0]} pts ({len(self.xyz[topic])} frames) -> {path}")
            ok += 1
        return ok


def main():
    a = sys.argv[1:]
    frames = int(a[0])
    a = a[1:]
    jobs = [(a[i], a[i + 1]) for i in range(0, len(a) - 1, 2)]  # (out, topic)
    rclpy.init()
    node = GrabN(jobs, frames)
    try:
        ticks = 0
        # ~ frames * cameras frames to gather; allow generous ceiling
        while rclpy.ok() and not node.complete() and ticks < 50 * (frames + 2):
            rclpy.spin_once(node, timeout_sec=0.1)
            ticks += 1
        ok = node.flush()
    finally:
        node.destroy_node()
        rclpy.shutdown()
    sys.exit(0 if ok == len(jobs) else 2)


if __name__ == "__main__":
    main()
