#!/usr/bin/env python3
"""Point cloud cleanup and object-isolation helpers for exported scans."""

import json
import os
from datetime import datetime

import numpy as np
import open3d as o3d


ISOLATION_PROFILES = {
    "raw_clean": {
        "label": "Raw clean",
        "description": "Keep the full scan and remove obvious outliers.",
    },
    "largest_cluster": {
        "label": "Largest cluster",
        "description": "Keep the densest contiguous object-sized cluster.",
    },
    "tabletop_object": {
        "label": "Tabletop object",
        "description": "Remove the dominant support plane, then keep the main object cluster.",
    },
    "center_focus": {
        "label": "Center focus",
        "description": "Bias toward the central object and trim edge clutter.",
    },
    "aggressive_hybrid": {
        "label": "Aggressive hybrid",
        "description": "Plane removal, center crop, and cluster filtering for tough scenes.",
    },
    "all_variants": {
        "label": "All variants",
        "description": "Save multiple isolation strategies for comparison.",
    },
}


def _clone_cloud(points, colors=None):
    cloud = o3d.geometry.PointCloud()
    cloud.points = o3d.utility.Vector3dVector(np.asarray(points, dtype=np.float64))
    if colors is not None and len(colors) == len(points):
        cloud.colors = o3d.utility.Vector3dVector(np.asarray(colors, dtype=np.float64))
    return cloud


def _finite_cloud(cloud):
    points = np.asarray(cloud.points)
    if points.size == 0:
        return o3d.geometry.PointCloud()
    mask = np.isfinite(points).all(axis=1)
    colors = np.asarray(cloud.colors) if cloud.has_colors() else None
    if not mask.all():
        points = points[mask]
        if colors is not None and len(colors) == len(mask):
            colors = colors[mask]
    return _clone_cloud(points, colors)


def _clean_cloud(cloud, voxel):
    cleaned = _finite_cloud(cloud)
    if len(cleaned.points) == 0:
        return cleaned

    if len(cleaned.points) > 350000:
        cleaned = cleaned.voxel_down_sample(max(voxel, 0.0015))

    if len(cleaned.points) > 1500:
        cleaned, _ = cleaned.remove_statistical_outlier(nb_neighbors=24, std_ratio=1.8)

    if len(cleaned.points) > 400:
        filtered, _ = cleaned.remove_radius_outlier(
            nb_points=12,
            radius=max(voxel * 3.0, 0.01),
        )
        # Guard: on sparse/large clouds (e.g. a room-scale RTAB-Map export) this
        # radius can reject everything. Only apply it when it keeps a sane share
        # of points; otherwise leave the statistically-cleaned cloud as-is.
        if len(filtered.points) >= 0.2 * len(cleaned.points):
            cleaned = filtered

    return cleaned


def _prepare_mesh_cloud(cloud, voxel):
    working = _finite_cloud(cloud)
    if len(working.points) == 0:
        return working

    if len(working.points) > 180000:
        working = working.voxel_down_sample(max(voxel * 1.5, 0.0015))

    search_radius = max(voxel * 4.0, 0.01)
    working.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(
            radius=search_radius,
            max_nn=30,
        )
    )

    if len(working.points) <= 50000:
        try:
            working.orient_normals_consistent_tangent_plane(
                max(10, min(50, len(working.points) // 50 or 10))
            )
        except RuntimeError:
            pass

    return working


def _reconstruct_mesh(cloud, voxel):
    if len(cloud.points) < 80:
        raise ValueError("Need at least 80 points to build a mesh")

    working = _prepare_mesh_cloud(cloud, voxel)
    if len(working.points) == 0:
        raise ValueError("Mesh preparation removed all points")

    depth = 7 if len(working.points) < 5000 else 8 if len(working.points) < 30000 else 9
    try:
        mesh, _densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            working,
            depth=depth,
        )
        mesh = mesh.crop(working.get_axis_aligned_bounding_box())
        method = "poisson"
    except Exception:
        radii = o3d.utility.DoubleVector([
            max(voxel * 2.0, 0.005),
            max(voxel * 4.0, 0.01),
            max(voxel * 8.0, 0.02),
        ])
        mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
            working,
            radii,
        )
        method = "ball_pivoting"

    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_non_manifold_edges()

    if len(mesh.triangles) > 250000:
        target = max(1000, len(mesh.triangles) // 2)
        mesh = mesh.simplify_quadric_decimation(target)

    mesh.compute_vertex_normals()
    return mesh, {
        "method": method,
        "depth": depth,
        "vertices": int(len(mesh.vertices)),
        "triangles": int(len(mesh.triangles)),
    }


def point_cloud_to_stl(input_path, output_path=None, voxel=0.003):
    cloud = o3d.io.read_point_cloud(input_path)
    if len(cloud.points) == 0:
        raise ValueError(f"No points found in {input_path}")

    cleaned = _clean_cloud(cloud, voxel)
    if len(cleaned.points) == 0:
        raise ValueError(f"Point cloud became empty after cleanup: {input_path}")

    mesh, meta = _reconstruct_mesh(cleaned, voxel)
    output_path = output_path or os.path.splitext(input_path)[0] + ".stl"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if not o3d.io.write_triangle_mesh(output_path, mesh, write_ascii=False):
        raise IOError(f"Failed to write STL mesh to {output_path}")

    return {
        **meta,
        "path": output_path,
        "input_points": int(len(cloud.points)),
        "clean_points": int(len(cleaned.points)),
        "mesh_points": int(len(mesh.vertices)),
    }


def _largest_cluster(cloud, voxel):
    count = len(cloud.points)
    if count < 80:
        return cloud, {"clusters": 0, "selected_points": count}

    # DBSCAN on very large clouds can be prohibitively slow.
    # For clustering, use a coarser proxy cloud when needed.
    cluster_cloud = cloud
    if count > 220000:
        cluster_cloud = cloud.voxel_down_sample(max(voxel * 1.8, 0.004))

    if len(cluster_cloud.points) < 80:
        return cluster_cloud, {"clusters": 0, "selected_points": len(cluster_cloud.points)}

    labels = np.array(
        cluster_cloud.cluster_dbscan(
            eps=max(voxel * 4.0, 0.02),
            min_points=max(24, int(0.002 * len(cluster_cloud.points))),
            print_progress=False,
        )
    )
    valid = labels[labels >= 0]
    if valid.size == 0:
        return cluster_cloud, {"clusters": 0, "selected_points": len(cluster_cloud.points)}

    counts = np.bincount(valid)
    chosen_label = int(np.argmax(counts))
    indices = np.where(labels == chosen_label)[0]
    return cluster_cloud.select_by_index(indices.tolist()), {
        "clusters": int(len(counts)),
        "selected_points": int(len(indices)),
        "cluster_input_points": int(len(cluster_cloud.points)),
    }


def _tabletop_object(cloud, voxel):
    if len(cloud.points) < 120:
        return cloud, {"plane_removed": False, "selected_points": len(cloud.points)}

    plane_model, inliers = cloud.segment_plane(
        distance_threshold=max(voxel * 1.8, 0.004),
        ransac_n=3,
        num_iterations=700,
    )
    if len(inliers) < 80:
        return cloud, {"plane_removed": False, "selected_points": len(cloud.points)}

    no_plane = cloud.select_by_index(inliers, invert=True)
    if len(no_plane.points) < 60:
        return cloud, {"plane_removed": False, "selected_points": len(cloud.points)}

    isolated, cluster_meta = _largest_cluster(no_plane, voxel)
    return isolated, {
        "plane_removed": True,
        "plane": [float(x) for x in plane_model],
        "selected_points": int(len(isolated.points)),
        **cluster_meta,
    }


def _center_focus(cloud):
    points = np.asarray(cloud.points)
    if len(points) < 120:
        return cloud, {"selected_points": len(points)}

    center = np.median(points, axis=0)
    radial = np.linalg.norm(points[:, :2] - center[:2], axis=1)
    radial_limit = np.quantile(radial, 0.72)
    z_min = np.quantile(points[:, 2], 0.02)
    z_max = np.quantile(points[:, 2], 0.985)
    keep = (radial <= radial_limit) & (points[:, 2] >= z_min) & (points[:, 2] <= z_max)

    if keep.sum() < 80:
        return cloud, {"selected_points": len(points)}

    colors = np.asarray(cloud.colors) if cloud.has_colors() else None
    focused = _clone_cloud(points[keep], colors[keep] if colors is not None else None)
    return focused, {"selected_points": int(keep.sum())}


def _aggressive_hybrid(cloud, voxel):
    tabletop, table_meta = _tabletop_object(cloud, voxel)
    focused, focus_meta = _center_focus(tabletop)
    clustered, cluster_meta = _largest_cluster(focused, voxel)
    return clustered, {
        **table_meta,
        **focus_meta,
        **cluster_meta,
        "selected_points": int(len(clustered.points)),
    }


def _bbox_extent(cloud):
    if len(cloud.points) == 0:
        return [0.0, 0.0, 0.0]
    bbox = cloud.get_axis_aligned_bounding_box()
    return [round(float(v), 4) for v in bbox.get_extent()]


def _profile_sequence(profile):
    if profile == "all_variants":
        return [
            "raw_clean",
            "largest_cluster",
            "tabletop_object",
            "center_focus",
            "aggressive_hybrid",
        ]
    return [profile]


def isolate_point_cloud_variants(input_path, output_dir=None, profile="tabletop_object", voxel=0.003):
    cloud = o3d.io.read_point_cloud(input_path)
    if len(cloud.points) == 0:
        raise ValueError(f"No points found in {input_path}")

    cleaned = _clean_cloud(cloud, voxel)
    if len(cleaned.points) == 0:
        raise ValueError(f"Point cloud became empty after cleanup: {input_path}")

    output_dir = output_dir or os.path.dirname(os.path.abspath(input_path))
    os.makedirs(output_dir, exist_ok=True)

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    results = []
    for name in _profile_sequence(profile):
        if name == "raw_clean":
            variant = cleaned
            meta = {"selected_points": int(len(cleaned.points))}
        elif name == "largest_cluster":
            variant, meta = _largest_cluster(cleaned, voxel)
        elif name == "tabletop_object":
            variant, meta = _tabletop_object(cleaned, voxel)
        elif name == "center_focus":
            variant, meta = _center_focus(cleaned)
        elif name == "aggressive_hybrid":
            variant, meta = _aggressive_hybrid(cleaned, voxel)
        else:
            continue

        if len(variant.points) == 0:
            variant = cleaned
            meta = {"fallback": True, "selected_points": int(len(cleaned.points))}

        path = os.path.join(output_dir, f"{base_name}_{name}.ply")
        o3d.io.write_point_cloud(path, variant)
        results.append({
            "profile": name,
            "label": ISOLATION_PROFILES[name]["label"],
            "description": ISOLATION_PROFILES[name]["description"],
            "path": path,
            "points": int(len(variant.points)),
            "bbox_extent_m": _bbox_extent(variant),
            "meta": meta,
        })

    preferred = next(
        (entry for entry in results if entry["profile"] in ("tabletop_object", "largest_cluster", "aggressive_hybrid", "center_focus")),
        results[0],
    )
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": input_path,
        "requested_profile": profile,
        "input_points": int(len(cloud.points)),
        "clean_points": int(len(cleaned.points)),
        "recommended_profile": preferred["profile"],
        "recommended_path": preferred["path"],
        "profiles": results,
    }

    summary_path = os.path.join(output_dir, f"{base_name}_isolation.json")
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    summary["summary_path"] = summary_path
    return summary


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Isolate objects from point clouds")
    parser.add_argument("input", help="Input PLY file")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: input dir)")
    parser.add_argument("--strategy", default="tabletop_object", choices=list(ISOLATION_PROFILES.keys()), help="Isolation strategy")
    parser.add_argument("--voxel", type=float, default=0.003, help="Voxel size (m)")
    parser.add_argument("--mesh-output", default=None, help="Optional STL mesh output path")
    parser.add_argument("--mesh-voxel", type=float, default=0.003, help="Voxel size used for STL reconstruction")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    try:
        summary = isolate_point_cloud_variants(
            args.input,
            output_dir=args.output_dir,
            profile=args.strategy,
            voxel=args.voxel
        )
        print(f"Success: {summary['summary_path']}")
        for result in summary["profiles"]:
            print(f"  {result['profile']}: {result['points']} points -> {result['path']}")
        if args.mesh_output:
            mesh_info = point_cloud_to_stl(
                summary["recommended_path"],
                output_path=args.mesh_output,
                voxel=args.mesh_voxel,
            )
            print(
                f"  STL: {mesh_info['path']} ({mesh_info['triangles']} triangles, {mesh_info['method']})"
            )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)