#!/usr/bin/env python3
"""Quick point cloud viewer — downsamples heavy files before display."""
import os
import sys

# Open3D's bundled GLFW can't create an OpenGL window on a native Wayland
# session — it fails with "Failed to initialize GLEW / Failed creating OpenGL
# window". Force the GL/Qt stack onto X11 (XWayland) before importing open3d so
# the viewer works both from the CLI and from the GUI's "View in 3D" button.
# GLFW selects its backend from XDG_SESSION_TYPE / WAYLAND_DISPLAY, so we must
# *force* X11 here (setdefault won't override an existing "wayland" value).
if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["XDG_SESSION_TYPE"] = "x11"
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    os.environ["GDK_BACKEND"] = "x11"
    os.environ.setdefault("DISPLAY", ":0")  # XWayland display for the fallback

import numpy as np
import open3d as o3d

path = sys.argv[1] if len(sys.argv) > 1 else "output/scan_20260426_001324_cloud_cloud.ply"

print(f"Loading {path} ...")
if path.lower().endswith((".stl", ".obj")):
    mesh = o3d.io.read_triangle_mesh(path)
    mesh.compute_vertex_normals()
    print(f"  mesh: {len(mesh.vertices):,} vertices, {len(mesh.triangles):,} triangles")
    print("Opening viewer — drag to rotate, scroll to zoom, Q to quit.")
    o3d.visualization.draw_geometries([mesh], window_name=path, width=1280, height=800,
                                      mesh_show_back_face=True)
    sys.exit(0)
pcd = o3d.io.read_point_cloud(path)
n = len(pcd.points)
print(f"  {n:,} points loaded")

# Only thin out really big clouds; the exports are already voxelled at the
# detail you chose, and re-gridding them here made them look blocky.
pcd_down = pcd
voxel = 0.0015
while len(pcd_down.points) > 4_000_000:
    pcd_down = pcd.voxel_down_sample(voxel)
    voxel *= 1.5
if len(pcd_down.points) != n:
    print(f"  Thinned to {len(pcd_down.points):,} points for display")

print("Opening viewer — drag to rotate, scroll to zoom, +/- point size, Q to quit.")
vis = o3d.visualization.Visualizer()
vis.create_window(window_name=path, width=1280, height=800)
vis.add_geometry(pcd_down)
opt = vis.get_render_option()
opt.point_size = 2.0          # Open3D's default 5 px squares look like blocks
opt.background_color = np.asarray([0.08, 0.08, 0.1])
vis.run()
vis.destroy_window()
