#!/usr/bin/env python3
"""fake_camera — a simulated RGB-D camera so the WHOLE pipeline (preview, snap,
RTAB-Map SLAM, export, cleanup, STL) can be exercised with NO hardware.

Runs INSIDE the ROS container (rclpy + numpy only) and publishes exactly what the
openni2 launch would:

    /<ns>/rgb/image_raw                  rgb8
    /<ns>/rgb/camera_info
    /<ns>/depth_registered/image_raw     16UC1 (millimetres), registered to RGB
    /<ns>/depth_registered/camera_info
    /<ns>/depth_registered/points        (NOT published; capture_cloud_ros.py
                                         deprojects depth itself)
    static TF  <ns>_link -> <ns>_rgb_frame -> <ns>_rgb_optical_frame
               <ns>_link -> <ns>_depth_frame -> <ns>_depth_optical_frame

Scene: a textured wall 2.2 m away with a textured box face 1.1 m in front of
it; the camera pans/sways slowly so visual odometry has parallax to track and
RTAB-Map adds keyframes and finds loop closures.

    python3 tests/fake_camera.py [--ns camera] [--hz 10] [--width 640 --height 480]
                                 [--motion 1.0] [--seconds 0]
Enabled from the launcher with SCANNER_FAKE_CAMERA=1.
"""
import argparse
import math
import sys
import time

import numpy as np
import rclpy
from geometry_msgs.msg import TransformStamped
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo, Image
from tf2_ros import StaticTransformBroadcaster

WALL_Z = 2.2          # m, world frame (camera optical convention: z forward)
BOX_Z = 1.1
BOX_HALF = (0.32, 0.22)  # half extents (x, y) of the box face
TEX = 512


def make_texture(seed, blobs=60):
    """A fixed, feature-rich RGB texture (noise + coloured blobs) so visual
    odometry has plenty of corners to track."""
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 200, size=(TEX, TEX, 3), dtype=np.int16)
    # smooth a little so features are not single-pixel noise
    k = np.ones((3, 3)) / 9.0
    for c in range(3):
        ch = base[:, :, c].astype(np.float32)
        pad = np.pad(ch, 1, mode="wrap")
        acc = np.zeros_like(ch)
        for dy in range(3):
            for dx in range(3):
                acc += pad[dy:dy + TEX, dx:dx + TEX] * k[dy, dx]
        base[:, :, c] = acc
    yy, xx = np.mgrid[0:TEX, 0:TEX]
    for _ in range(blobs):
        cx, cy = rng.integers(0, TEX, 2)
        r = rng.integers(8, 40)
        col = rng.integers(0, 255, 3)
        d2 = ((xx - cx) ** 2 + (yy - cy) ** 2)
        base[d2 < r * r] = col
    return np.clip(base, 0, 255).astype(np.uint8)


class FakeCamera(Node):
    def __init__(self, ns, hz, width, height, motion):
        super().__init__("fake_camera", namespace=ns)
        self.ns = ns
        self.w, self.h = width, height
        self.motion = motion
        self.fx = self.fy = 525.0 * width / 640.0
        self.cx, self.cy = width / 2.0, height / 2.0
        self.t0 = time.time()
        self.frame = 0
        self.rgb_tex = make_texture(1)
        self.box_tex = make_texture(2, blobs=25)

        # Pixel rays (camera frame, z = 1)
        us, vs = np.meshgrid(np.arange(width, dtype=np.float32),
                             np.arange(height, dtype=np.float32))
        self.dx = (us - self.cx) / self.fx
        self.dy = (vs - self.cy) / self.fy

        self.pub_rgb = self.create_publisher(Image, "rgb/image_raw", 5)
        self.pub_rgb_info = self.create_publisher(CameraInfo, "rgb/camera_info", 5)
        self.pub_depth = self.create_publisher(Image, "depth_registered/image_raw", 5)
        self.pub_depth_info = self.create_publisher(CameraInfo, "depth_registered/camera_info", 5)
        self.tf_static = StaticTransformBroadcaster(self)
        self.publish_static_tf()
        self.timer = self.create_timer(1.0 / hz, self.tick)
        self.get_logger().info(
            f"fake camera '{ns}' {width}x{height} @ {hz} Hz, motion x{motion}")

    # ----------------------------------------------------------------- TF tree
    def publish_static_tf(self):
        def tf(parent, child, xyz, q):
            t = TransformStamped()
            t.header.stamp = self.get_clock().now().to_msg()
            t.header.frame_id = parent
            t.child_frame_id = child
            t.transform.translation.x, t.transform.translation.y, t.transform.translation.z = xyz
            t.transform.rotation.x, t.transform.rotation.y, t.transform.rotation.z, \
                t.transform.rotation.w = q
            return t
        ident = (0.0, 0.0, 0.0, 1.0)
        optical = (-0.5, 0.5, -0.5, 0.5)   # roll -90°, yaw -90° (ROS optical frame)
        ns = self.ns
        self.tf_static.sendTransform([
            tf(f"{ns}_link", f"{ns}_rgb_frame", (0.0, -0.045, 0.0), ident),
            tf(f"{ns}_link", f"{ns}_depth_frame", (0.0, -0.02, 0.0), ident),
            tf(f"{ns}_rgb_frame", f"{ns}_rgb_optical_frame", (0.0, 0.0, 0.0), optical),
            tf(f"{ns}_depth_frame", f"{ns}_depth_optical_frame", (0.0, 0.0, 0.0), optical),
        ])

    # ------------------------------------------------------------------ render
    def render(self, t):
        """Return (rgb uint8 HxWx3, depth_mm uint16 HxW) for time t."""
        m = self.motion
        tx = 0.25 * m * math.sin(0.35 * t)          # sway left/right
        ty = 0.08 * m * math.sin(0.23 * t + 1.0)    # bob
        yaw = 0.18 * m * math.sin(0.30 * t + 0.6)   # pan
        c, s = math.cos(yaw), math.sin(yaw)
        # ray in world frame (rotate about the vertical y axis)
        wx = c * self.dx + s * 1.0
        wz = -s * self.dx + c * 1.0
        wy = self.dy
        # intersect the wall plane z = WALL_Z
        t_wall = (WALL_Z - 0.0) / wz
        px = tx + t_wall * wx
        py = ty + t_wall * wy
        depth = t_wall.copy()                      # camera-frame z == ray parameter
        # float32 modulo can round to exactly TEX, so wrap again after the int cast
        tex_u = ((px * 220.0) % TEX).astype(np.int32) % TEX
        tex_v = ((py * 220.0) % TEX).astype(np.int32) % TEX
        rgb = self.rgb_tex[tex_v, tex_u]
        # box face at z = BOX_Z (in front of the wall)
        t_box = BOX_Z / wz
        bx = tx + t_box * wx
        by = ty + t_box * wy
        hit = (np.abs(bx) < BOX_HALF[0]) & (np.abs(by - 0.05) < BOX_HALF[1]) & (t_box > 0)
        depth[hit] = t_box[hit]
        bu = ((bx * 420.0) % TEX).astype(np.int32) % TEX
        bv = ((by * 420.0) % TEX).astype(np.int32) % TEX
        rgb = rgb.copy()
        rgb[hit] = self.box_tex[bv[hit], bu[hit]]
        # a little shading from distance + per-frame sensor noise
        shade = np.clip(1.15 - 0.12 * depth, 0.55, 1.0)[..., None]
        rgb = np.clip(rgb.astype(np.float32) * shade, 0, 255).astype(np.uint8)
        noise = np.random.default_rng(self.frame).normal(0.0, 0.002, depth.shape)
        depth_mm = np.clip((depth + noise) * 1000.0, 0, 65535).astype(np.uint16)
        # structured-light cameras have a border of no-data pixels
        depth_mm[:, :6] = 0
        depth_mm[:, -6:] = 0
        return rgb, depth_mm

    def camera_info(self, stamp, frame):
        ci = CameraInfo()
        ci.header.stamp = stamp
        ci.header.frame_id = frame
        ci.width, ci.height = self.w, self.h
        ci.distortion_model = "plumb_bob"
        ci.d = [0.0, 0.0, 0.0, 0.0, 0.0]
        ci.k = [self.fx, 0.0, self.cx, 0.0, self.fy, self.cy, 0.0, 0.0, 1.0]
        ci.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        ci.p = [self.fx, 0.0, self.cx, 0.0, 0.0, self.fy, self.cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        return ci

    def tick(self):
        t = time.time() - self.t0
        rgb, depth_mm = self.render(t)
        stamp = self.get_clock().now().to_msg()
        frame = f"{self.ns}_rgb_optical_frame"

        img = Image()
        img.header.stamp = stamp
        img.header.frame_id = frame
        img.height, img.width = self.h, self.w
        img.encoding = "rgb8"
        img.is_bigendian = 0
        img.step = self.w * 3
        img.data = rgb.tobytes()

        dep = Image()
        dep.header.stamp = stamp
        dep.header.frame_id = frame
        dep.height, dep.width = self.h, self.w
        dep.encoding = "16UC1"
        dep.is_bigendian = 0
        dep.step = self.w * 2
        dep.data = depth_mm.tobytes()

        self.pub_rgb.publish(img)
        self.pub_rgb_info.publish(self.camera_info(stamp, frame))
        self.pub_depth.publish(dep)
        self.pub_depth_info.publish(self.camera_info(stamp, frame))
        self.frame += 1
        if self.frame % 100 == 0:
            self.get_logger().info(f"published {self.frame} synthetic frames")


def main(argv=None):
    ap = argparse.ArgumentParser(description="simulated RGB-D camera")
    ap.add_argument("--ns", default="camera")
    # QVGA by default: the real rig runs the Xtions at QVGA too, and a 640x480 rgb8
    # frame (920 KB) is above what FastDDS moves without UDP fragmentation, which
    # made RTAB-Map drop frames ("Did not receive data since 5 seconds").
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--height", type=int, default=240)
    ap.add_argument("--motion", type=float, default=1.0, help="0 = static camera")
    ap.add_argument("--seconds", type=float, default=0.0, help="exit after N seconds")
    args, ros_args = ap.parse_known_args(argv)
    rclpy.init(args=ros_args)
    node = FakeCamera(args.ns, args.hz, args.width, args.height, args.motion)
    deadline = time.time() + args.seconds if args.seconds > 0 else None
    try:
        while rclpy.ok() and (deadline is None or time.time() < deadline):
            rclpy.spin_once(node, timeout_sec=0.1)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
