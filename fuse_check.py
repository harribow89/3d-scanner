#!/usr/bin/env python3
"""Fuse the per-camera clouds of a station sweep using the CURRENT extrinsics
(output/camera_extrinsics.json) and colour each camera distinctly, so you can
eyeball how well/badly the cameras are aligned. Writes output/alignment_check.ply.

    .venv/bin/python fuse_check.py [sweep_dir]      # default: sweep_00_cams
    python3 view_cloud.py output/alignment_check.ply
"""
import json
import os
import sys

import numpy as np
import open3d as o3d
from scipy.spatial.transform import Rotation as R

HERE = os.path.dirname(os.path.abspath(__file__))
EXTR = os.path.join(HERE, "output", "camera_extrinsics.json")
COLORS = {  # one solid colour per camera
    "camera1": [1.0, 0.25, 0.25],   # red    (reference)
    "camera2": [0.25, 1.0, 0.25],   # green
    "camera3": [0.30, 0.45, 1.0],   # blue
}


def link_from_optical():
    Tt = np.eye(4); Tt[:3, 3] = [0.0, -0.045, 0.0]
    Rm = np.eye(4)
    Rm[:3, :3] = R.from_euler("ZYX", [-np.pi / 2, 0.0, -np.pi / 2]).as_matrix()
    return Tt @ Rm


def m_link(c):
    M = np.eye(4)
    M[:3, :3] = R.from_quat([c["qx"], c["qy"], c["qz"], c["qw"]]).as_matrix()
    M[:3, 3] = [c["x"], c["y"], c["z"]]
    return M


def main():
    sweep_dir = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(HERE, "output", "sweeps", "sweep_00_cams")
    with open(EXTR) as f:
        extr = json.load(f)
    L = link_from_optical()
    merged = o3d.geometry.PointCloud()
    for ns in ("camera1", "camera2", "camera3"):
        p = os.path.join(sweep_dir, f"{ns}.ply")
        if not os.path.exists(p):
            print(f"  {ns}: no cloud at {p} — skipped"); continue
        pcd = o3d.io.read_point_cloud(p).voxel_down_sample(0.01)
        c = extr.get(ns)
        if c and not c.get("rejected"):
            T = np.linalg.inv(L) @ m_link(c) @ L      # ref_opt <- ns_opt
            pcd.transform(T)
            tag = f"t(mm)=[{c['x']*1000:+.0f},{c['y']*1000:+.0f},{c['z']*1000:+.0f}]"
        else:
            tag = "identity (no/!rejected extrinsics)"
        pcd.paint_uniform_color(COLORS[ns])
        merged += pcd
        print(f"  {ns}: {len(pcd.points):,} pts, {tag}, colour={COLORS[ns]}")
    out = os.path.join(HERE, "output", "alignment_check.ply")
    o3d.io.write_point_cloud(out, merged)
    print(f"\nwrote {out}  ({len(merged.points):,} pts)")
    print("view with:  python3 view_cloud.py output/alignment_check.ply")


if __name__ == "__main__":
    main()
