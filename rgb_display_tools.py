#!/usr/bin/env python3
"""
Optional RGB display and calibration tools for ros_scanner_app.
Extracted from xtion_gui.py. Use these functions to add live video/depth preview.
"""

import cv2
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk


def display_rgb_frame(rgb_image, label_widget):
    """Convert OpenCV BGR image to PhotoImage and display in Tkinter label."""
    if rgb_image is None or rgb_image.size == 0:
        return
    try:
        img = Image.fromarray(cv2.cvtColor(rgb_image, cv2.COLOR_BGR2RGB))
        photo = ImageTk.PhotoImage(img)
        label_widget.config(image=photo)
        label_widget.image = photo  # Keep a reference
    except Exception as e:
        print(f"Error displaying RGB frame: {e}")


def display_depth_frame(depth_image, label_widget, colormap=cv2.COLORMAP_JET):
    """Convert depth image to color-mapped visualization and display."""
    if depth_image is None or depth_image.size == 0:
        return
    try:
        depth_norm = cv2.normalize(depth_image, None, 0, 255, cv2.NORM_MINMAX, cv2.CV_8U)
        depth_colored = cv2.applyColorMap(depth_norm, colormap)
        img = Image.fromarray(cv2.cvtColor(depth_colored, cv2.COLOR_BGR2RGB))
        photo = ImageTk.PhotoImage(img)
        label_widget.config(image=photo)
        label_widget.image = photo
    except Exception as e:
        print(f"Error displaying depth frame: {e}")


def calibrate_camera_checkerboard(images, checkerboard_size=(9, 6)):
    """
    Calibrate camera using checkerboard images.
    
    Args:
        images: List of CV2 BGR images containing checkerboard
        checkerboard_size: (cols, rows) of checkerboard corners
    
    Returns:
        camera_matrix, distortion_coefficients, success_count
    """
    objp = np.zeros((checkerboard_size[0] * checkerboard_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:checkerboard_size[0], 0:checkerboard_size[1]].T.reshape(-1, 2)
    
    objpoints = []
    imgpoints = []
    
    for img in images:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ret, corners = cv2.findChessboardCorners(gray, checkerboard_size, None)
        
        if ret:
            objpoints.append(objp)
            corners2 = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
                                       (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001))
            imgpoints.append(corners2)
    
    if len(objpoints) < 3:
        raise ValueError(f"Need at least 3 calibration images, got {len(objpoints)}")
    
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
        objpoints, imgpoints, gray.shape[::-1], None, None
    )
    
    return mtx, dist, len(objpoints)


def register_rgb_to_depth(rgb_frame, depth_frame, camera_matrix, depth_scale=1000.0):
    """
    Simple RGB-to-depth registration assuming aligned cameras.
    Returns depth frame reprojected to RGB dimensions.
    
    Args:
        rgb_frame: RGB image
        depth_frame: Depth image (in mm)
        camera_matrix: Camera intrinsics from calibration
        depth_scale: Millimeters per depth unit
    
    Returns:
        Registered depth frame
    """
    h, w = rgb_frame.shape[:2]
    depth_h, depth_w = depth_frame.shape[:2]
    
    # Simple bilinear resize if dimensions differ
    if (depth_h, depth_w) != (h, w):
        depth_registered = cv2.resize(depth_frame, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        depth_registered = depth_frame.copy()
    
    return depth_registered


def estimate_surface_normals(points_3d, neighborhood_size=30):
    """
    Estimate surface normals using neighboring points.
    
    Args:
        points_3d: Nx3 numpy array of 3D points
        neighborhood_size: Number of neighbors for normal estimation
    
    Returns:
        Nx3 array of normal vectors
    """
    try:
        import open3d as o3d
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(points_3d)
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamKNN(knn=neighborhood_size)
        )
        return np.asarray(pcd.normals)
    except Exception as e:
        print(f"Error estimating normals: {e}")
        return np.zeros_like(points_3d)


__all__ = [
    'display_rgb_frame',
    'display_depth_frame',
    'calibrate_camera_checkerboard',
    'register_rgb_to_depth',
    'estimate_surface_normals',
]
