#!/usr/bin/env python3
"""Station/sweep scanning — the Matterport-style capture pipeline (Stage 1).

Background: the Matterport Capture engine ("EOS") does NOT scan a room with
continuous handheld SLAM. It captures discrete 360° **sweeps** from fixed tripod
positions, then GLOBALLY registers them into one coordinate frame and fuses.
For a static room that is markedly higher quality than handheld drift. Full
reverse-engineering write-up: ~/Desktop/matterport work/analysis/FINDINGS.md.
This module brings that workflow to the multi-Xtion rig. See
MATTERPORT_REPLICATION_PLAN.md (Stage 1).

Workflow
--------
    # at each tripod position, after moving the rig:
    .venv/bin/python station_scan.py capture          # -> output/sweeps/sweep_00.ply
    .venv/bin/python station_scan.py capture          # -> output/sweeps/sweep_01.ply
    ...
    # then stitch every sweep into one room cloud:
    .venv/bin/python station_scan.py build             # -> output/room.ply
    .venv/bin/python station_scan.py list              # show captured sweeps

Each `capture`:
  1. brings up all cameras in cameras.json (multi_camera.launch.py),
  2. accumulates N frames per camera (capture_station.py) for temporal denoising,
  3. voxel-averages each camera cloud (kills structured-light jitter),
  4. fuses the cameras into ONE sweep using the calibrated extrinsics
     (output/camera_extrinsics.json — same data multi_camera.launch.py uses).

`build` registers sweeps pairwise (FPFH+RANSAC global -> point-to-plane ICP, the
same markerless approach as calibrate_multi.py), runs an Open3D pose-graph global
optimization (the open analogue of EOS's Ceres bundle adjustment), then fuses.
"""
import argparse
import glob
import json
import os
import subprocess

import numpy as np
import open3d as o3d
from open3d.pipelines import registration as reg
from scipy.spatial.transform import Rotation as R

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "scanner-ros:jazzy"
SWEEP_DIR = os.path.join(HERE, "output", "sweeps")
EXTR_PATH = os.path.join(HERE, "output", "camera_extrinsics.json")

VOXEL = 0.012          # m — fusion/averaging grid (matches calibrate_multi.py)
DEFAULT_FRAMES = 12    # depth frames stacked per camera per station


# --- frame math (shared convention with calibrate_multi.py) ------------------

def link_from_optical():
    """rgb_optical -> link transform (L). Same chain as calibrate_multi.py."""
    Tt = np.eye(4); Tt[:3, 3] = [0.0, -0.045, 0.0]
    Rm = np.eye(4)
    Rm[:3, :3] = R.from_euler("ZYX", [-np.pi / 2, 0.0, -np.pi / 2]).as_matrix()
    return Tt @ Rm


def roster():
    with open(os.path.join(HERE, "cameras.json")) as f:
        return json.load(f)["cameras"]


def load_extrinsics():
    """Return {ns: M} where M maps ns_link -> reference_link (ref = identity)."""
    out = {}
    if not os.path.exists(EXTR_PATH):
        return out
    with open(EXTR_PATH) as f:
        data = json.load(f)
    for ns, c in data.items():
        if ns.startswith("_") or not isinstance(c, dict):
            continue  # skip metadata keys (_comment, _method, …)
        if c.get("rejected"):
            continue
        M = np.eye(4)
        M[:3, :3] = R.from_quat([c["qx"], c["qy"], c["qz"], c["qw"]]).as_matrix()
        M[:3, 3] = [c["x"], c["y"], c["z"]]
        out[ns] = M
    return out


def optical_transform(M_link, L):
    """ref_link<-ns_link (M_link)  ->  ref_optical<-ns_optical."""
    return np.linalg.inv(L) @ M_link @ L


# --- capture -----------------------------------------------------------------

def _next_sweep_index():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    existing = sorted(glob.glob(os.path.join(SWEEP_DIR, "sweep_*.ply")))
    if not existing:
        return 0
    last = os.path.basename(existing[-1])
    return int(last[len("sweep_"):-len(".ply")]) + 1


def capture(frames):
    cams = roster()
    os.makedirs(SWEEP_DIR, exist_ok=True)
    idx = _next_sweep_index()
    raw_dir = os.path.join(SWEEP_DIR, f"sweep_{idx:02d}_cams")
    os.makedirs(raw_dir, exist_ok=True)

    rel = f"output/sweeps/sweep_{idx:02d}_cams"
    print(f"[station] sweep {idx:02d}: capturing {len(cams)} camera(s) SEQUENTIALLY "
          f"({frames} frames each)…")
    print("[station]   (one camera at a time — 3 Xtions can't share one USB-2 "
          "controller's bandwidth simultaneously)")

    # Bring up cameras ONE AT A TIME inside a single container. Each iteration sets
    # STATION_ONLY=<ns> so multi_camera.launch.py resolves+starts only that camera
    # (port->live address), captures N frames, then tears it down to free the USB
    # bus before the next. Logs persist to /scanner/ros_logs (the --rm /tmp is lost).
    # Per-camera capture as a shell function with a RETRY: an Xtion sometimes fails
    # to (re)open right after the previous camera's teardown ("Failed to set USB
    # interface" / device still releasing), so we relaunch it once with a longer
    # warmup before giving up.
    steps = []
    for c in cams:
        ns = c["ns"]
        out = f"/scanner/{rel}/{ns}.ply"
        topic = f"/{ns}/depth_registered/points"
        steps.append(
            f'echo "== {ns} ==" ; '
            f'for try in 1 2 ; do '
            f'STATION_ONLY={ns} ros2 launch /scanner/multi_camera.launch.py '
            f'> /scanner/ros_logs/station_{ns}.log 2>&1 & LP=$! ; '
            f'sleep $((12 + try*4)) ; '   # 16s, then 20s on retry
            f'if ros2 topic list 2>/dev/null | grep -q "{ns}/depth_registered/points" ; then '
            f'echo "   {ns} streaming (try $try)" ; '
            f'python3 /scanner/capture_station.py {frames} {out} {topic} ; '
            f'kill -INT $LP 2>/dev/null ; pkill -INT -f multi_camera 2>/dev/null ; sleep 4 ; break ; '
            f'else echo "   {ns} not up (try $try) — retrying" ; '
            f'kill -INT $LP 2>/dev/null ; pkill -INT -f multi_camera 2>/dev/null ; sleep 5 ; fi ; '
            f'done'
        )
    cmd = ("source /opt/ros/jazzy/setup.bash && mkdir -p /scanner/ros_logs && "
           + " ; ".join(steps))
    run = subprocess.run(
        ["sudo", "docker", "run", "--rm", "--privileged", "--network", "host",
         "-v", "/dev/bus/usb:/dev/bus/usb", "-v", f"{HERE}:/scanner",
         IMAGE, "bash", "-lc", cmd],
        capture_output=True, text=True, timeout=90 + len(cams) * 2 * (40 + 2 * frames))
    print(run.stdout[-1500:])
    if run.returncode != 0 and run.stderr:
        print(run.stderr[-300:])
    subprocess.run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}",
                    os.path.join(HERE, "output")], check=False)

    sweep = fuse_station(cams, raw_dir)
    if sweep is None:
        print("[station] FAILED: no camera clouds captured for this sweep.")
        return
    out = os.path.join(SWEEP_DIR, f"sweep_{idx:02d}.ply")
    o3d.io.write_point_cloud(out, sweep)
    print(f"[station] sweep {idx:02d}: {len(sweep.points)} pts -> {out}")
    print(f"[station] capture more, or run:  python3 station_scan.py build")


def fuse_station(cams, raw_dir):
    """Merge per-camera clouds (in raw_dir) into one sweep in the reference
    camera's optical frame, using calibrated extrinsics. Voxel-averages to
    denoise the stacked frames."""
    L = link_from_optical()
    extr = load_extrinsics()
    ref = cams[0]["ns"]
    merged = None
    for c in cams:
        ns = c["ns"]
        path = os.path.join(raw_dir, f"{ns}.ply")
        if not os.path.exists(path):
            print(f"   {ns}: no cloud — skipped")
            continue
        pcd = o3d.io.read_point_cloud(path)
        if len(pcd.points) == 0:
            print(f"   {ns}: empty cloud — skipped")
            continue
        # temporal denoise: many stacked frames -> per-voxel average
        pcd = pcd.voxel_down_sample(VOXEL)
        if ns != ref:
            M = extr.get(ns)
            if M is None:
                print(f"   {ns}: no extrinsics (run calibrate_multi.py) — skipped")
                continue
            pcd.transform(optical_transform(M, L))
        merged = pcd if merged is None else merged + pcd
    if merged is None:
        return None
    # final unify + light statistical outlier removal
    merged = merged.voxel_down_sample(VOXEL)
    merged, _ = merged.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    return merged


# --- build (global registration of sweeps) -----------------------------------

def _prep_fpfh(pcd, voxel):
    p = pcd.voxel_down_sample(voxel)
    p.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
    f = reg.compute_fpfh_feature(
        p, o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 5, max_nn=100))
    return p, f


def _prep_icp(pcd, voxel):
    p = pcd.voxel_down_sample(voxel)
    p.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
    return p


def pairwise(src, dst, voxel):
    """T mapping src onto dst, plus (fitness, rmse, info). FPFH+RANSAC global at a
    COARSE voxel (fast), refined by point-to-plane ICP at the fine voxel. Also
    tries an identity seed (station sweeps from a tripod that rotates near a
    common centre are roughly pre-aligned) and keeps whichever ICP fits best."""
    coarse = voxel * 3.0
    sc, scf = _prep_fpfh(src, coarse)
    dc, dcf = _prep_fpfh(dst, coarse)
    cdist = coarse * 1.5
    g = reg.registration_ransac_based_on_feature_matching(
        sc, dc, scf, dcf, True, cdist,
        reg.TransformationEstimationPointToPoint(False), 3,
        [reg.CorrespondenceCheckerBasedOnEdgeLength(0.9),
         reg.CorrespondenceCheckerBasedOnDistance(cdist)],
        reg.RANSACConvergenceCriteria(100000, 0.999))

    sf = _prep_icp(src, voxel)
    df = _prep_icp(dst, voxel)
    best = None
    for seed in (g.transformation, np.eye(4)):
        icp = reg.registration_icp(
            sf, df, voxel * 1.5, seed,
            reg.TransformationEstimationPointToPlane(),
            reg.ICPConvergenceCriteria(max_iteration=60))
        if best is None or icp.fitness > best.fitness:
            best = icp
    info = reg.get_information_matrix_from_point_clouds(sf, df, voxel * 1.5,
                                                        best.transformation)
    return best.transformation, best.fitness, best.inlier_rmse, info


def build(voxel, out_path):
    sweeps = sorted(glob.glob(os.path.join(SWEEP_DIR, "sweep_*.ply")))
    if len(sweeps) == 0:
        print("[build] no sweeps in output/sweeps/ — run `capture` first."); return
    print(f"[build] {len(sweeps)} sweep(s):")
    clouds = [o3d.io.read_point_cloud(s) for s in sweeps]
    for s, c in zip(sweeps, clouds):
        print(f"   {os.path.basename(s)}: {len(c.points)} pts")

    if len(sweeps) == 1:
        room = clouds[0].voxel_down_sample(voxel)
        o3d.io.write_point_cloud(out_path, room)
        print(f"[build] single sweep -> {out_path} ({len(room.points)} pts)")
        return

    # Pose graph (Open3D canonical multiway convention): pairwise(a, b) returns T
    # mapping a onto b. Consecutive pairs are odometry edges; every other pair
    # that fits well becomes an (uncertain) loop-closure edge. Node poses are the
    # inverse of the accumulated odometry, matching the fusion transform below.
    pg = reg.PoseGraph()
    odometry = np.eye(4)
    pg.nodes.append(reg.PoseGraphNode(odometry))
    LOOP_FIT = 0.4
    WEAK_FIT = 0.3
    n = len(clouds)
    weak = []
    for src in range(n):
        for tgt in range(src + 1, n):
            T, fit, rmse, info = pairwise(clouds[src], clouds[tgt], voxel)
            if tgt == src + 1:  # odometry
                odometry = T @ odometry
                pg.nodes.append(reg.PoseGraphNode(np.linalg.inv(odometry)))
                pg.edges.append(reg.PoseGraphEdge(src, tgt, T, info, uncertain=False))
                flag = "  <-- LOW OVERLAP" if fit < WEAK_FIT else ""
                print(f"   edge {src}->{tgt}: fit={fit:.2f} rmse={rmse*1000:.1f}mm (odometry){flag}")
                if fit < WEAK_FIT:
                    weak.append((src, tgt, fit))
            elif fit >= LOOP_FIT:  # loop closure
                pg.edges.append(reg.PoseGraphEdge(src, tgt, T, info, uncertain=True))
                print(f"   edge {src}->{tgt}: fit={fit:.2f} rmse={rmse*1000:.1f}mm (loop)")
    if weak:
        print(f"[build] WARNING: {len(weak)} consecutive sweep pair(s) registered with "
              f"low overlap (fit<{WEAK_FIT}): {[f'{a}->{b}({f:.2f})' for a,b,f in weak]}")
        print("[build]   -> those sweeps may be misaligned in the room. Re-capture them "
              "closer together / with more overlapping structure (aim for ~30%+ overlap).")

    opt = reg.GlobalOptimizationLevenbergMarquardt()
    crit = reg.GlobalOptimizationConvergenceCriteria()
    option = reg.GlobalOptimizationOption(
        max_correspondence_distance=voxel * 1.5,
        edge_prune_threshold=0.25, reference_node=0)
    print("[build] global pose-graph optimization (Ceres-BA analogue)…")
    reg.global_optimization(pg, opt, crit, option)

    room = o3d.geometry.PointCloud()
    for i, c in enumerate(clouds):
        cc = c.voxel_down_sample(voxel)
        cc.transform(pg.nodes[i].pose)
        room += cc
    room = room.voxel_down_sample(voxel)
    room, _ = room.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    o3d.io.write_point_cloud(out_path, room)
    print(f"[build] fused room -> {out_path} ({len(room.points)} pts)")
    print(f"[build] view with:  python3 view_cloud.py {out_path}")


def list_sweeps():
    sweeps = sorted(glob.glob(os.path.join(SWEEP_DIR, "sweep_*.ply")))
    if not sweeps:
        print("No sweeps yet. Run: python3 station_scan.py capture"); return
    print(f"{len(sweeps)} sweep(s) in {SWEEP_DIR}:")
    for s in sweeps:
        c = o3d.io.read_point_cloud(s)
        print(f"  {os.path.basename(s)}  {len(c.points)} pts")


def main():
    ap = argparse.ArgumentParser(description="Matterport-style station/sweep scanning")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="capture one sweep at the current position")
    cap.add_argument("--frames", type=int, default=DEFAULT_FRAMES,
                     help=f"depth frames stacked per camera (default {DEFAULT_FRAMES})")
    bld = sub.add_parser("build", help="register + fuse all sweeps into output/room.ply")
    bld.add_argument("--voxel", type=float, default=VOXEL, help="fusion voxel size (m)")
    bld.add_argument("--out", default=os.path.join(HERE, "output", "room.ply"))
    sub.add_parser("list", help="list captured sweeps")
    sub.add_parser("clear", help="delete all captured sweeps")
    args = ap.parse_args()

    if args.cmd == "capture":
        capture(args.frames)
    elif args.cmd == "build":
        build(args.voxel, args.out)
    elif args.cmd == "list":
        list_sweeps()
    elif args.cmd == "clear":
        import shutil
        if os.path.isdir(SWEEP_DIR):
            shutil.rmtree(SWEEP_DIR)
        print("[station] cleared all sweeps.")


if __name__ == "__main__":
    main()
