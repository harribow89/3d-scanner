#!/usr/bin/env python3
"""Bring up N ASUS Xtions (roster in cameras.json) as independent ROS camera
streams, each with its own XYZRGB point cloud, plus per-camera static TFs placing
them in a common frame using the markerless-calibrated extrinsics.

  - cameras.json           : roster [{ns, dev}, ...]; first entry is the reference
  - output/camera_extrinsics.json : {ns: {x,y,z,qx,qy,qz,qw}} from calibrate_multi.py
                                     (reference cam is identity; missing -> identity)

The stock openni2 launch has no device_id, so this sets it per driver (by OpenNI2
bus URI). Modes are forced low (QVGA) so multiple streams share a USB controller.

USB note: each Xtion wants a good slice of USB-2 bandwidth. 2 fit one USB-2
controller at QVGA here; for 3, spread them across controllers (use USB-3 ports,
which are a separate controller) or expect the extra one to fail to start.
"""
import json
import os

import launch
import launch_ros.actions
import launch_ros.descriptions
from launch.actions import TimerAction
from launch_ros.actions import Node

HERE = os.path.dirname(__file__)
ROSTER = os.path.join(HERE, "cameras.json")
EXTR = os.path.join(HERE, "output", "camera_extrinsics.json")
_OPT = ["-1.5707963267948966", "-1.5707963267948966"]  # roll, yaw (optical)
# Stamp frames with the device clock by default (good for single-camera). For
# MULTI-CAMERA SLAM set PREVIEW_DEVICE_TIME=0 so every camera stamps with the
# shared ROS clock — otherwise rtabmap can't sync 3 independent device clocks
# together ("Did not receive data").
_USE_DEV_TIME = os.environ.get("PREVIEW_DEVICE_TIME", "1") == "1"


def camera_graph(ns, device_id, depth_mode, color_mode, depth_only=False):
    # depth_only mode: no color stream, no depth->color registration. This halves
    # USB bandwidth and drops the colour isochronous endpoint (the one dmesg shows
    # failing with "cannot get freq at ep" when multiple Xtions share a USB-2 bus),
    # so it's the mode used for the live multi-camera OVERLAY preview. The cloud is
    # grey (geometry only) and lives in <ns>_depth_optical_frame.
    if depth_only:
        nodes = [
            launch_ros.descriptions.ComposableNode(
                package="openni2_camera", plugin="openni2_wrapper::OpenNI2Driver",
                name="driver", namespace=ns,
                parameters=[{"device_id": device_id},
                            {"depth_mode": depth_mode},
                            {"depth_registration": False},
                            {"use_device_time": _USE_DEV_TIME},
                            {"depth_frame_id": ns + "_depth_optical_frame"},
                            {"ir_frame_id": ns + "_ir_optical_frame"}],
            ),
            launch_ros.descriptions.ComposableNode(
                package="depth_image_proc", plugin="depth_image_proc::PointCloudXyzNode",
                name="points_xyz", namespace=ns,
                parameters=[{"queue_size": 5}],
                remappings=[("image_rect", "depth/image"),
                            ("camera_info", "depth/camera_info"),
                            ("points", "depth_registered/points")],
            ),
        ]
    else:
        nodes = [
            launch_ros.descriptions.ComposableNode(
                package="openni2_camera", plugin="openni2_wrapper::OpenNI2Driver",
                name="driver", namespace=ns,
                parameters=[{"device_id": device_id},
                            {"depth_mode": depth_mode},
                            {"color_mode": color_mode},
                            {"depth_registration": True},
                            {"use_device_time": _USE_DEV_TIME},
                            {"rgb_frame_id": ns + "_rgb_optical_frame"},
                            {"depth_frame_id": ns + "_depth_optical_frame"},
                            {"ir_frame_id": ns + "_ir_optical_frame"}],
                remappings=[("depth/image", "depth_registered/image_raw")],
            ),
            launch_ros.descriptions.ComposableNode(
                package="depth_image_proc", plugin="depth_image_proc::PointCloudXyzrgbNode",
                name="points_xyzrgb", namespace=ns,
                parameters=[{"queue_size": 10}],
                remappings=[("rgb/image_rect_color", "rgb/image_raw"),
                            ("rgb/camera_info", "rgb/camera_info"),
                            ("depth_registered/image_rect", "depth_registered/image_raw"),
                            ("points", "depth_registered/points")],
            ),
        ]
    container = launch_ros.actions.ComposableNodeContainer(
        name="container", namespace=ns,
        package="rclcpp_components", executable="component_container",
        composable_node_descriptions=nodes,
        output="screen",
    )

    def stf(parent, child, args):
        return Node(package="tf2_ros", executable="static_transform_publisher",
                    name=f"stf_{child}", output="screen",
                    arguments=["--frame-id", parent, "--child-frame-id", child] + args)

    tfs = [
        stf(f"{ns}_link", f"{ns}_depth_frame", ["--y", "-0.02"]),
        stf(f"{ns}_link", f"{ns}_rgb_frame", ["--y", "-0.045"]),
        stf(f"{ns}_depth_frame", f"{ns}_depth_optical_frame", ["--roll", _OPT[0], "--yaw", _OPT[1]]),
        stf(f"{ns}_rgb_frame", f"{ns}_rgb_optical_frame", ["--roll", _OPT[0], "--yaw", _OPT[1]]),
    ]
    return [container] + tfs


def present_xtions():
    """Scan sysfs for every connected Xtion PRO LIVE (1d27:0601) -> {port: uri},
    sorted by port path. The bus/address in the URI changes on every
    re-enumeration, so we read the live busnum/devnum here at launch."""
    base = "/sys/bus/usb/devices"
    found = {}
    try:
        names = os.listdir(base)
    except OSError:
        return found
    for name in names:
        d = os.path.join(base, name)
        try:
            with open(os.path.join(d, "idVendor")) as f:
                if f.read().strip() != "1d27":
                    continue
            with open(os.path.join(d, "idProduct")) as f:
                if f.read().strip() != "0601":
                    continue
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read().strip())
            with open(os.path.join(d, "devnum")) as f:
                addr = int(f.read().strip())
        except OSError:
            continue
        found[name] = f"1d27/0601@{bus}/{addr}"
    return dict(sorted(found.items()))


def assign_devices(cams):
    """Map each roster camera to a CURRENT OpenNI2 device URI, self-healing across
    replugs. A camera's pinned 'port' is honoured when that port is actually
    present (preserves the intended fan order); when a pinned port is missing
    (cameras got replugged into different sockets), the camera is assigned from
    the pool of detected-but-unclaimed Xtions in sorted-port order so the preview
    still comes up. Empty device_id is never emitted — that makes multiple drivers
    fight over the same device ("Failed to set USB interface!")."""
    present = present_xtions()
    assigned, claimed = {}, set()
    for cam in cams:                       # pass 1: pinned ports that exist
        port = cam.get("port")
        if port and port in present:
            assigned[cam["ns"]] = present[port]
            claimed.add(port)
    pool = [p for p in present if p not in claimed]
    for cam in cams:                       # pass 2: fill the rest from the pool
        if cam["ns"] in assigned:
            continue
        if pool:
            port = pool.pop(0)
            assigned[cam["ns"]] = present[port]
            print(f"[multi] NOTE: {cam['ns']} pinned port {cam.get('port')!r} not "
                  f"present; auto-assigned Xtion at port {port} ({present[port]}). "
                  f"If the fused sweep fans the wrong way, set this port in cameras.json.")
        else:
            assigned[cam["ns"]] = cam.get("dev", "")
            print(f"[multi] WARNING: no free Xtion for {cam['ns']}; "
                  f"using {assigned[cam['ns']]!r} (preview will likely fail to open it)")
    return assigned


def mount_args(extr, ns):
    c = extr.get(ns)
    if not c:
        return ["--x", "0", "--y", "0", "--z", "0"]  # identity (uncalibrated)
    return ["--x", str(c["x"]), "--y", str(c["y"]), "--z", str(c["z"]),
            "--qx", str(c["qx"]), "--qy", str(c["qy"]),
            "--qz", str(c["qz"]), "--qw", str(c["qw"])]


def generate_launch_description():
    with open(ROSTER) as f:
        roster = json.load(f)
    cams = roster["cameras"]
    dmode, cmode = roster.get("depth_mode", "QVGA_30Hz"), roster.get("color_mode", "QVGA_30Hz")
    # Live-overlay preview knobs (set by the GUI / station tooling):
    #   PREVIEW_DEPTH_ONLY=1   -> grey depth-only clouds, no colour (half bandwidth)
    #   PREVIEW_DMODE=QQVGA_30Hz -> override depth resolution (e.g. to fit 3 on one bus)
    #   PREVIEW_STAGGER=6      -> seconds between each camera's USB open (see below)
    depth_only = os.environ.get("PREVIEW_DEPTH_ONLY") == "1"
    dmode = os.environ.get("PREVIEW_DMODE", dmode)
    stagger = float(os.environ.get("PREVIEW_STAGGER", "6"))
    extr = {}
    if os.path.exists(EXTR):
        with open(EXTR) as f:
            extr = json.load(f)

    # STATION_ONLY=camera1[,camera2…] brings up only those cameras — station_scan.py
    # uses this to capture one camera at a time (3 Xtions exceed one USB-2 controller).
    only = os.environ.get("STATION_ONLY")
    if only:
        wanted = set(only.split(","))
        cams = [c for c in cams if c["ns"] in wanted]

    devmap = assign_devices(cams)
    ref = cams[0]["ns"]
    nodes = []
    for i, cam in enumerate(cams):
        graph = camera_graph(cam["ns"], devmap[cam["ns"]], dmode, cmode, depth_only)
        container, tfs = graph[0], graph[1:]
        # STAGGERED USB OPEN: starting all the OpenNI2 drivers at once makes them
        # all try to reserve their USB interface simultaneously, and on a shared
        # USB-2 controller the later ones lose the race ("Failed to set USB
        # interface!") — so only 1-2 of 3 Xtions come up. Opening them one at a
        # time, a few seconds apart, lets each reservation settle before the next
        # (the same reason station_scan.py brings cameras up sequentially). The
        # reference camera (i=0) starts immediately; the rest are delayed by
        # i*stagger seconds. TFs are cheap and publish right away.
        if i == 0 or stagger <= 0:
            nodes.append(container)
        else:
            nodes.append(TimerAction(period=i * stagger, actions=[container]))
        nodes += tfs
        if cam["ns"] != ref:
            # reference_link -> this_camera_link (calibrated, else identity)
            nodes.append(Node(
                package="tf2_ros", executable="static_transform_publisher",
                name=f"stf_{ref}_to_{cam['ns']}", output="screen",
                arguments=["--frame-id", f"{ref}_link", "--child-frame-id", f"{cam['ns']}_link"]
                + mount_args(extr, cam["ns"]),
            ))
    return launch.LaunchDescription(nodes)
