#!/usr/bin/env python3
"""Quick point cloud viewer — downsamples heavy files before display."""
import sys
import open3d as o3d

path = sys.argv[1] if len(sys.argv) > 1 else "output/scan_20260426_001324_cloud_cloud.ply"

print(f"Loading {path} ...")
pcd = o3d.io.read_point_cloud(path)
n = len(pcd.points)
print(f"  {n:,} points loaded")

# Downsample to something renderable (~1–2 M points)
voxel = 0.003  # 3 mm
pcd_down = pcd.voxel_down_sample(voxel)
n_down = len(pcd_down.points)
print(f"  Downsampled to {n_down:,} points (voxel={voxel*1000:.0f} mm)")

pcd_down.estimate_normals(
    search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.02, max_nn=30)
)

print("Opening viewer — drag to rotate, scroll to zoom, Q to quit.")
o3d.visualization.draw_geometries(
    [pcd_down],
    window_name=path,
    width=1280,
    height=800,
    point_show_normal=False,
)
