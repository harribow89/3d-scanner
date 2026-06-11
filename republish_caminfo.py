#!/usr/bin/env python3
"""Re-publish the OpenNI2 RGB camera_info continuously, time-synced to the image.

The openni2 driver publishes /camera/rgb/camera_info only ONCE at stream start
(volatile QoS), so any node that subscribes later — notably RTAB-Map launched a
few seconds after the camera — never receives it and cannot build a camera model.

This node subscribes BEFORE the camera streams (start it first), latches the
one-shot real camera_info, and re-emits it on /camera/rgb/camera_info_sync for
every RGB frame, copying the image header (stamp + frame_id) so approx_sync in
RTAB-Map matches trivially. If the real camera_info is missed, it falls back to
PrimeSense PS1080 defaults (focal ~525 @ 640x480) so mapping is never hard-blocked.

Usage (inside the ROS container, started before the camera):
    python3 republish_caminfo.py
"""
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from sensor_msgs.msg import CameraInfo, Image

SRC_INFO = "/camera/rgb/camera_info"
SRC_IMG = "/camera/rgb/image_raw"
OUT_INFO = "/camera/rgb/camera_info_sync"


class CamInfoRepublisher(Node):
    def __init__(self):
        super().__init__("caminfo_republisher")
        self.info = None
        self.got_real = False
        self.frames = 0
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE,
                         history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(CameraInfo, SRC_INFO, self._info_cb, qos)
        self.create_subscription(Image, SRC_IMG, self._img_cb, qos)
        self.pub = self.create_publisher(CameraInfo, OUT_INFO, qos)
        self.get_logger().info(
            f"republisher up; subscribed to {SRC_INFO} (waiting for the one-shot)")

    def _info_cb(self, msg):
        if not self.got_real:
            self.info = msg
            self.got_real = True
            self.get_logger().info(
                f"captured REAL camera_info: {msg.width}x{msg.height} "
                f"fx={msg.k[0]:.1f} fy={msg.k[4]:.1f} cx={msg.k[2]:.1f} cy={msg.k[5]:.1f}")

    def _default_info(self, img):
        ci = CameraInfo()
        ci.width, ci.height = img.width, img.height
        f = 525.0 * (img.width / 640.0)
        cx, cy = img.width / 2.0, img.height / 2.0
        ci.k = [f, 0.0, cx, 0.0, f, cy, 0.0, 0.0, 1.0]
        ci.p = [f, 0.0, cx, 0.0, 0.0, f, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        ci.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        ci.distortion_model = "plumb_bob"
        ci.d = [0.0, 0.0, 0.0, 0.0, 0.0]
        return ci

    def _img_cb(self, img):
        ci = self.info if self.info is not None else self._default_info(img)
        ci.header = img.header  # sync stamp + frame_id to the image
        self.pub.publish(ci)
        self.frames += 1
        if self.frames % 90 == 1:
            src = "REAL" if self.got_real else "DEFAULT(525)"
            self.get_logger().info(f"publishing {OUT_INFO} [{src}] — {self.frames} frames")


def main():
    rclpy.init()
    node = CamInfoRepublisher()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
