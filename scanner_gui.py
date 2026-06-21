#!/usr/bin/env python3
"""
PySide6-based GUI for ASUS Xtion 3D Scanner (ROS + RTAB-Map + Docker).

Controls all scanner modes: camera preview, mapping, export, multi-camera.
Displays real-time telemetry from the RTAB-Map database.
"""

import sys
import json
import sqlite3
import subprocess
import threading
import time
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel, QSpinBox, QDoubleSpinBox,
    QComboBox, QFileDialog, QMessageBox, QProgressBar, QFrame,
    QGridLayout, QGroupBox, QTextEdit, QCheckBox, QSlider,
    QSplitter, QScrollArea
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal


HERE = Path(__file__).parent.absolute()
DOCKER_IMAGE = "scanner-ros:jazzy"
DB_PATH = HERE / "rtabmap.db"
OUTPUT_DIR = HERE / "output"


class DockerCommand(QThread):
    """Run a Docker command in a background thread."""
    output = Signal(str)
    # Named to avoid shadowing QThread's built-in no-arg `finished` signal.
    command_done = Signal(int)  # exit code

    def __init__(self, cmd: str):
        super().__init__()
        self.cmd = cmd

    def run(self):
        try:
            result = subprocess.run(
                self.cmd, shell=True, capture_output=True, text=True, timeout=600
            )
            if result.stdout:
                self.output.emit(result.stdout)
            if result.stderr:
                self.output.emit(f"[stderr] {result.stderr}")
            self.command_done.emit(result.returncode)
        except subprocess.TimeoutExpired:
            self.output.emit("[ERROR] Command timed out (10 min)")
            self.command_done.emit(124)
        except Exception as e:
            self.output.emit(f"[ERROR] {str(e)}")
            self.command_done.emit(1)


class TelemetryMonitor(QThread):
    """Poll RTAB-Map database for live telemetry."""
    updated = Signal(dict)  # telemetry dict

    def __init__(self, db_path: Path):
        super().__init__()
        self.db_path = db_path
        self.running = True

    def run(self):
        while self.running:
            telemetry = self._read_db()
            self.updated.emit(telemetry)
            time.sleep(2)

    def _read_db(self) -> dict:
        telemetry = {
            "nodes": 0,
            "keyframes": 0,
            "links": 0,
            "last_loop_closure": None,
            "map_size_mb": 0,
            "status": "offline"
        }
        if not self.db_path.exists():
            return telemetry

        try:
            # CRITICAL: open read-only + immutable so polling NEVER takes a lock.
            # A normal connection takes a SQLite shared lock, which makes
            # RTAB-Map's COMMIT fail ("database is locked") and crashes the
            # mapping process mid-scan. immutable=1 disables all locking on this
            # connection — telemetry may read a slightly stale count, which is
            # fine, but the writer is never blocked.
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro&immutable=1", uri=True
            )
            cur = conn.cursor()

            # Node count
            cur.execute("SELECT COUNT(*) FROM Node")
            count = cur.fetchone()
            if count:
                telemetry["nodes"] = count[0]

            # Keyframe count (RTAB-Map: weight >= 0 are keyframes; -1 = intermediate)
            cur.execute("SELECT COUNT(*) FROM Node WHERE weight >= 0")
            count = cur.fetchone()
            if count:
                telemetry["keyframes"] = count[0]

            # Neighbour (trajectory) link count: Link.type 0 = neighbour
            cur.execute("SELECT COUNT(*) FROM Link WHERE type = 0")
            count = cur.fetchone()
            if count:
                telemetry["links"] = count[0]

            # Latest loop closure (Link.type 1/2/3 = loop/global/local closures)
            cur.execute("""
                SELECT from_id, to_id FROM Link WHERE type IN (1, 2, 3)
                ORDER BY rowid DESC LIMIT 1
            """)
            lc = cur.fetchone()
            if lc:
                telemetry["last_loop_closure"] = f"nodes {lc[0]}↔{lc[1]}"

            conn.close()

            # DB file size
            telemetry["map_size_mb"] = round(self.db_path.stat().st_size / 1024 / 1024, 2)
            telemetry["status"] = "online" if telemetry["nodes"] > 0 else "idle"
        except Exception as e:
            telemetry["status"] = f"error: {str(e)[:30]}"

        return telemetry

    def stop(self):
        self.running = False


class ScannerGUI(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ASUS Xtion 3D Scanner Control Panel")
        self.setGeometry(100, 100, 1200, 800)

        # State
        self.current_process = None
        self.telemetry = {}

        # Telemetry monitor
        self.monitor = TelemetryMonitor(DB_PATH)
        self.monitor.updated.connect(self._on_telemetry_update)
        self.monitor.start()

        # UI
        self._setup_ui()
        self._setup_timers()

    def _setup_ui(self):
        """Build the main UI."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Top status bar
        status_layout = QHBoxLayout()
        self.status_label = QLabel("Status: Idle")
        self.status_label.setStyleSheet("font-weight: bold; color: green;")
        self.nodes_label = QLabel("Nodes: 0")
        self.db_size_label = QLabel("DB: 0 MB")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.nodes_label)
        status_layout.addWidget(self.db_size_label)
        layout.addLayout(status_layout)

        # Tab widget
        tabs = QTabWidget()
        tabs.addTab(self._build_camera_tab(), "Camera Preview")
        tabs.addTab(self._build_mapping_tab(), "Mapping")
        tabs.addTab(self._build_export_tab(), "Export")
        tabs.addTab(self._build_multicam_tab(), "Multi-Camera")
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.addTab(self._build_log_tab(), "Log")
        layout.addWidget(tabs)

        # Bottom buttons
        button_layout = QHBoxLayout()
        self.stop_btn = QPushButton("Stop / Cancel")
        self.stop_btn.setStyleSheet("background-color: #ff6b6b;")
        self.stop_btn.clicked.connect(self._on_stop)
        button_layout.addStretch()
        button_layout.addWidget(self.stop_btn)
        layout.addLayout(button_layout)

    def _build_camera_tab(self) -> QWidget:
        """Camera preview and single-frame capture."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Test camera stream
        test_group = QGroupBox("Test Camera Stream")
        test_layout = QVBoxLayout(test_group)
        self.camera_test_btn = QPushButton("Launch Live Preview (30 sec)")
        self.camera_test_btn.clicked.connect(self._on_camera_preview)
        test_layout.addWidget(self.camera_test_btn)
        layout.addWidget(test_group)

        # Single frame capture
        snap_group = QGroupBox("Capture Single Frame")
        snap_layout = QVBoxLayout(snap_group)
        self.snap_output_label = QLabel(f"Output: {OUTPUT_DIR}/single_frame.ply")
        snap_layout.addWidget(self.snap_output_label)
        self.snap_btn = QPushButton("Capture Frame")
        self.snap_btn.clicked.connect(self._on_snap)
        snap_layout.addWidget(self.snap_btn)
        layout.addWidget(snap_group)

        layout.addStretch()
        return widget

    def _build_mapping_tab(self) -> QWidget:
        """Full RTAB-Map SLAM mapping."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Quick map mode
        quick_group = QGroupBox("Quick SLAM Scan")
        quick_layout = QVBoxLayout(quick_group)
        quick_desc = QLabel(
            "Continuous mapping with RTAB-Map.\nMove camera slowly to build a 3D map.\n"
            "Press 'Stop / Cancel' to finish."
        )
        quick_layout.addWidget(quick_desc)
        self.quick_map_btn = QPushButton("Start Quick Scan")
        self.quick_map_btn.clicked.connect(self._on_quick_map)
        quick_layout.addWidget(self.quick_map_btn)
        layout.addWidget(quick_group)

        object_group = QGroupBox("Object Capture Helper")
        object_layout = QVBoxLayout(object_group)
        object_layout.addWidget(QLabel(
            "For single objects, use a tighter depth window, keep the object centered, "
            "and finish with one slow top pass before export."
        ))
        self.object_preset_btn = QPushButton("Apply Object-Capture Preset")
        self.object_preset_btn.clicked.connect(self._apply_object_capture_preset)
        object_layout.addWidget(self.object_preset_btn)
        self.object_scan_btn = QPushButton("Start Object Capture Scan")
        self.object_scan_btn.clicked.connect(self._on_object_capture_scan)
        object_layout.addWidget(self.object_scan_btn)
        layout.addWidget(object_group)

        guide_group = QGroupBox("Capture Guide")
        guide_layout = QVBoxLayout(guide_group)
        guide_layout.addWidget(QLabel("Coverage Progress:"))
        self.coverage_bar = QProgressBar()
        self.coverage_bar.setRange(0, 100)
        self.coverage_bar.setValue(0)
        self.coverage_bar.setFormat("%p%")
        guide_layout.addWidget(self.coverage_bar)

        self.coverage_hint = QLabel("Start a guided scan to see what to do next.")
        self.coverage_hint.setWordWrap(True)
        guide_layout.addWidget(self.coverage_hint)

        checklist_frame = QFrame()
        checklist_layout = QVBoxLayout(checklist_frame)
        self.checklist_base_texts = [
            "1. Keep the object centered and move slowly.",
            "2. Complete one full side orbit with overlap.",
            "3. Add a slightly higher angle pass.",
            "4. Finish with one slow top pass and export.",
        ]
        self.checklist_labels = []
        for text in self.checklist_base_texts:
            label = QLabel(f"• {text}")
            label.setWordWrap(True)
            checklist_layout.addWidget(label)
            self.checklist_labels.append(label)
        guide_layout.addWidget(checklist_frame)

        self.guided_scan_btn = QPushButton("Start Guided Object Scan")
        self.guided_scan_btn.clicked.connect(self._on_guided_object_scan)
        guide_layout.addWidget(self.guided_scan_btn)
        layout.addWidget(guide_group)

        # Full GUI mode (hands-off SLAM + visualization)
        gui_group = QGroupBox("Full SLAM GUI (Hands-Off)")
        gui_layout = QVBoxLayout(gui_group)
        gui_desc = QLabel(
            "Combines camera + RTAB-Map node + rtabmap_viz.\n"
            "Mapping starts automatically. Close the window to finish."
        )
        gui_layout.addWidget(gui_desc)
        self.full_gui_btn = QPushButton("Launch Full SLAM GUI")
        self.full_gui_btn.clicked.connect(self._on_full_gui)
        gui_layout.addWidget(self.full_gui_btn)
        layout.addWidget(gui_group)

        # Map stats
        stats_group = QGroupBox("Current Map Statistics")
        stats_layout = QGridLayout(stats_group)
        stats_layout.addWidget(QLabel("Keyframes:"), 0, 0)
        self.kf_label = QLabel("0")
        stats_layout.addWidget(self.kf_label, 0, 1)
        stats_layout.addWidget(QLabel("Links:"), 1, 0)
        self.links_label = QLabel("0")
        stats_layout.addWidget(self.links_label, 1, 1)
        stats_layout.addWidget(QLabel("Last Loop:"), 2, 0)
        self.loop_label = QLabel("—")
        stats_layout.addWidget(self.loop_label, 2, 1)
        layout.addWidget(stats_group)

        layout.addStretch()
        return widget

    def _coverage_state(self) -> tuple[int, str, list[bool]]:
        stats = self.telemetry or {}
        nodes = int(stats.get("nodes", 0) or 0)
        keyframes = int(stats.get("keyframes", 0) or 0)
        links = int(stats.get("links", 0) or 0)
        db_size = float(stats.get("map_size_mb", 0) or 0)

        if nodes <= 0 and keyframes <= 0:
            return 0, "Start a scan and circle the object slowly to build the first coverage.", [False, False, False, False]

        score = min(100, int(nodes * 4 + keyframes * 3 + links * 7 + db_size * 3))

        if score < 20:
            hint = "Build the first side orbit. Keep the object centered and move slower than you think."
        elif score < 45:
            hint = "Good start. Add a second pass from a slightly higher angle and keep overlap high."
        elif score < 70:
            hint = "Coverage is decent. Add a top-biased pass to fill in the roof and upper edges."
        else:
            hint = "Coverage looks ready for export. If the shape is thin, add one final top pass first."

        checklist = [
            nodes >= 5 or keyframes >= 4,
            nodes >= 10 or links >= 2,
            nodes >= 16 or db_size >= 0.2,
            score >= 70,
        ]
        return score, hint, checklist

    def _update_capture_guide(self):
        if not hasattr(self, "coverage_bar"):
            return

        score, hint, checklist = self._coverage_state()
        self.coverage_bar.setValue(score)
        self.coverage_hint.setText(hint)

        for index, label in enumerate(self.checklist_labels):
            done = checklist[index]
            prefix = "[x]" if done else "[ ]"
            base_text = self.checklist_base_texts[index]
            label.setText(f"{prefix} {base_text}")
            label.setStyleSheet("font-weight: bold; color: #2e7d32;" if done else "color: #666;")

    def _build_export_tab(self) -> QWidget:
        """Export and post-processing."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Export options
        export_group = QGroupBox("Export Point Cloud")
        export_layout = QGridLayout(export_group)

        export_layout.addWidget(QLabel("Output File:"), 0, 0)
        self.export_path_label = QLabel(f"{OUTPUT_DIR}/scan.ply")
        export_layout.addWidget(self.export_path_label, 0, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._on_browse_export)
        export_layout.addWidget(browse_btn, 0, 2)

        export_layout.addWidget(QLabel("RTAB-Map writes <name>_cloud.ply automatically."), 1, 0, 1, 3)

        export_layout.addWidget(QLabel("Database:"), 2, 0)
        self.db_selector = QComboBox()
        self.db_selector.addItem(f"{DB_PATH} (current)")
        for ply in sorted(OUTPUT_DIR.glob("cal_*.ply")):
            # Allow selecting intermediate calibration clouds (for reference)
            pass
        export_layout.addWidget(self.db_selector, 2, 1)

        self.export_btn = QPushButton("Export Cloud")
        self.export_btn.clicked.connect(self._on_export)
        export_layout.addWidget(self.export_btn, 3, 0)

        self.view_btn = QPushButton("View in 3D")
        self.view_btn.clicked.connect(self._on_view_cloud)
        export_layout.addWidget(self.view_btn, 3, 1)

        self.stl_btn = QPushButton("Generate STL")
        self.stl_btn.clicked.connect(self._on_generate_stl)
        export_layout.addWidget(self.stl_btn, 3, 2)

        self.export_stl_btn = QPushButton("Export + STL")
        self.export_stl_btn.clicked.connect(self._on_export_and_stl)
        export_layout.addWidget(self.export_stl_btn, 4, 0, 1, 3)

        layout.addWidget(export_group)

        # Post-processing
        post_group = QGroupBox("Post-Processing")
        post_layout = QVBoxLayout(post_group)

        post_layout.addWidget(QLabel("Isolation Strategy:"))
        self.isolation_combo = QComboBox()
        self.isolation_combo.addItems([
            "raw_clean",
            "largest_cluster",
            "tabletop_object",
            "center_focus",
            "aggressive_hybrid",
            "all_variants"
        ])
        post_layout.addWidget(self.isolation_combo)

        self.isolation_btn = QPushButton("Apply Isolation")
        self.isolation_btn.clicked.connect(self._on_isolate)
        post_layout.addWidget(self.isolation_btn)

        layout.addWidget(post_group)

        # Additional exports
        layout.addWidget(QLabel("Saved Files in output/:"))
        self.files_list = QTextEdit()
        self.files_list.setReadOnly(True)
        layout.addWidget(self.files_list)

        self._refresh_file_list()
        return widget

    def _build_multicam_tab(self) -> QWidget:
        """Multi-camera setup and calibration."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        layout.addWidget(QLabel("Multiple Camera Control"))

        # Multi-camera preview
        multi_group = QGroupBox("Multi-Camera Live View")
        multi_layout = QVBoxLayout(multi_group)
        multi_desc = QLabel(
            "Displays all detected ASUS Xtions from cameras.json.\n"
            "Uses calibrated extrinsics from calibrate_multi.py."
        )
        multi_layout.addWidget(multi_desc)
        self.multi_view_btn = QPushButton("Launch Multi-Camera RViz")
        self.multi_view_btn.clicked.connect(self._on_multi_view)
        multi_layout.addWidget(self.multi_view_btn)
        layout.addWidget(multi_group)

        # Calibration
        calib_group = QGroupBox("Calibration")
        calib_layout = QVBoxLayout(calib_group)
        calib_desc = QLabel(
            "Runs markerless extrinsic calibration (FPFH + RANSAC + ICP).\n"
            "Aim all cameras at a structured scene with overlap.\n"
            "Updates output/camera_extrinsics.json."
        )
        calib_layout.addWidget(calib_desc)
        self.calib_btn = QPushButton("Run Auto-Calibration")
        self.calib_btn.clicked.connect(self._on_calibrate)
        calib_layout.addWidget(self.calib_btn)
        layout.addWidget(calib_group)

        # Station / sweep scanning (Matterport-style) — see MATTERPORT_REPLICATION_PLAN.md
        station_group = QGroupBox("Station / Sweep Scan (Matterport-style)")
        station_layout = QVBoxLayout(station_group)
        station_layout.addWidget(QLabel(
            "Capture discrete sweeps from fixed tripod positions, then fuse.\n"
            "Move the rig between sweeps; each sweep stacks N frames/camera and\n"
            "fuses all cameras via the calibrated extrinsics. Build globally\n"
            "registers all sweeps (pose-graph) into output/room.ply."
        ))
        frames_row = QHBoxLayout()
        frames_row.addWidget(QLabel("Frames per camera:"))
        self.station_frames_spin = QSpinBox()
        self.station_frames_spin.setRange(1, 60)
        self.station_frames_spin.setValue(12)
        frames_row.addWidget(self.station_frames_spin)
        frames_row.addStretch()
        station_layout.addLayout(frames_row)

        self.station_count_label = QLabel("Sweeps captured: 0")
        station_layout.addWidget(self.station_count_label)

        btn_row = QHBoxLayout()
        self.station_capture_btn = QPushButton("Capture Sweep")
        self.station_capture_btn.clicked.connect(self._on_station_capture)
        btn_row.addWidget(self.station_capture_btn)
        self.station_build_btn = QPushButton("Build Room")
        self.station_build_btn.clicked.connect(self._on_station_build)
        btn_row.addWidget(self.station_build_btn)
        station_layout.addLayout(btn_row)

        btn_row2 = QHBoxLayout()
        self.station_view_btn = QPushButton("View Room")
        self.station_view_btn.clicked.connect(self._on_station_view)
        btn_row2.addWidget(self.station_view_btn)
        self.station_clear_btn = QPushButton("Clear Sweeps")
        self.station_clear_btn.clicked.connect(self._on_station_clear)
        btn_row2.addWidget(self.station_clear_btn)
        station_layout.addLayout(btn_row2)
        layout.addWidget(station_group)

        # Camera roster
        roster_group = QGroupBox("Detected Cameras (cameras.json)")
        roster_layout = QVBoxLayout(roster_group)
        self.roster_text = QTextEdit()
        self.roster_text.setReadOnly(True)
        roster_layout.addWidget(self.roster_text)
        layout.addWidget(roster_group)

        self._refresh_roster()
        self._refresh_sweep_count()
        return widget

    def _build_settings_tab(self) -> QWidget:
        """Settings and parameters."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Project settings
        proj_group = QGroupBox("Project Settings")
        proj_layout = QGridLayout(proj_group)

        proj_layout.addWidget(QLabel("Output Directory:"), 0, 0)
        output_label = QLabel(str(OUTPUT_DIR))
        proj_layout.addWidget(output_label, 0, 1)

        proj_layout.addWidget(QLabel("RTAB-Map Database:"), 1, 0)
        db_label = QLabel(str(DB_PATH))
        proj_layout.addWidget(db_label, 1, 1)

        layout.addWidget(proj_group)

        # Scanner parameters
        param_group = QGroupBox("Scanner Parameters")
        param_layout = QGridLayout(param_group)

        param_layout.addWidget(QLabel("Voxel Size (m):"), 0, 0)
        self.voxel_spin = QDoubleSpinBox()
        self.voxel_spin.setValue(0.012)
        self.voxel_spin.setRange(0.001, 0.1)
        self.voxel_spin.setSingleStep(0.001)
        param_layout.addWidget(self.voxel_spin, 0, 1)

        param_layout.addWidget(QLabel("Depth Quality Gate:"), 1, 0)
        self.depth_quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.depth_quality_slider.setValue(30)
        self.depth_quality_slider.setMaximum(100)
        param_layout.addWidget(self.depth_quality_slider, 1, 1)

        param_layout.addWidget(QLabel("Max Depth (mm):"), 2, 0)
        self.max_depth_spin = QSpinBox()
        self.max_depth_spin.setValue(2500)
        self.max_depth_spin.setRange(500, 5000)
        self.max_depth_spin.setSingleStep(100)
        param_layout.addWidget(self.max_depth_spin, 2, 1)

        layout.addWidget(param_group)

        # System info
        info_group = QGroupBox("System Information")
        info_layout = QVBoxLayout(info_group)
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self._update_system_info()
        info_layout.addWidget(self.info_text)
        layout.addWidget(info_group)

        # Recovery operations (safe)
        recover_group = QGroupBox("Recovery")
        recover_layout = QVBoxLayout(recover_group)

        stop_containers_btn = QPushButton("Stop All Scanner Containers")
        stop_containers_btn.setStyleSheet("background-color: #44aa55; color: white;")
        stop_containers_btn.setToolTip(
            "Stop every running scanner-ros Docker container. Use this if the "
            "camera seems stuck/busy or buttons stop responding — leftover "
            "containers hold the USB camera and must be cleared."
        )
        stop_containers_btn.clicked.connect(self._on_stop_containers)
        recover_layout.addWidget(stop_containers_btn)

        layout.addWidget(recover_group)

        # Unsafe operations
        unsafe_group = QGroupBox("Maintenance (Use with Care)")
        unsafe_layout = QVBoxLayout(unsafe_group)

        clear_db_btn = QPushButton("Clear RTAB-Map Database")
        clear_db_btn.setStyleSheet("background-color: #ffaa00;")
        clear_db_btn.clicked.connect(self._on_clear_db)
        unsafe_layout.addWidget(clear_db_btn)

        rebuild_docker_btn = QPushButton("Rebuild Docker Image")
        rebuild_docker_btn.setStyleSheet("background-color: #ffaa00;")
        rebuild_docker_btn.clicked.connect(self._on_rebuild_docker)
        unsafe_layout.addWidget(rebuild_docker_btn)

        layout.addWidget(unsafe_group)
        layout.addStretch()
        return widget

    def _build_log_tab(self) -> QWidget:
        """Command output log."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)
        return widget

    def _setup_timers(self):
        """Set up periodic UI updates."""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_file_list)
        self._timer.start(5000)  # Refresh every 5 sec

    def _resolve_export_paths(self, requested_path: str) -> Tuple[Path, Path]:
        path = Path(requested_path)
        suffix = path.suffix.lower()
        if suffix in {".ply", ".stl"}:
            stem = path.stem
        else:
            stem = path.name

        raw_cloud = path.with_name(f"{stem}_cloud.ply")
        stl_path = path.with_name(f"{stem}.stl")
        return raw_cloud, stl_path

    def _resolve_export_request(self, requested_path: str) -> Path:
        path = Path(requested_path)
        suffix = path.suffix.lower()
        if suffix == ".ply":
            return path
        if suffix == ".stl":
            return path.with_suffix(".ply")
        return path.with_suffix(".ply") if path.suffix else path.with_name(f"{path.name}.ply")

    def _apply_object_capture_preset(self):
        self.voxel_spin.setValue(0.008)
        self.depth_quality_slider.setValue(40)
        self.max_depth_spin.setValue(2200)
        self.isolation_combo.setCurrentText("tabletop_object")
        self.export_path_label.setText(str(OUTPUT_DIR / "object_scan.ply"))
        self._log("Applied object-capture preset: tighter depth window, tabletop isolation, finer voxel.")

    def _on_object_capture_scan(self):
        self._apply_object_capture_preset()
        self._on_quick_map()

    def _on_guided_object_scan(self):
        """Start an object scan with the on-screen capture guide active.
        Applies the object preset and begins mapping; the coverage bar,
        checklist and hint update from telemetry via the refresh timer."""
        self._apply_object_capture_preset()
        if hasattr(self, "coverage_hint"):
            self.coverage_hint.setText("Guided scan started — follow the checklist below.")
        self._update_capture_guide()
        self._on_quick_map()

    # Command handlers
    def _run_command(self, cmd: str, title: str = "Command Running"):
        """Run a Docker command in a thread and show output."""
        if self.current_process and self.current_process.isRunning():
            QMessageBox.warning(self, "Busy", "A command is already running.")
            return

        self.current_process = DockerCommand(cmd)
        self.current_process.output.connect(self._on_command_output)
        self.current_process.command_done.connect(self._on_command_finished)
        self.current_process.start()

        self.status_label.setText(f"Status: {title}")
        self.status_label.setStyleSheet("font-weight: bold; color: orange;")
        self._log(f"\n[{datetime.now().strftime('%H:%M:%S')}] {title}")
        self._log(f"Command: {cmd}\n")

    def _on_camera_preview(self):
        self._cleanup_before_scan()
        cmd = f"cd {HERE} && ./run_scanner_docker.sh camera"
        self._run_command(cmd, "Camera Preview (30 sec)")

    def _on_snap(self):
        self._cleanup_before_scan()
        output = str(OUTPUT_DIR / "single_frame.ply")
        cmd = f"cd {HERE} && ./run_scanner_docker.sh snap {output}"
        self._run_command(cmd, "Capturing Single Frame")

    def _on_quick_map(self):
        self._cleanup_before_scan()
        cmd = f"cd {HERE} && ./run_scanner_docker.sh map"
        self._run_command(cmd, "Quick SLAM Mapping")

    def _on_full_gui(self):
        self._cleanup_before_scan()
        cmd = f"cd {HERE} && ./run_scanner_docker.sh gui"
        self._run_command(cmd, "Full SLAM GUI (check your display window)")

    def _on_export(self):
        output = self.export_path_label.text()
        export_request = self._resolve_export_request(output)
        max_range_m = self.max_depth_spin.value() / 1000.0
        voxel_m = self.voxel_spin.value()
        cmd = (
            f"cd {HERE} && ./run_scanner_docker.sh export rtabmap.db {export_request} "
            f"{max_range_m:.3f} {voxel_m:.4f}"
        )
        self._run_command(cmd, f"Exporting to {export_request}")

    def _on_view_cloud(self):
        output = self.export_path_label.text()
        raw_cloud, _ = self._resolve_export_paths(output)
        candidate = raw_cloud if raw_cloud.exists() else Path(output)
        if not candidate.exists():
            QMessageBox.warning(self, "Not Found", f"File not found: {candidate}")
            return
        cmd = f"./.venv/bin/python view_cloud.py {candidate}"
        self._run_command(cmd, f"Viewing {candidate.name}")

    def _on_isolate(self):
        strategy = self.isolation_combo.currentText()
        output = self.export_path_label.text()
        raw_cloud, _ = self._resolve_export_paths(output)
        candidate = raw_cloud if raw_cloud.exists() else Path(output)
        if not candidate.exists():
            QMessageBox.warning(self, "Not Found", f"No exported cloud found at {candidate}")
            return
        voxel_m = self.voxel_spin.value()
        cmd = f"./.venv/bin/python point_cloud_tools.py {candidate} --strategy {strategy} --voxel {voxel_m:.4f}"
        self._run_command(cmd, f"Isolating with {strategy}")

    def _on_generate_stl(self):
        strategy = self.isolation_combo.currentText()
        output = self.export_path_label.text()
        raw_cloud, stl_path = self._resolve_export_paths(output)
        candidate = raw_cloud if raw_cloud.exists() else Path(output)
        if not candidate.exists():
            QMessageBox.warning(self, "Not Found", f"No exported cloud found at {candidate}")
            return
        voxel_m = self.voxel_spin.value()
        cmd = (
            f"./.venv/bin/python point_cloud_tools.py {candidate} "
            f"--strategy {strategy} --voxel {voxel_m:.4f} --mesh-output {stl_path}"
        )
        self._run_command(cmd, f"Building STL {stl_path.name}")

    def _on_export_and_stl(self):
        strategy = self.isolation_combo.currentText()
        output = self.export_path_label.text()
        export_request = self._resolve_export_request(output)
        raw_cloud, stl_path = self._resolve_export_paths(output)
        max_range_m = self.max_depth_spin.value() / 1000.0
        voxel_m = self.voxel_spin.value()
        cmd = (
            f"cd {HERE} && ./run_scanner_docker.sh export rtabmap.db {export_request} "
            f"{max_range_m:.3f} {voxel_m:.4f} && "
            f"./.venv/bin/python point_cloud_tools.py {raw_cloud} "
            f"--strategy {strategy} --voxel {voxel_m:.4f} --mesh-output {stl_path}"
        )
        self._run_command(cmd, f"Exporting STL via {strategy}")

    def _on_multi_view(self):
        cmd = f"cd {HERE} && ./run_scanner_docker.sh multi"
        self._run_command(cmd, "Multi-Camera RViz (check your display window)")

    def _on_calibrate(self):
        cmd = f"cd {HERE} && ./.venv/bin/python calibrate_multi.py"
        self._run_command(cmd, "Running Markerless Calibration")

    # --- Station / sweep scanning handlers ---
    def _sweep_dir(self) -> Path:
        return OUTPUT_DIR / "sweeps"

    def _count_sweeps(self) -> int:
        d = self._sweep_dir()
        return len(list(d.glob("sweep_*.ply"))) if d.exists() else 0

    def _refresh_sweep_count(self):
        if hasattr(self, "station_count_label"):
            self.station_count_label.setText(f"Sweeps captured: {self._count_sweeps()}")

    def _on_station_capture(self):
        self._cleanup_before_scan()
        frames = self.station_frames_spin.value()
        n = self._count_sweeps()
        cmd = f"cd {HERE} && ./run_scanner_docker.sh station capture --frames {frames}"
        self._run_command(cmd, f"Capturing sweep {n:02d} ({frames} frames/cam)")

    def _on_station_build(self):
        if self._count_sweeps() == 0:
            QMessageBox.warning(self, "No Sweeps",
                                "Capture at least one sweep before building.")
            return
        voxel_m = self.voxel_spin.value()
        cmd = (f"cd {HERE} && ./run_scanner_docker.sh station build "
               f"--voxel {voxel_m:.4f}")
        self._run_command(cmd, "Building room from sweeps (pose-graph fusion)")

    def _on_station_view(self):
        room = OUTPUT_DIR / "room.ply"
        if not room.exists():
            QMessageBox.warning(self, "Not Found",
                                f"No room cloud yet: {room}\nRun 'Build Room' first.")
            return
        cmd = f"cd {HERE} && ./.venv/bin/python view_cloud.py {room}"
        self._run_command(cmd, "Viewing room.ply")

    def _on_station_clear(self):
        if self._count_sweeps() == 0:
            self._refresh_sweep_count()
            return
        reply = QMessageBox.question(
            self, "Clear Sweeps",
            f"Delete all {self._count_sweeps()} captured sweep(s)?")
        if reply == QMessageBox.StandardButton.Yes:
            subprocess.run(f"cd {HERE} && ./run_scanner_docker.sh station clear",
                           shell=True, capture_output=True, text=True)
            self._log("Cleared all sweeps.")
            self._refresh_sweep_count()

    def _on_browse_export(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Point Cloud", str(OUTPUT_DIR / "scan.ply"),
            "PLY Files (*.ply);;STL Files (*.stl);;All Files (*)"
        )
        if file_path:
            self.export_path_label.setText(file_path)

    def _on_clear_db(self):
        reply = QMessageBox.question(
            self, "Clear Database?",
            "This will delete rtabmap.db and lose all current map data.\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            if DB_PATH.exists():
                DB_PATH.unlink()
            QMessageBox.information(self, "Done", "Database cleared.")

    def _stop_scanner_containers(self) -> int:
        """Stop all running scanner-ros containers. Returns how many were stopped.

        Runs docker directly (not via _run_command) so it works even while a
        scan/camera command is still running — that's exactly when it's needed.
        Raises on docker failure so callers can decide how loud to be.
        """
        # End any command this GUI is tracking so it no longer reports "busy".
        if self.current_process and self.current_process.isRunning():
            self.current_process.terminate()
            self._log("[Containers] terminated tracked command")

        result = subprocess.run(
            "sudo docker ps -q --filter ancestor=scanner-ros:jazzy",
            shell=True, capture_output=True, text=True, timeout=30
        )
        ids = result.stdout.split()
        if not ids:
            return 0
        subprocess.run(
            ["sudo", "docker", "stop", *ids],
            capture_output=True, text=True, timeout=120
        )
        return len(ids)

    def _cleanup_before_scan(self):
        """Clear any leftover containers so a new scan gets the camera cleanly."""
        try:
            n = self._stop_scanner_containers()
            if n:
                self._log(f"[Auto-cleanup] cleared {n} leftover container(s) before scan")
        except Exception as e:
            self._log(f"[Auto-cleanup] warning: {e}")

    def _on_stop_containers(self):
        """Stop All Scanner Containers button handler (with user feedback)."""
        try:
            n = self._stop_scanner_containers()
        except Exception as e:
            self._log(f"[Stop Containers] error: {e}")
            QMessageBox.warning(self, "Error", f"Failed to stop containers:\n{e}")
            return

        if n == 0:
            self._log("[Stop Containers] none were running.")
            QMessageBox.information(
                self, "Containers", "No scanner containers were running."
            )
            return

        msg = f"Stopped {n} scanner container(s)."
        self._log(f"[Stop Containers] {msg}")
        self.status_label.setText("Status: Idle")
        self.status_label.setStyleSheet("font-weight: bold; color: green;")
        QMessageBox.information(self, "Containers", msg)

    def _on_rebuild_docker(self):
        reply = QMessageBox.question(
            self, "Rebuild Docker Image?",
            "This will rebuild scanner-ros:jazzy from Dockerfile.\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            cmd = f"cd {HERE} && ./run_scanner_docker.sh build"
            self._run_command(cmd, "Building Docker Image")

    def _on_stop(self):
        """Stop the current command."""
        if self.current_process and self.current_process.isRunning():
            self.current_process.terminate()
            self._log("[STOPPED by user]")
            self.status_label.setText("Status: Stopped")
            self.status_label.setStyleSheet("font-weight: bold; color: red;")

    def _on_command_output(self, text: str):
        """Append command output to log."""
        self._log(text)

    def _on_command_finished(self, code: int):
        """Handle command completion."""
        status = "Completed" if code == 0 else f"Failed (exit {code})"
        self.status_label.setText(f"Status: {status}")
        self.status_label.setStyleSheet(
            "font-weight: bold; color: green;" if code == 0
            else "font-weight: bold; color: red;"
        )
        self._log(f"\n[{status}]\n")
        self._refresh_sweep_count()

    def _on_telemetry_update(self, telemetry: dict):
        """Update telemetry display."""
        self.telemetry = telemetry
        self.status_label.setText(f"Status: {telemetry['status'].title()}")
        color = "green" if telemetry["status"] == "online" else "orange"
        self.status_label.setStyleSheet(f"font-weight: bold; color: {color};")

        self.nodes_label.setText(f"Nodes: {telemetry['nodes']}")
        self.db_size_label.setText(f"DB: {telemetry['map_size_mb']} MB")

        self.kf_label.setText(str(telemetry["keyframes"]))
        self.links_label.setText(str(telemetry["links"]))
        self.loop_label.setText(telemetry["last_loop_closure"] or "—")
        self._update_capture_guide()

    def _refresh_file_list(self):
        """Refresh the list of exported files."""
        if OUTPUT_DIR.exists():
            files = sorted(list(OUTPUT_DIR.glob("*.ply")) + list(OUTPUT_DIR.glob("*.stl")))
            file_text = "\n".join([f"• {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)"
                                   for f in files[-10:]])  # Last 10
            self.files_list.setText(file_text or "(no files yet)")

    def _refresh_roster(self):
        """Refresh the camera roster from cameras.json."""
        roster_file = HERE / "cameras.json"
        if roster_file.exists():
            try:
                with open(roster_file) as f:
                    data = json.load(f)
                text = json.dumps(data, indent=2)
                self.roster_text.setText(text)
            except Exception as e:
                self.roster_text.setText(f"Error reading cameras.json: {e}")

    def _update_system_info(self):
        """Update system info display."""
        info = [
            f"Docker Image: {DOCKER_IMAGE}",
            f"Project Dir: {HERE}",
            f"Output Dir: {OUTPUT_DIR}",
            f"Database: {DB_PATH}",
        ]
        self.info_text.setText("\n".join(info))

    def _log(self, text: str):
        """Append to log."""
        self.log_text.append(text)

    def closeEvent(self, event):
        """Clean up on exit."""
        self.monitor.stop()
        if self.current_process and self.current_process.isRunning():
            self.current_process.terminate()
        self.monitor.wait()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = ScannerGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
