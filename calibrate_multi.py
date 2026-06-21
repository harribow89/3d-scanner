#!/usr/bin/env python3
"""Markerless extrinsic auto-calibration for the multi-Xtion rig.

For each non-reference camera it recovers the full 6-DOF transform to the
reference purely from the scene — no printed target, no manual nudging:

  1. captures one denoised cloud per camera, ONE CAMERA AT A TIME (sequential —
     3 Xtions can't stream together on one USB-2 bus); see capture_all()
  2. FPFH + RANSAC GLOBAL registration  -> coarse pose (no initial guess needed,
     feature-based so it does NOT slide along depth like plain ICP)
  3. point-to-plane ICP refine
  4. CHAINED composition: each camera is registered to the PREVIOUS one in the
     roster (wide fans have no cam1<->cam3 overlap), then composed back to the
     reference
  5. converts optical-frame transform -> reference_link -> camera_link and writes
     output/camera_extrinsics.json, which multi_camera.launch.py then uses

Needs OVERLAP between ADJACENT cameras (cam1&cam2, cam2&cam3). Aim them at a
shared static scene with real 3-D structure (a room corner / cluttered shelf,
not a blank wall) ~1-1.5 m away, with cam2 bridging cam1 and cam3, then:

    .venv/bin/python calibrate_multi.py            # live: sequential capture + solve
    .venv/bin/python calibrate_multi.py --offline [dir]   # solve from saved clouds
    .venv/bin/python fuse_check.py && python3 view_cloud.py output/alignment_check.ply
"""
import json
import os
import subprocess

import numpy as np
import open3d as o3d
from open3d.pipelines import registration as reg
from scipy.spatial.transform import Rotation as R

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "scanner-ros:jazzy"
VOXEL = 0.012  # m


def link_from_optical():
    """Known static chain link -> rgb_frame(y=-0.045) -> rgb_optical(roll/yaw=-90)."""
    Tt = np.eye(4); Tt[:3, 3] = [0.0, -0.045, 0.0]
    Rm = np.eye(4)
    Rm[:3, :3] = R.from_euler("ZYX", [-np.pi / 2, 0.0, -np.pi / 2]).as_matrix()
    return Tt @ Rm


def roster():
    with open(os.path.join(HERE, "cameras.json")) as f:
        return json.load(f)["cameras"]


def capture_all(cams, frames=10):
    """Capture ONE denoised cloud per camera, ONE CAMERA AT A TIME.

    3 Xtions can't stream together on a single USB-2 controller, so we don't even
    try here — calibration only needs one good cloud of the (static) scene per
    camera. Each camera is brought up alone via STATION_ONLY, `frames` frames are
    stacked/denoised by capture_station.py, then it's torn down before the next
    (same proven sequence as station_scan.py, incl. a retry with a longer warmup
    when an Xtion fails to (re)open right after the previous one's teardown).
    Writes output/cal_<ns>.ply (one per camera, each in its own optical frame)."""
    steps = []
    for c in cams:
        ns = c["ns"]
        out = f"/scanner/output/cal_{ns}.ply"
        topic = f"/{ns}/depth_registered/points"
        steps.append(
            f'echo "[calib] === {ns} ===" ; for t in 1 2 ; do '
            f'STATION_ONLY={ns} ros2 launch /scanner/multi_camera.launch.py '
            f'> /scanner/ros_logs/cal_{ns}.log 2>&1 & LP=$! ; '
            f'sleep $((12 + t*4)) ; '
            f'if ros2 topic list 2>/dev/null | grep -q "{ns}/depth_registered/points" ; then '
            f'python3 /scanner/capture_station.py {frames} {out} {topic} ; '
            f'kill -INT $LP 2>/dev/null ; pkill -INT -f multi_camera 2>/dev/null ; sleep 4 ; break ; '
            f'else echo "[calib] {ns}: topic not up (try $t), retrying" ; '
            f'kill -INT $LP 2>/dev/null ; pkill -INT -f multi_camera 2>/dev/null ; sleep 5 ; fi ; done')
    cmd = "source /opt/ros/jazzy/setup.bash ; " + " ; ".join(steps)
    print(f"[calib] sequential capture of {len(cams)} camera(s), one at a time "
          f"({frames} frames each)…")
    run = subprocess.run(
        ["sudo", "docker", "run", "--rm", "--privileged", "--network", "host",
         "-v", "/dev/bus/usb:/dev/bus/usb", "-v", f"{HERE}:/scanner",
         IMAGE, "bash", "-lc", cmd],
        capture_output=True, text=True, timeout=90 * len(cams) + 60)
    print(run.stdout[-1500:]); print(run.stderr[-300:])
    subprocess.run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}",
                    os.path.join(HERE, "output")], check=False)


def _prep(pcd):
    p = pcd.voxel_down_sample(VOXEL)
    p.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=VOXEL * 2, max_nn=30))
    f = reg.compute_fpfh_feature(
        p, o3d.geometry.KDTreeSearchParamHybrid(radius=VOXEL * 5, max_nn=100))
    return p, f


def register(src_path, dst_path):
    """Return (T, fitness, rmse) with T mapping src cloud onto dst (both optical)."""
    src = o3d.io.read_point_cloud(src_path)
    dst = o3d.io.read_point_cloud(dst_path)
    s, sf = _prep(src)
    d, df = _prep(dst)
    dist = VOXEL * 1.5
    g = reg.registration_ransac_based_on_feature_matching(
        s, d, sf, df, True, dist,
        reg.TransformationEstimationPointToPoint(False), 3,
        [reg.CorrespondenceCheckerBasedOnEdgeLength(0.9),
         reg.CorrespondenceCheckerBasedOnDistance(dist)],
        reg.RANSACConvergenceCriteria(200000, 0.999))
    fine = reg.registration_icp(
        s, d, VOXEL * 1.2, g.transformation,
        reg.TransformationEstimationPointToPlane(),
        reg.ICPConvergenceCriteria(max_iteration=80))
    return fine.transformation, fine.fitness, fine.inlier_rmse


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    # OFFLINE mode: skip the live capture and register already-saved per-camera
    # clouds, named <ns>.ply, from a directory (default: the first station sweep's
    # raw per-camera clouds). Lets us solve real extrinsics from data already on
    # disk with no cameras/USB:  .venv/bin/python calibrate_multi.py --offline
    offline_dir = None
    if argv and argv[0] in ("--offline", "-o"):
        offline_dir = (argv[1] if len(argv) > 1 else
                       os.path.join(HERE, "output", "sweeps", "sweep_00_cams"))

    def cloud_path(ns):
        if offline_dir:
            return os.path.join(offline_dir, f"{ns}.ply")
        return os.path.join(HERE, "output", f"cal_{ns}.ply")

    cams = roster()
    if len(cams) < 2:
        print("[calib] need >= 2 cameras in cameras.json"); return
    if offline_dir:
        print(f"[calib] OFFLINE mode: registering saved clouds in {offline_dir}")
    else:
        capture_all(cams)

    ref = cams[0]["ns"]
    ref_ply = cloud_path(ref)
    if not os.path.exists(ref_ply):
        print(f"[calib] FAILED: no cloud for reference camera {ref} at {ref_ply}"); return

    L = link_from_optical()
    Linv = np.linalg.inv(L)
    extr = {ref: {"x": 0.0, "y": 0.0, "z": 0.0,
                  "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0}}
    # CHAINED registration: a wide fan rig has little/no overlap between the end
    # cameras (cam1<->cam3), so we register each camera to the PREVIOUS one in the
    # roster (which it DOES overlap) and compose the transform back to the
    # reference. Roster order must be physical fan order (see cameras.json).
    #   T_to_ref[ns] : ref_opt <- ns_opt  (composed)
    T_to_ref = {ref: np.eye(4)}
    prev_ns, prev_ply = ref, ref_ply
    print(f"\n[calib] reference = {ref}  (chained pairwise registration)")
    for c in cams[1:]:
        ns = c["ns"]
        src = cloud_path(ns)
        if not os.path.exists(src):
            print(f"  {ns}: NO CLOUD at {src} — skipped")
            continue
        T_pair, fit, rmse = register(src, prev_ply)        # prev_opt <- ns_opt
        T_ns = T_to_ref[prev_ns] @ T_pair                  # ref_opt  <- ns_opt
        M = L @ T_ns @ Linv                                # ref_link <- ns_link
        t = M[:3, 3]
        q = R.from_matrix(M[:3, :3]).as_quat()
        rpy = R.from_matrix(M[:3, :3]).as_euler("xyz", degrees=True)
        # Sanity gate on the PAIRWISE fit (the composed cam-vs-ref overlap is
        # meaningless for a fan). A real adjacent-pair transform has good overlap
        # and a physically small composed baseline.
        ok = fit >= 0.4 and np.linalg.norm(t) <= 0.8
        print(f"  {ns} (vs {prev_ns}): fit={fit:.2f} rmse={rmse*1000:.1f}mm | "
              f"t(mm)=[{t[0]*1000:+.0f},{t[1]*1000:+.0f},{t[2]*1000:+.0f}] "
              f"rpy(deg)=[{rpy[0]:+.1f},{rpy[1]:+.1f},{rpy[2]:+.1f}]")
        if ok:
            extr[ns] = {"x": float(t[0]), "y": float(t[1]), "z": float(t[2]),
                        "qx": float(q[0]), "qy": float(q[1]),
                        "qz": float(q[2]), "qw": float(q[3]),
                        "fitness": float(fit), "rmse_mm": float(rmse * 1000)}
            T_to_ref[ns] = T_ns
            prev_ns, prev_ply = ns, src      # advance the chain only on success
        else:
            extr[ns] = {"x": 0.0, "y": 0.0, "z": 0.0,
                        "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0, "rejected": True}
            print(f"       REJECTED (fit<0.4 or |t|>0.8m) -> kept identity. "
                  f"Needs real overlap with {prev_ns} + structure; aim adjacent "
                  f"cameras at a shared scene. Chain stays anchored at {prev_ns}.")

    out = os.path.join(HERE, "output", "camera_extrinsics.json")
    with open(out, "w") as f:
        json.dump(extr, f, indent=2)
    print(f"\n[calib] wrote {out}\n[calib] verify with:  ./run_scanner_docker.sh multi")


if __name__ == "__main__":
    main()
