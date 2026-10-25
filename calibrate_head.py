#!/usr/bin/env python3
"""calibrate_head — extrinsic calibration of the multi-Xtion rig from RGB-D
FEATURE MATCHES, through the network sensor head (no ROS, no containers).

    .venv/bin/python calibrate_head.py [--head 127.0.0.1] [--seconds 4]

Why not calibrate_multi.py (FPFH shape matching)? The Matterport fan has only
partial overlap between neighbouring sensors and the Xtion's depth is noisy,
so pure geometry matching scored 0.05-0.31 and flipped between runs. The
colour images, however, share lots of texture. So, per camera pair:

  1. median-of-N depth (denoised), registered to the colour image;
  2. SIFT on the colour images, ratio test + mutual check;
  3. each match lifted to 3-D with its own camera's depth -> 3-D/3-D pairs;
  4. RANSAC rigid fit (Kabsch) -> T and an inlier count;
  5. point-to-plane ICP on the two clouds, seeded with T, kept only if it
     agrees with the features (it may only sharpen, never jump).

Every pair is tried, so the roster order does not have to be the physical fan
order: cameras are linked through a maximum spanning tree of inlier counts,
rooted at the roster's first camera. Writes output/camera_extrinsics.json in
the same format calibrate_multi.py does (so multi_camera.launch.py, fuse and
station_scan.py use it unchanged) and output/alignment_check.ply.

A pair needs >= MIN_INLIERS agreeing features. If one is missing, aim that
camera so it shares textured objects with a neighbour and rerun.
"""
import argparse
import json
import os
import socket
import sys
import time

import cv2
import numpy as np
import open3d as o3d
from open3d.pipelines import registration as reg
from scipy.spatial.transform import Rotation as R

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
MIN_INLIERS = 15               # agreeing 3-D feature pairs needed to trust a link
MAX_BASELINE_M = 0.30          # physically: the sensors sit a few cm apart
NEAR, FAR = 0.45, 4.5          # m: usable Xtion depth


# ------------------------------------------------------------------ capture
def grab(host, port, seconds):
    """{ns: {"rgb": HxWx3, "depth": HxW mm (median), "K": (fx, fy, cx, cy)}}"""
    sock = socket.create_connection((host, port), timeout=20)
    sock.settimeout(20)
    sock.sendall((json.dumps({"fps": 15, "color": True}) + "\n").encode())
    f = sock.makefile("rb", buffering=1 << 20)
    hello = json.loads(f.readline())
    info = {c["ns"]: c for c in hello["cameras"]}
    depths, rgb = {ns: [] for ns in info}, {}
    t0 = time.time()
    while time.time() - t0 < seconds or any(len(depths[n]) < 3 or n not in rgb for n in info):
        if time.time() - t0 > seconds + 20:
            break
        msg = json.loads(f.readline())
        if msg["type"] == "status":
            for ns, st in msg["cameras"].items():
                if st.get("fx") and ns in info:
                    info[ns].update({k: st[k] for k in ("fx", "fy", "cx", "cy")})
            continue
        buf = f.read(msg["n"])
        h, w = msg["h"], msg["w"]
        if msg["kind"] == "depth":
            depths[msg["ns"]].append(np.frombuffer(buf, np.uint16).reshape(h, w))
        else:
            rgb[msg["ns"]] = np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()
    sock.close()
    out = {}
    for ns, c in info.items():
        if not depths[ns] or ns not in rgb:
            print(f"  {ns}: no frames received — skipped")
            continue
        stack = np.stack(depths[ns]).astype(np.float32)
        stack[stack == 0] = np.nan
        with np.errstate(all="ignore"):
            med = np.nanmedian(stack, axis=0)
            spread = np.nanstd(stack, axis=0)
        med[np.isnan(med) | (spread > 25)] = 0          # drop flickering pixels
        out[ns] = {"rgb": rgb[ns], "depth": med.astype(np.uint16),
                   "K": (c["fx"], c["fy"], c["cx"], c["cy"]), "port": c.get("port")}
        print(f"  {ns} (USB {c.get('port')}): {len(depths[ns])} depth frames, "
              f"fill {(med > 0).mean() * 100:.0f} %")
    return out


# ------------------------------------------------------------------ geometry
def lift(cam, pts):
    """pixel coords (N,2) -> 3-D points (N,3) in the optical frame, NaN if no depth."""
    fx, fy, cx, cy = cam["K"]
    d = cam["depth"]
    h, w = d.shape
    out = np.full((len(pts), 3), np.nan)
    for i, (u, v) in enumerate(pts):
        ui, vi = int(round(u)), int(round(v))
        win = d[max(vi - 1, 0):min(vi + 2, h), max(ui - 1, 0):min(ui + 2, w)]
        win = win[win > 0]
        if win.size < 3:
            continue
        z = float(np.median(win)) / 1000.0
        if not NEAR <= z <= FAR:
            continue
        out[i] = ((u - cx) * z / fx, (v - cy) * z / fy, z)
    return out


def kabsch(a, b):
    """R, t minimising |R a + t - b|."""
    ca, cb = a.mean(0), b.mean(0)
    H = (a - ca).T @ (b - cb)
    U, _S, Vt = np.linalg.svd(H)
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    Rm = Vt.T @ D @ U.T
    return Rm, cb - Rm @ ca


def to_T(Rm, t):
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = Rm, t
    return T


def ransac_rigid(a, b, iters=4000, seed=0):
    rng = np.random.default_rng(seed)
    n = len(a)
    best = (None, np.zeros(n, bool))
    if n < 4:
        return best
    tol = 0.015 + 0.012 * b[:, 2]               # Xtion depth error grows with range
    for _ in range(iters):
        idx = rng.choice(n, 3, replace=False)
        Rm, t = kabsch(a[idx], b[idx])
        inl = np.linalg.norm((a @ Rm.T + t) - b, axis=1) < tol
        if inl.sum() > best[1].sum():
            best = (to_T(Rm, t), inl)
    if best[0] is not None and best[1].sum() >= 4:
        Rm, t = kabsch(a[best[1]], b[best[1]])     # refit on all inliers
        T = to_T(Rm, t)
        inl = np.linalg.norm((a @ Rm.T + t) - b, axis=1) < tol
        best = (T, inl)
    return best


def cloud(cam, voxel=0.008):
    fx, fy, cx, cy = cam["K"]
    d = cam["depth"].astype(np.float32) / 1000.0
    h, w = d.shape
    us, vs = np.meshgrid(np.arange(w), np.arange(h))
    m = (d > NEAR) & (d < FAR)
    z = d[m]
    xyz = np.stack([(us[m] - cx) * z / fx, (vs[m] - cy) * z / fy, z], 1)
    p = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz))
    p.colors = o3d.utility.Vector3dVector(cam["rgb"][m].astype(np.float64) / 255.0)
    p = p.voxel_down_sample(voxel)
    p.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 4, max_nn=30))
    return p


def solve_pair(a, b, sift):
    """T (b_opt <- a_opt), inlier count, detail string."""
    ga = cv2.cvtColor(a["rgb"], cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor(b["rgb"], cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(2.0, (8, 8))
    ka, da = sift.detectAndCompute(clahe.apply(ga), None)
    kb, db = sift.detectAndCompute(clahe.apply(gb), None)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return None, 0, "too few features"
    bf = cv2.BFMatcher(cv2.NORM_L2)
    ab = bf.knnMatch(da, db, k=2)
    ba = {m.queryIdx: m.trainIdx for m, *_ in bf.knnMatch(db, da, k=1)}
    good = [m for m, n2 in (p for p in ab if len(p) == 2)
            if m.distance < 0.8 * n2.distance and ba.get(m.trainIdx) == m.queryIdx]
    if len(good) < 6:
        return None, 0, f"{len(good)} feature matches"
    pa = lift(a, np.array([ka[m.queryIdx].pt for m in good]))
    pb = lift(b, np.array([kb[m.trainIdx].pt for m in good]))
    ok = np.isfinite(pa).all(1) & np.isfinite(pb).all(1)
    pa, pb = pa[ok], pb[ok]
    T, inl = ransac_rigid(pa, pb)
    n_inl = int(inl.sum()) if T is not None else 0
    if T is None or n_inl < 6:
        return None, n_inl, f"{len(good)} matches, {len(pa)} with depth, no consistent motion"
    # ICP refinement on the clouds — accepted only if it stays near the feature fit
    ca, cb = cloud(a), cloud(b)
    icp = reg.registration_icp(ca, cb, 0.03, T, reg.TransformationEstimationPointToPlane(),
                               reg.ICPConvergenceCriteria(max_iteration=60))
    dT = np.linalg.inv(T) @ icp.transformation
    drot = np.degrees(np.linalg.norm(R.from_matrix(dT[:3, :3]).as_rotvec()))
    dtr = np.linalg.norm(dT[:3, 3])
    resid = np.linalg.norm((pa[inl] @ icp.transformation[:3, :3].T + icp.transformation[:3, 3])
                           - pb[inl], axis=1)
    use_icp = drot < 3.0 and dtr < 0.04 and np.median(resid) < 0.03
    Tf = icp.transformation if use_icp else T
    detail = (f"{len(good)} matches, {n_inl} 3-D inliers, ICP "
              + (f"refined (overlap fit {icp.fitness:.2f}, rmse {icp.inlier_rmse * 1000:.0f} mm)"
                 if use_icp else f"rejected (moved {drot:.1f} deg / {dtr * 100:.1f} cm)"))
    return Tf, n_inl, detail


def depth_points(cam):
    fx, fy, cx, cy = cam["K"]
    d = cam["depth"].astype(np.float32) / 1000.0
    h, w = d.shape
    us, vs = np.meshgrid(np.arange(w), np.arange(h))
    m = (d > NEAR) & (d < FAR)
    z = d[m]
    return np.stack([(us[m] - cx) * z / fx, (vs[m] - cy) * z / fy, z], 1)


def seam(a, b, T, tol=0.05):
    """Project b's depth points into a's image with T (a_opt <- b_opt).
    Returns (overlap points, median |dz| m, share within 2 cm, matched a/b point arrays)."""
    pb = depth_points(b)
    pa_ = pb @ T[:3, :3].T + T[:3, 3]
    fx, fy, cx, cy = a["K"]
    da = a["depth"].astype(np.float32) / 1000.0
    h, w = da.shape
    z = pa_[:, 2]
    ok = z > NEAR
    u = np.round(pa_[ok, 0] * fx / z[ok] + cx).astype(int)
    v = np.round(pa_[ok, 1] * fy / z[ok] + cy).astype(int)
    inside = (u >= 0) & (u < w) & (v >= 0) & (v < h)
    idx = np.flatnonzero(ok)[inside]
    za = da[v[inside], u[inside]]
    dz = np.abs(za - z[idx])
    good = (za > NEAR) & (dz < tol)
    if good.sum() < 50:
        return int(good.sum()), float("nan"), 0.0, None, None
    dzg = dz[good]
    return int(good.sum()), float(np.median(dzg)), float((dzg < 0.02).mean()), \
        pb[idx[good]], None


def refine_on_seam(a, b, T):
    """ICP restricted to the points both cameras see; only accepted if it lowers
    the seam error and stays within 4 cm / 3 deg of the feature solution."""
    n0, e0, f0, pts_b, _ = seam(a, b, T)
    if pts_b is None or n0 < 300:
        return T, n0, e0, f0, "overlap too small to refine"
    src = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts_b)).voxel_down_sample(0.006)
    dst = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(depth_points(a))).voxel_down_sample(0.006)
    dst.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.03, max_nn=30))
    Tr = T
    for dist in (0.04, 0.025, 0.015):
        Tr = reg.registration_icp(src, dst, dist, Tr, reg.TransformationEstimationPointToPlane(),
                                  reg.ICPConvergenceCriteria(max_iteration=40)).transformation
    dT = np.linalg.inv(T) @ Tr
    drot = np.degrees(np.linalg.norm(R.from_matrix(dT[:3, :3]).as_rotvec()))
    dtr = np.linalg.norm(dT[:3, 3])
    n1, e1, f1, _p, _ = seam(a, b, Tr)
    if drot < 3.0 and dtr < 0.04 and n1 >= 0.8 * n0 and e1 < e0:
        return Tr, n1, e1, f1, f"seam-refined ({dtr * 100:.1f} cm, {drot:.1f} deg)"
    return T, n0, e0, f0, "seam refine rejected"


def link_from_optical():
    """Same chain as calibrate_multi.py: link -> rgb_frame(y=-0.045) -> rgb_optical."""
    Tt = np.eye(4)
    Tt[:3, 3] = [0.0, -0.045, 0.0]
    Rm = np.eye(4)
    Rm[:3, :3] = R.from_euler("ZYX", [-np.pi / 2, 0.0, -np.pi / 2]).as_matrix()
    return Tt @ Rm


# ---------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--head", default=os.environ.get("SCANNER_HEAD") or "127.0.0.1")
    ap.add_argument("--seconds", type=float, default=4.0)
    a = ap.parse_args(argv)
    host, _, port = a.head.partition(":")
    with open(os.path.join(HERE, "cameras.json")) as fh:
        roster = [c["ns"] for c in json.load(fh)["cameras"]]

    print(f"[calib] capturing {a.seconds:.0f} s from the sensor head at {host} — keep the rig still")
    cams = grab(host, int(port or 5599), a.seconds)
    names = [n for n in roster if n in cams] + [n for n in cams if n not in roster]
    if len(names) < 2:
        print("[calib] FAILED: need at least 2 cameras streaming"); return 2
    ref = names[0]

    sift = cv2.SIFT_create(nfeatures=4000, contrastThreshold=0.02)
    pairs, seams = {}, {}
    print("\n[calib] pairwise feature registration:")
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            T, n, detail = solve_pair(cams[y], cams[x], sift)     # x_opt <- y_opt
            if T is not None:
                T, ov, err, within, how = refine_on_seam(cams[x], cams[y], T)
                detail += (f"; {how}; seam: {ov} overlapping pts, median depth disagreement "
                           f"{err * 1000:.0f} mm, {within * 100:.0f} % within 2 cm")
                seams[(x, y)] = (ov, err, within)
                ang = np.degrees(np.linalg.norm(R.from_matrix(T[:3, :3]).as_rotvec()))
                base = np.linalg.norm(T[:3, 3])
                detail += f" -> {ang:.0f} deg apart, {base * 100:.1f} cm baseline"
                if base > MAX_BASELINE_M:
                    detail += "  REJECTED (baseline too large to be one rig)"
                    T, n = None, 0
            pairs[(x, y)] = (T, n)
            if T is not None:
                pairs[(y, x)] = (np.linalg.inv(T), n)
            print(f"  {x} <-> {y}: {detail}")

    # maximum spanning tree (Prim) on inlier counts, rooted at the reference
    T_to_ref = {ref: np.eye(4)}
    via = {}
    while True:
        best = None
        for p in T_to_ref:
            for c in names:
                if c in T_to_ref:
                    continue
                T, n = pairs.get((p, c), (None, 0))
                if T is not None and n >= MIN_INLIERS and (best is None or n > best[2]):
                    best = (p, c, n)
        if best is None:
            break
        p, c, n = best
        T_to_ref[c] = T_to_ref[p] @ pairs[(p, c)][0]              # ref <- p <- c
        via[c] = (p, n)

    L = link_from_optical()
    Linv = np.linalg.inv(L)
    extr = {"_method": "calibrate_head.py: SIFT RGB-D features + RANSAC + ICP, max spanning tree",
            "_created": time.strftime("%Y-%m-%d %H:%M:%S"),
            ref: {"x": 0.0, "y": 0.0, "z": 0.0, "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0}}
    print(f"\n[calib] reference = {ref}")
    ok = True
    for ns in names[1:]:
        if ns not in T_to_ref:
            ok = False
            extr[ns] = {"x": 0.0, "y": 0.0, "z": 0.0, "qx": 0.0, "qy": 0.0, "qz": 0.0,
                        "qw": 1.0, "rejected": True}
            print(f"  {ns}: NOT LINKED — fewer than {MIN_INLIERS} agreeing features with any "
                  f"calibrated camera. Aim it to share textured objects with a neighbour.")
            continue
        M = L @ T_to_ref[ns] @ Linv                             # ref_link <- ns_link
        t, q = M[:3, 3], R.from_matrix(M[:3, :3]).as_quat()
        rpy = R.from_matrix(M[:3, :3]).as_euler("xyz", degrees=True)
        extr[ns] = {"x": float(t[0]), "y": float(t[1]), "z": float(t[2]),
                    "qx": float(q[0]), "qy": float(q[1]), "qz": float(q[2]), "qw": float(q[3]),
                    "via": via[ns][0], "inliers": via[ns][1]}
        print(f"  {ns}: via {via[ns][0]} ({via[ns][1]} inliers)  t(mm)=[{t[0] * 1000:+.0f},"
              f"{t[1] * 1000:+.0f},{t[2] * 1000:+.0f}]  rpy(deg)=[{rpy[0]:+.1f},{rpy[1]:+.1f},"
              f"{rpy[2]:+.1f}]")

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "camera_extrinsics.json")
    if not ok:
        # never replace a working calibration with a partial one
        path = os.path.join(OUT, "camera_extrinsics.incomplete.json")
    elif os.path.exists(path):
        os.replace(path, path + ".bak")
    with open(path, "w") as fh:
        json.dump(extr, fh, indent=2)
    fused = o3d.geometry.PointCloud()
    for ns, T in T_to_ref.items():
        c = cloud(cams[ns], 0.006)
        c.transform(T)
        fused += c
        o3d.io.write_point_cloud(os.path.join(OUT, f"cal_{ns}.ply"), cloud(cams[ns], 0.004))
    o3d.io.write_point_cloud(os.path.join(OUT, "alignment_check.ply"), fused)
    print(f"\n[calib] wrote {path}" + (" (previous kept as .bak)" if os.path.exists(path + '.bak') else ""))
    print(f"[calib] fused check cloud: {os.path.join(OUT, 'alignment_check.ply')}")
    print("[calib] RESULT:", "ALL CAMERAS CALIBRATED" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
