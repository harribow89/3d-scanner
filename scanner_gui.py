#!/usr/bin/env python3
"""scanner_gui.py — PySide6 control panel for the ASUS Xtion 3D scanner.

Everything runs in the `scanner-ros:jazzy` Docker container through
run_scanner_docker.sh; this window just drives it and shows you what is
happening:

  * Scan tab      — live RGB + depth preview, camera test, single frame,
                    RTAB-Map SLAM (headless, or with the rtabmap_viz window),
                    live map statistics straight from rtabmap.db
  * Export & Clean — export the map to a PLY, isolate the object, build an STL,
                    browse / view the files in output/
  * Multi-Camera   — overlap check, auto-calibration, multi-cam RViz, 3-cam SLAM,
                    Matterport-style station sweeps
  * Setup          — the doctor checklist, simulated-camera switch, image build,
                    container stop, database reset
  * Log            — live output of whatever is running

Design rules that fix the old GUI's problems:
  * commands are argv lists from scanner_core (paths with spaces are fine)
  * one QProcess streams its output live; nothing is killed after 10 minutes
  * Stop sends SIGTERM to the launcher, whose trap `docker stop`s the container,
    so the USB cameras are really released
  * the status line says what is RUNNING; map numbers are shown separately
  * camera modes are refused up front when no Xtion is plugged in

    ./launch_gui.sh                       # normal use
    ./launch_gui.sh --smoke [--screenshot DIR]   # offscreen self-test
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QObject, QProcess, QProcessEnvironment, QSettings, QSize, Qt,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

import scanner_core as core

APP_QSS = """
QFrame#header { background: #1f2630; border-radius: 6px; }
QFrame#header QLabel { color: #e6edf3; }
QLabel#title { font-size: 17px; font-weight: 600; }
QLabel#running { font-weight: 600; }
QGroupBox { font-weight: 600; margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
QPushButton { padding: 6px 12px; }
QPushButton[class="primary"] { background: #2d7ff9; color: white; font-weight: 600; border: none; border-radius: 4px; }
QPushButton[class="primary"]:hover { background: #1f6fe0; }
QPushButton[class="primary"]:disabled { background: #8fb4ea; }
QPushButton#stop { background: #d9534f; color: white; font-weight: 700; border: none; border-radius: 4px; padding: 8px 18px; }
QPushButton#stop:disabled { background: #c9a1a0; }
QFrame#progress { background: #eef3fb; border: 1px solid #b6c8e6; border-radius: 6px; }
QFrame#progress[state="failed"] { background: #fdecea; border-color: #f0a19c; }
QFrame#progress[state="stuck"] { background: #fff4e5; border-color: #f0c27a; }
QFrame#progress[state="done"] { background: #eaf6ec; border-color: #9fd5a8; }
QLabel#progstep { font-weight: 600; }
QProgressBar { border: 1px solid #9aa4ae; border-radius: 4px; text-align: center; background: white; }
QProgressBar::chunk { background: #2d7ff9; border-radius: 3px; }
QFrame#progress[state="failed"] QProgressBar::chunk { background: #d9534f; }
QFrame#progress[state="done"] QProgressBar::chunk { background: #2ea043; }
QPushButton#mode { font-size: 14px; font-weight: 600; border: 2px solid #8b949e; border-radius: 6px; }
QPushButton#mode:checked { background: #238636; color: white; border-color: #238636; }
QLabel#hint { color: #555; }
QLabel.preview { background: #111; color: #999; border: 1px solid #333; }
"""

GREEN, RED, ORANGE, BLUE, GREY = "#3fb950", "#f85149", "#f0883e", "#58a6ff", "#9aa4ae"


# ----------------------------------------------------------------- process runner
class Runner(QObject):
    """Runs ONE command at a time with QProcess and streams merged output lines."""

    line = Signal(str)
    started = Signal(object)        # core.Command
    finished = Signal(int, object)  # exit code (-2 = stopped by user), core.Command

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self.cmd: core.Command | None = None
        self._buf = b""
        self._stopping = False
        self.crashed = False

    def busy(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self, cmd: core.Command, extra_env: dict | None = None) -> bool:
        if self.busy():
            return False
        self.cmd = cmd
        self._buf = b""
        self._stopping = False
        p = QProcess(self)
        p.setProgram(cmd.argv[0])
        p.setArguments(cmd.argv[1:])
        p.setWorkingDirectory(str(cmd.cwd))
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        for k, v in {**cmd.env, **(extra_env or {})}.items():
            env.insert(k, str(v))
        p.setProcessEnvironment(env)
        p.readyReadStandardOutput.connect(self._on_ready)
        p.finished.connect(self._on_finished)
        p.errorOccurred.connect(self._on_error)
        self.proc = p
        p.start()
        self.started.emit(cmd)
        return True

    def stop(self):
        """SIGTERM the launcher: its trap stops the container cleanly."""
        if not self.busy():
            return
        self._stopping = True
        self.line.emit("[stop] asking the launcher to shut down (containers are stopped by name)…")
        proc = self.proc
        proc.terminate()
        # The escalation must target THIS process only: by the time the timer
        # fires the user may have started a new command, which must not be killed.
        QTimer.singleShot(25000, lambda: self._kill_if_alive(proc))

    def _kill_if_alive(self, proc: QProcess):
        if self.proc is proc and proc.state() != QProcess.ProcessState.NotRunning:
            self.line.emit("[stop] still running after 25 s — killing it and stopping containers")
            proc.kill()
            QProcess.startDetached(str(core.LAUNCHER), ["stop"], str(core.HERE))

    def _on_ready(self):
        self._buf += bytes(self.proc.readAllStandardOutput().data())
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self.line.emit(raw.decode("utf-8", "replace").rstrip("\r"))

    def _on_finished(self, code: int, status):
        if self._buf:
            self.line.emit(self._buf.decode("utf-8", "replace"))
            self._buf = b""
        self.crashed = status == QProcess.ExitStatus.CrashExit and not self._stopping
        if self._stopping or status == QProcess.ExitStatus.CrashExit:
            code = -2 if self._stopping else code or -3
        cmd, self.cmd = self.cmd, None
        self.finished.emit(code, cmd)

    def _on_error(self, err):
        if err == QProcess.ProcessError.FailedToStart:
            self.line.emit(f"[error] could not start: {self.proc.program()}")
            cmd, self.cmd = self.cmd, None
            self.finished.emit(-1, cmd)


# ---------------------------------------------------------------- background polls
class TelemetryThread(QThread):
    """Polls rtabmap.db (lock-free) and the USB bus every 1.5 s."""
    telemetry = Signal(dict)
    cameras = Signal(list)

    def __init__(self):
        super().__init__()
        self._run = True

    def run(self):
        while self._run:
            self.telemetry.emit(core.read_telemetry())
            self.cameras.emit(core.detect_xtions())
            for _ in range(15):
                if not self._run:
                    break
                time.sleep(0.1)

    def stop(self):
        self._run = False


class ActivityThread(QThread):
    """While a job runs, measures how much CPU its processes are burning —
    the launcher's own tree plus our Docker containers (their processes hang
    off containerd, not the launcher). Emits CPU % (100 = one full core)."""
    cpu = Signal(float)

    def __init__(self):
        super().__init__()
        self._run = True
        self.root_pid = 0
        self.watch_containers = False

    def run(self):
        hz = os.sysconf("SC_CLK_TCK")
        last_ticks, last_t = None, None
        cont_pids, cont_checked = [], 0.0
        while self._run:
            if not self.root_pid:
                last_ticks = None
                time.sleep(0.3)
                continue
            now = time.time()
            if self.watch_containers and now - cont_checked > 4:
                cont_pids = core.container_pids()
                cont_checked = now
            ticks = core.tree_cpu_ticks([self.root_pid] + (cont_pids if self.watch_containers else []))
            if last_ticks is not None and now > last_t:
                self.cpu.emit(max(0.0, (ticks - last_ticks) / hz / (now - last_t) * 100.0))
            last_ticks, last_t = ticks, now
            for _ in range(15):
                if not self._run:
                    break
                time.sleep(0.1)

    def stop(self):
        self._run = False


class DoctorThread(QThread):
    done = Signal(list)

    def __init__(self, full: bool):
        super().__init__()
        self.full = full

    def run(self):
        self.done.emit(core.run_doctor(full=self.full))


# ------------------------------------------------------------------------ widgets
def primary(btn: QPushButton) -> QPushButton:
    btn.setProperty("class", "primary")
    btn.setMinimumHeight(34)
    return btn


class PreviewPanel(QWidget):
    """Two image panes (RGB, depth) fed from .live/ by live_preview_node.py."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.rgb = self._pane("RGB")
        self.depth = self._pane("Depth")
        row.addWidget(self.rgb)
        row.addWidget(self.depth)
        lay.addLayout(row)
        self.stats = QLabel("Preview off — press “Start live preview”.")
        self.stats.setObjectName("hint")
        self.stats.setWordWrap(True)
        lay.addWidget(self.stats)
        self._mtime = {}
        self._pix = {}

    def _pane(self, caption: str) -> QLabel:
        lab = QLabel(caption)
        lab.setProperty("class", "preview")
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setMinimumSize(320, 240)
        lab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return lab

    def refresh(self, running_camera: bool):
        status = core.read_live_status()
        rgb_p, depth_p = core.live_image_paths()
        for key, path, lab in (("rgb", rgb_p, self.rgb), ("depth", depth_p, self.depth)):
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if self._mtime.get(key) != m:
                pix = QPixmap(str(path))
                if not pix.isNull():
                    self._pix[key] = pix
                    self._mtime[key] = m
            if key in self._pix:
                lab.setPixmap(self._pix[key].scaled(
                    lab.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))

        if status is None:
            self.stats.setText("Preview off — press “Start live preview” (or start a scan)."
                               if not running_camera else "Starting the camera…")
            return
        cam = next(iter(status.get("cameras", {}).values()), None)
        if not status["fresh"]:
            self.stats.setText(f"Last frame {status['age_s']:.0f} s ago"
                               + (" — the camera stream has stopped." if running_camera else "."))
            return
        if not cam or (cam["rgb_frames"] == 0 and cam["depth_frames"] == 0):
            self.stats.setText("Camera node up, waiting for the first frames…")
            return
        self.stats.setText(
            f"RGB {cam['rgb_fps']} fps · depth {cam['depth_fps']} fps · "
            f"{cam['width']}×{cam['height']} · depth fill {cam['depth_fill_pct']}% · "
            f"median {cam['median_depth_m']} m")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        for key, lab in (("rgb", self.rgb), ("depth", self.depth)):
            if key in self._pix:
                lab.setPixmap(self._pix[key].scaled(
                    lab.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))


# ------------------------------------------------------------------- main window
class ScannerWindow(QMainWindow):
    def __init__(self, smoke: bool = False):
        super().__init__()
        self.setWindowTitle("3D Scanner — ASUS Xtion / RTAB-Map")
        if core.ICON_PNG.exists():
            self.setWindowIcon(QIcon(str(core.ICON_PNG)))
        self.resize(1280, 840)
        self.settings = QSettings("scanner", "control-panel")
        self.smoke = smoke
        self.cameras: list[core.Xtion] = []
        self.telemetry: dict = {}
        self.doctor_checks: list[core.Check] = []
        self.doctor_thread: DoctorThread | None = None

        self.runner = Runner(self)
        self.runner.line.connect(self._log)
        self.runner.line.connect(self._on_progress_line)
        self.runner.started.connect(self._on_started)
        self.runner.finished.connect(self._on_finished)

        self._build_ui()

        self.activity = ActivityThread()
        self.activity.cpu.connect(self._on_cpu)
        self.activity.start()
        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self._tick_progress)
        self.progress_timer.start(1000)
        self._restore_settings()

        self.poller = TelemetryThread()
        self.poller.telemetry.connect(self._on_telemetry)
        self.poller.cameras.connect(self._on_cameras)
        self.poller.start()

        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._tick_preview)
        self.preview_timer.start(150)

        self.files_timer = QTimer(self)
        self.files_timer.timeout.connect(self._refresh_files)
        self.files_timer.start(5000)

        self._log(f"Project: {core.HERE}")
        self._log("Tip: the Setup tab shows what is missing; the Log tab shows what every "
                  "button really ran.")
        self._run_doctor(full=not smoke)

    # ------------------------------------------------------------------ build
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)

        header = QFrame()
        header.setObjectName("header")
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 8, 14, 8)
        title = QLabel("3D Scanner")
        title.setObjectName("title")
        h.addWidget(title)
        h.addSpacing(18)
        self.cam_label = QLabel("○ looking for cameras…")
        h.addWidget(self.cam_label)
        h.addSpacing(18)
        self.run_label = QLabel("Idle")
        self.run_label.setObjectName("running")
        self.run_label.setMinimumWidth(80)
        self.run_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        h.addWidget(self.run_label, 1)
        self.map_label = QLabel("Map: none")
        self.map_label.setMinimumWidth(240)
        h.addWidget(self.map_label)
        h.addSpacing(14)
        self.stop_btn = QPushButton("■ Stop  (F8)")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setShortcut("F8")
        self.stop_btn.setToolTip("Stop the running command and free the camera (F8)")
        self.stop_btn.clicked.connect(self._on_stop)
        h.addWidget(self.stop_btn)
        root.addWidget(header)

        # ---- job progress (export / clean / mesh / any finite job)
        self.prog_frame = QFrame()
        self.prog_frame.setObjectName("progress")
        pv = QVBoxLayout(self.prog_frame)
        pv.setContentsMargins(12, 6, 12, 6)
        pv.setSpacing(3)
        top = QHBoxLayout()
        self.prog_step = QLabel("")
        self.prog_step.setObjectName("progstep")
        top.addWidget(self.prog_step, 1)
        self.prog_activity = QLabel("")
        top.addWidget(self.prog_activity)
        pv.addLayout(top)
        self.prog_bar = QProgressBar()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setTextVisible(True)
        self.prog_bar.setMinimumHeight(22)
        pv.addWidget(self.prog_bar)
        self.prog_detail = QLabel("")
        self.prog_detail.setObjectName("hint")
        pv.addWidget(self.prog_detail)
        self.prog_frame.hide()
        root.addWidget(self.prog_frame)
        self._job_t0 = 0.0
        self._last_out_t = 0.0
        self._stage = ""
        self._cpu = None
        self._chain_step = ""       # "Step 2 of 3 · Cleaning + mesh" during Finish
        self._prog_hide_at = 0.0

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_scan_tab(), "Scan")
        self.tabs.addTab(self._build_export_tab(), "Export && Clean")
        self.tabs.addTab(self._build_multicam_tab(), "Multi-Camera (advanced)")
        self.tabs.addTab(self._build_setup_tab(), "Setup")
        self.tabs.addTab(self._build_log_tab(), "Log")
        root.addWidget(self.tabs, 1)
        self.statusBar().showMessage("Ready")

    def _build_scan_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)

        self.preview = PreviewPanel()
        lay.addWidget(self.preview, 3)

        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(0, 0, 6, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(right_w)
        scroll.setMinimumWidth(470)
        lay.addWidget(scroll, 2)

        g0 = QGroupBox("What are you scanning?")
        l0 = QVBoxLayout(g0)
        row = QHBoxLayout()
        self.mode_btns: dict[str, QPushButton] = {}
        for key, m in core.SCAN_MODES.items():
            b = QPushButton(("🏠  " if key == "room" else "📦  ") + m["label"])
            b.setCheckable(True)
            b.setMinimumHeight(38)
            b.setObjectName("mode")
            b.clicked.connect(lambda _=False, k=key: self._set_mode(k))
            row.addWidget(b)
            self.mode_btns[key] = b
        l0.addLayout(row)
        self.mode_tips = QLabel("")
        self.mode_tips.setObjectName("hint")
        self.mode_tips.setWordWrap(True)
        l0.addWidget(self.mode_tips)
        right.addWidget(g0)
        self.scan_mode = core.DEFAULT_SCAN_MODE

        g1 = QGroupBox("1 · Camera")
        l1 = QVBoxLayout(g1)
        self.preview_btn = primary(QPushButton("▶  Start live preview  (F5)"))
        self.preview_btn.setShortcut("F5")
        self.preview_btn.setToolTip("Streams RGB + depth into the panes on the left. Runs until Stop. (F5)")
        self.preview_btn.clicked.connect(lambda: self._run(core.cmd_preview()))
        l1.addWidget(self.preview_btn)
        row = QHBoxLayout()
        b = QPushButton("Test camera (20 s)")
        b.setToolTip("Starts the driver, measures the frame rate for 20 s, then stops.")
        b.clicked.connect(lambda: self._run(core.cmd_camera_test(20)))
        row.addWidget(b)
        b = QPushButton("Capture one frame → PLY")
        b.setToolTip("Saves a single point cloud to output/single_frame.ply")
        b.clicked.connect(lambda: self._run(core.cmd_snap()))
        row.addWidget(b)
        l1.addLayout(row)
        right.addWidget(g1)

        g2 = QGroupBox("2 · Scan (RTAB-Map SLAM)")
        l2 = QVBoxLayout(g2)
        self.fresh_cb = QCheckBox("Start a new map (clears the previous one)")
        self.fresh_cb.setChecked(True)
        l2.addWidget(self.fresh_cb)
        self.scan_btn = primary(QPushButton("●  Start scan  (F6)"))
        self.scan_btn.setShortcut("F6")
        self.scan_btn.setToolTip("Headless SLAM with the live preview on the left. "
                                 "Move the camera slowly; press Stop (F8) to finish. (F6)")
        self.scan_btn.clicked.connect(self._on_scan)
        l2.addWidget(self.scan_btn)
        b = QPushButton("Scan with the RTAB-Map 3D window  (F7)")
        b.setShortcut("F7")
        b.setToolTip("Same SLAM, plus rtabmap_viz showing the growing 3D map. "
                     "Close that window (or press Stop) to finish. (F7)")
        b.clicked.connect(self._on_scan_window)
        l2.addWidget(b)
        tips = QLabel("Move slowly and keep lots of overlap between views. If the "
                      "keyframe count stops growing, tracking is lost: go back to the "
                      "last spot that worked and hold still for a second.")
        tips.setObjectName("hint")
        tips.setWordWrap(True)
        l2.addWidget(tips)
        right.addWidget(g2)

        g3 = QGroupBox("Map progress (live from rtabmap.db)")
        l3 = QGridLayout(g3)
        self.stat_labels = {}
        for i, (key, text) in enumerate([("nodes", "Keyframes"), ("links", "Links"),
                                         ("loop_closures", "Loop closures"),
                                         ("map_size_mb", "Database"),
                                         ("updated", "Last update")]):
            l3.addWidget(QLabel(text + ":"), i // 2, (i % 2) * 2)
            lab = QLabel("—")
            lab.setFont(QFont("", -1, QFont.Weight.Bold))
            l3.addWidget(lab, i // 2, (i % 2) * 2 + 1)
            self.stat_labels[key] = lab
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hint")
        self.hint_label.setWordWrap(True)
        l3.addWidget(self.hint_label, 3, 0, 1, 4)
        right.addWidget(g3)

        g4 = QGroupBox("3 · Finished scanning?")
        l4 = QVBoxLayout(g4)
        self.finish_btn = primary(QPushButton("✔  Finish: export → clean → mesh → view  (F9)"))
        self.finish_btn.setShortcut("F9")
        self.finish_btn.setToolTip("Stops the scan if it is still running, then exports the map "
                                   "with the current mode's settings, isolates the room/object, "
                                   "builds an STL and opens the result. (F9)")
        self.finish_btn.clicked.connect(self._on_finish)
        l4.addWidget(self.finish_btn)
        b = QPushButton("Fine-tune export and cleaning →")
        b.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        l4.addWidget(b)
        right.addWidget(g4)
        right.addStretch()
        return w

    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        g = QGroupBox("Export the map to a point cloud")
        grid = QGridLayout(g)
        grid.addWidget(QLabel("Preset:"), 0, 0)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(core.EXPORT_PRESETS) + ["Custom"])
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        grid.addWidget(self.preset_combo, 0, 1)
        grid.addWidget(QLabel("Name:"), 0, 2)
        self.name_edit = QLineEdit("scan")
        self.name_edit.setToolTip("RTAB-Map writes output/<name>_cloud.ply")
        self.name_edit.textChanged.connect(self._update_export_target)
        grid.addWidget(self.name_edit, 0, 3)
        grid.addWidget(QLabel("Max range (m):"), 1, 0)
        self.range_spin = QDoubleSpinBox()
        self.range_spin.setRange(0.5, 10.0)
        self.range_spin.setSingleStep(0.5)
        self.range_spin.setValue(4.0)
        self.range_spin.setToolTip("Points farther than this from the camera are dropped "
                                   "(Xtion depth is poor beyond ~3.5 m).")
        grid.addWidget(self.range_spin, 1, 1)
        grid.addWidget(QLabel("Voxel (mm):"), 1, 2)
        self.voxel_spin = QDoubleSpinBox()
        self.voxel_spin.setRange(0.5, 50.0)
        self.voxel_spin.setDecimals(1)
        self.voxel_spin.setSingleStep(0.5)
        self.voxel_spin.setValue(5.0)
        self.voxel_spin.setToolTip("Downsampling grid. Smaller keeps more detail but makes "
                                   "bigger, slower files. The Xtion's own depth noise is "
                                   "~1.5 mm at 0.8 m and ~1 cm at 3 m.")
        grid.addWidget(self.voxel_spin, 1, 3)
        grid.addWidget(QLabel("Detail:"), 2, 0)
        self.decim_combo = QComboBox()
        self.decim_combo.addItem("Full (every depth pixel)", 1)
        self.decim_combo.addItem("High (every 2nd pixel)", 2)
        self.decim_combo.addItem("Draft (every 4th pixel)", 4)
        self.decim_combo.setToolTip("How many depth pixels of each keyframe go into the cloud. "
                                    "RTAB-Map's own default is every 4th, which looks blocky.")
        grid.addWidget(self.decim_combo, 2, 1)
        self.export_target = QLabel("")
        self.export_target.setObjectName("hint")
        grid.addWidget(self.export_target, 3, 0, 1, 3)
        self.export_btn = primary(QPushButton("Export cloud"))
        self.export_btn.clicked.connect(self._on_export)
        grid.addWidget(self.export_btn, 3, 3)
        lay.addWidget(g)

        g = QGroupBox("Clean / isolate the object and build a mesh")
        grid = QGridLayout(g)
        grid.addWidget(QLabel("Strategy:"), 0, 0)
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItems(core.ISOLATION_STRATEGIES)
        self.strategy_combo.setToolTip(
            "raw_clean: outliers only · largest_cluster: main blob · tabletop_object: "
            "remove the table plane then keep the object · center_focus: crop to the "
            "middle · aggressive_hybrid: all of it · all_variants: save every strategy")
        grid.addWidget(self.strategy_combo, 0, 1)
        self.clean_input = QLabel("")
        self.clean_input.setObjectName("hint")
        grid.addWidget(self.clean_input, 0, 2, 1, 2)
        b = QPushButton("Apply isolation")
        b.clicked.connect(lambda: self._on_clean(stl=False))
        grid.addWidget(b, 1, 0)
        b = QPushButton("Make STL mesh")
        b.clicked.connect(lambda: self._on_clean(stl=True))
        grid.addWidget(b, 1, 1)
        b = primary(QPushButton("Export + clean + STL"))
        b.clicked.connect(self._on_export_clean_stl)
        grid.addWidget(b, 1, 2, 1, 2)
        lay.addWidget(g)

        g = QGroupBox("Files in output/")
        v = QVBoxLayout(g)
        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels(["File", "Size", "Modified"])
        fh = self.files.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        fh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.files.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.files.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files.itemSelectionChanged.connect(self._update_clean_input)
        self.files.doubleClicked.connect(lambda _: self._on_view())
        v.addWidget(self.files)
        row = QHBoxLayout()
        b = QPushButton("View in 3D")
        b.setToolTip("Opens the selected PLY/STL in the Open3D viewer (double-click also works)")
        b.clicked.connect(self._on_view)
        row.addWidget(b)
        b = QPushButton("Open folder")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(core.OUTPUT_DIR))))
        row.addWidget(b)
        b = QPushButton("Refresh")
        b.clicked.connect(self._refresh_files)
        row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        lay.addWidget(g, 1)

        self._update_export_target()
        self._refresh_files()
        return w

    def _build_multicam_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel("For rigs with 2–3 Xtions (roster in cameras.json). Single-camera "
                       "scanning does not need anything here.")
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        g = QGroupBox("Sensor head (all cameras streamed by one service)")
        v = QVBoxLayout(g)
        lab = QLabel("The cameras are run by a sensor head: the Matterport i.MX6 board, or this "
                     "PC. Every multi-camera button below then uses it. Leave the address empty "
                     "to drive USB cameras directly the old way.")
        lab.setObjectName("hint")
        lab.setWordWrap(True)
        v.addWidget(lab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Head address:"))
        self.head_edit = QLineEdit()
        self.head_edit.setPlaceholderText("e.g. 127.0.0.1 (this PC) or matterport-head.local")
        row.addWidget(self.head_edit, 1)
        self.head_local_btn = QPushButton("▶ Run head on this PC")
        self.head_local_btn.setToolTip("Streams every Xtion plugged into this PC and sets the "
                                       "address to 127.0.0.1. Runs in the background.")
        self.head_local_btn.clicked.connect(self._toggle_local_head)
        row.addWidget(self.head_local_btn)
        v.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("Check head (10 s)")
        b.setToolTip("Frame rate per camera and stream, and the head's own status")
        b.clicked.connect(lambda: self._with_head(core.cmd_head_probe))
        row.addWidget(b)
        b = primary(QPushButton("Calibrate cameras (feature matching)"))
        b.setToolTip("Matches what neighbouring cameras both see in colour, lifts it to 3-D and "
                     "solves each camera's pose. Keep the rig still for ~5 s. Writes "
                     "output/camera_extrinsics.json only when every camera is linked.")
        b.clicked.connect(lambda: self._with_head(core.cmd_calibrate_head))
        row.addWidget(b)
        self.head_status = QLabel("")
        self.head_status.setObjectName("hint")
        row.addWidget(self.head_status, 1)
        v.addLayout(row)
        lay.addWidget(g)
        self.head_proc: QProcess | None = None

        g = QGroupBox("Calibration without a head (USB cameras, shape matching)")
        v = QVBoxLayout(g)
        lab = QLabel("Aim all cameras at a STRUCTURED scene 1–1.5 m away so adjacent "
                     "cameras overlap 30–50 %. Cameras are captured one at a time. "
                     "See CALIBRATION_GUIDE.md.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        row = QHBoxLayout()
        b = QPushButton("1 · Check camera overlap")
        b.clicked.connect(lambda: self._run(core.cmd_overlap()))
        row.addWidget(b)
        b = QPushButton("2 · Auto-calibrate")
        b.clicked.connect(lambda: self._run(core.cmd_calibrate()))
        row.addWidget(b)
        v.addLayout(row)
        lay.addWidget(g)

        g = QGroupBox("Live")
        row = QHBoxLayout(g)
        b = QPushButton("Multi-camera RViz")
        b.setToolTip("All cameras' clouds in one RViz window using the calibrated extrinsics.")
        b.clicked.connect(lambda: self._run(core.cmd_multi()))
        row.addWidget(b)
        b = QPushButton("3-camera SLAM (experimental)")
        b.setToolTip("Needs GOOD calibration and all 3 RGB-D streams (marginal on one USB-2 bus). "
                     "Takes ~40 s before the window appears.")
        b.clicked.connect(self._on_multi_slam)
        row.addWidget(b)
        lay.addWidget(g)

        g = QGroupBox("Station / sweep scan (Matterport-style)")
        v = QVBoxLayout(g)
        lab = QLabel("Capture a sweep from each tripod position (N stacked frames per camera, "
                     "fused with the calibration), then Build registers all sweeps into "
                     "output/room.ply.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Frames per camera:"))
        self.frames_spin = QSpinBox()
        self.frames_spin.setRange(1, 60)
        self.frames_spin.setValue(12)
        row.addWidget(self.frames_spin)
        self.sweeps_label = QLabel("")
        row.addWidget(self.sweeps_label)
        row.addStretch()
        v.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("Capture sweep")
        b.clicked.connect(lambda: self._run(core.cmd_station("capture", "--frames",
                                                             str(self.frames_spin.value()))))
        row.addWidget(b)
        b = QPushButton("Build room")
        b.clicked.connect(self._on_station_build)
        row.addWidget(b)
        b = QPushButton("View room")
        b.clicked.connect(lambda: self._view_file(core.OUTPUT_DIR / "room.ply"))
        row.addWidget(b)
        b = QPushButton("Clear sweeps")
        b.clicked.connect(self._on_station_clear)
        row.addWidget(b)
        v.addLayout(row)
        lay.addWidget(g)

        g = QGroupBox("cameras.json")
        v = QVBoxLayout(g)
        self.roster_text = QPlainTextEdit()
        self.roster_text.setReadOnly(True)
        self.roster_text.setMaximumHeight(150)
        try:
            self.roster_text.setPlainText(core.CAMERAS_JSON.read_text())
        except OSError as e:
            self.roster_text.setPlainText(f"(cannot read cameras.json: {e})")
        v.addWidget(self.roster_text)
        lay.addWidget(g)
        lay.addStretch()
        self._refresh_sweeps()
        return w

    def _build_setup_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        g = QGroupBox("Checklist")
        v = QVBoxLayout(g)
        self.doctor_table = QTableWidget(0, 3)
        self.doctor_table.setHorizontalHeaderLabels(["Check", "Result", "What to do"])
        hh = self.doctor_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.doctor_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.doctor_table.setWordWrap(True)
        self.doctor_table.setMinimumHeight(300)
        v.addWidget(self.doctor_table)
        row = QHBoxLayout()
        self.doctor_btn = QPushButton("Re-run checks")
        self.doctor_btn.clicked.connect(lambda: self._run_doctor(full=True))
        row.addWidget(self.doctor_btn)
        self.fake_cb = QCheckBox("Use the SIMULATED camera (no hardware) — for trying the app")
        self.fake_cb.setToolTip("Every camera mode then uses tests/fake_camera.py, a synthetic "
                                "RGB-D scene, so you can try preview / scan / export without an Xtion.")
        self.fake_cb.toggled.connect(self._on_fake_toggled)
        row.addWidget(self.fake_cb)
        row.addStretch()
        v.addLayout(row)
        lay.addWidget(g)

        g = QGroupBox("Maintenance")
        grid = QGridLayout(g)
        b = QPushButton("Stop all scanner containers")
        b.setToolTip("Use this if a camera seems stuck: leftover containers hold the USB device.")
        b.clicked.connect(self._on_stop_all)
        grid.addWidget(b, 0, 0)
        b = QPushButton("Build / rebuild the Docker image")
        b.clicked.connect(self._on_build)
        grid.addWidget(b, 0, 1)
        b = QPushButton("Clear the map database")
        b.clicked.connect(self._on_clear_db)
        grid.addWidget(b, 1, 0)
        b = QPushButton("Open ROS logs folder")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(core.LOG_DIR))))
        grid.addWidget(b, 1, 1)
        lay.addWidget(g)

        paths = QLabel(f"Project: {core.HERE}\nOutput: {core.OUTPUT_DIR}\nMap database: {core.DB_PATH}\n"
                       f"Docker image: {core.DOCKER_IMAGE}\nPython: {core.python_exe()}")
        paths.setObjectName("hint")
        paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(paths)
        lay.addStretch()
        return w

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(8000)
        self.log.setFont(QFont("Monospace", 9))
        lay.addWidget(self.log)
        row = QHBoxLayout()
        b = QPushButton("Clear")
        b.clicked.connect(self.log.clear)
        row.addWidget(b)
        b = QPushButton("Open ROS logs folder")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(core.LOG_DIR))))
        row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        return w

    # --------------------------------------------------------------- settings
    def _restore_settings(self):
        s = self.settings
        # The simulated camera is deliberately NOT restored: a test session that
        # left it on made the real Xtion silently ignored on the next launch.
        # Use --fake-camera or the Setup tab switch each time.
        self.fake_cb.setChecked(False)
        self.name_edit.setText(s.value("export_name", "scan", type=str) or "scan")
        preset = s.value("preset", list(core.EXPORT_PRESETS)[0], type=str)
        if preset in [self.preset_combo.itemText(i) for i in range(self.preset_combo.count())]:
            self.preset_combo.setCurrentText(preset)
        if preset == "Custom":
            self.range_spin.setValue(s.value("max_range", 4.0, type=float))
            self.voxel_spin.setValue(s.value("voxel_mm", 5.0, type=float))
            self._set_decimation(s.value("decimation", 2, type=int))
            self.strategy_combo.setCurrentText(s.value("strategy", "raw_clean", type=str))
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self._set_mode(s.value("scan_mode", core.DEFAULT_SCAN_MODE, type=str))
        self.head_edit.setText(s.value("head", "", type=str) or "")

    def _save_settings(self):
        s = self.settings
        s.setValue("export_name", self.name_edit.text())
        s.setValue("preset", self.preset_combo.currentText())
        s.setValue("max_range", self.range_spin.value())
        s.setValue("voxel_mm", self.voxel_spin.value())
        s.setValue("decimation", self._decimation())
        s.setValue("strategy", self.strategy_combo.currentText())
        s.setValue("geometry", self.saveGeometry())
        s.setValue("scan_mode", self.scan_mode)
        # a local head (127.0.0.1) only exists while this window runs it
        head = self.head_edit.text().strip()
        s.setValue("head", "" if head.startswith("127.") else head)

    # ---------------------------------------------------------------- running
    def _extra_env(self) -> dict:
        env = {core.FAKE_CAMERA_ENV: "1" if self.fake_cb.isChecked() else "0"}
        head = self.head_edit.text().strip() if hasattr(self, "head_edit") else ""
        if head:
            env[core.HEAD_ENV] = head
        return env

    # ------------------------------------------------------------ sensor head
    def _with_head(self, builder):
        head = self.head_edit.text().strip()
        if not head:
            QMessageBox.information(self, "Sensor head",
                                    "Enter the head's address first, or press "
                                    "“Run head on this PC”.")
            return
        self._run(builder(head))

    def _toggle_local_head(self):
        if self.head_proc and self.head_proc.state() != QProcess.ProcessState.NotRunning:
            self.head_proc.terminate()
            if not self.head_proc.waitForFinished(8000):
                self.head_proc.kill()
            return
        if self.runner.busy() and self.runner.cmd and self.runner.cmd.needs_camera:
            QMessageBox.warning(self, "Busy", "Stop the running camera command first — "
                                "the head needs the cameras to itself.")
            return
        argv = core.head_local_argv()
        p = QProcess(self)
        p.setProgram(argv[0])
        p.setArguments(argv[1:])
        p.setWorkingDirectory(str(core.HERE))
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(
            lambda: [self._log("[head] " + ln) for ln in
                     bytes(p.readAllStandardOutput().data()).decode("utf-8", "replace").splitlines()
                     if ln.strip()])
        p.finished.connect(self._on_local_head_finished)
        self.head_proc = p
        p.start()
        self.head_edit.setText("127.0.0.1")
        self.head_local_btn.setText("■ Stop head on this PC")
        self.head_status.setText("head running on this PC")
        self._log("[head] started: " + " ".join(argv))

    def _on_local_head_finished(self, code, _status):
        self.head_local_btn.setText("▶ Run head on this PC")
        self.head_status.setText(f"head on this PC stopped (exit {code})")
        self._log(f"[head] stopped (exit {code})")

    def _run(self, cmd: core.Command) -> bool:
        if self.runner.busy():
            QMessageBox.information(
                self, "Busy", f"“{self.runner.cmd.title}” is still running.\n\n"
                "Press Stop first (it also frees the camera).")
            return False
        if cmd.needs_camera and not self.fake_cb.isChecked():
            cams = core.detect_xtions()
            if len(cams) < cmd.needs_camera:
                QMessageBox.warning(
                    self, "No camera",
                    f"This needs {cmd.needs_camera} depth camera(s); "
                    f"{core.camera_summary(cams).lower()}.\n\n"
                    "Plug the Xtion into this machine (it should show up in the header "
                    "within a couple of seconds), or tick “simulated camera” on the "
                    "Setup tab to try the app without hardware.")
                return False
        self._log("")
        self._log(f"[{datetime.now():%H:%M:%S}] ▶ {cmd.title}")
        self._log(f"$ {cmd.shell_preview()}")
        return self.runner.start(cmd, self._extra_env())

    def _on_started(self, cmd: core.Command):
        self.run_label.setText(f"Running: {cmd.title}" + ("  (press Stop to finish)" if cmd.long_running else ""))
        self.run_label.setStyleSheet(f"color: {ORANGE};")
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage(cmd.title)
        self._progress_begin(cmd)

    # ------------------------------------------------------------- progress
    @staticmethod
    def _reports_progress(cmd: core.Command) -> bool:
        """Commands whose output parse_progress understands (a real % bar);
        anything else finite gets a moving 'busy' bar + the activity readout."""
        a = cmd.argv
        return (len(a) > 1 and a[1] == "export") or any(x.endswith("point_cloud_tools.py") for x in a)

    def _set_prog_state(self, state: str):
        self.prog_frame.setProperty("state", state)
        self.prog_frame.style().unpolish(self.prog_frame)
        self.prog_frame.style().polish(self.prog_frame)
        for w in self.prog_frame.findChildren(QWidget):
            w.style().unpolish(w)
            w.style().polish(w)

    def _progress_begin(self, cmd: core.Command):
        if cmd.long_running:
            self.prog_frame.hide()          # scans/previews have their own live readouts
            return
        now = time.time()
        self._job_t0 = self._last_out_t = now
        self._cpu = None
        self._prog_hide_at = 0.0
        self._stage = "Starting…"
        self._set_prog_state("")
        self.prog_step.setText((self._chain_step + "  ·  " if self._chain_step else "") + cmd.title)
        if self._reports_progress(cmd):
            self.prog_bar.setRange(0, 100)
            self.prog_bar.setValue(0)
            self.prog_bar.setFormat("%p%")
        else:
            self.prog_bar.setRange(0, 0)    # animated 'busy' bar
        self.prog_activity.setText("")
        self.prog_detail.setText(self._stage)
        self.prog_frame.show()
        self.activity.watch_containers = len(cmd.argv) > 1 and cmd.argv[0] == str(core.LAUNCHER)
        self.activity.root_pid = int(self.runner.proc.processId() or 0) if self.runner.proc else 0

    def _on_progress_line(self, line: str):
        if not self.prog_frame.isVisible() or not self.runner.busy():
            return
        self._last_out_t = time.time()
        p = core.parse_progress(line)
        if p is None:
            return
        pct, label = p
        if self.prog_bar.maximum() > 0 and pct >= self.prog_bar.value():
            self.prog_bar.setValue(pct)
        if label:
            self._stage = label
        self._tick_progress()

    def _on_cpu(self, pct: float):
        self._cpu = pct

    @staticmethod
    def _fmt_secs(s: float) -> str:
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"

    def _tick_progress(self):
        if not self.prog_frame.isVisible():
            return
        now = time.time()
        if not self.runner.busy():
            if self._prog_hide_at and now >= self._prog_hide_at and not self._chain_step:
                self.prog_frame.hide()
            return
        elapsed = now - self._job_t0
        quiet = now - self._last_out_t
        self.prog_detail.setText(f"{self._stage}   ·   {self._fmt_secs(elapsed)} elapsed   ·   "
                                 f"last output {self._fmt_secs(quiet)} ago")
        mem = core.mem_available_gb()
        if mem is not None:
            self.prog_detail.setText(self.prog_detail.text() + f"   ·   RAM free {mem:.1f} GB")
            if mem < 1.0:
                self.prog_activity.setText(f"⚠ RAM almost full ({mem:.1f} GB free) — "
                                           "this job may be killed")
                self.prog_activity.setStyleSheet(f"color: {RED}; font-weight: 700;")
                return
        cpu = self._cpu
        if cpu is None:
            self.prog_activity.setText("checking activity…")
            self.prog_activity.setStyleSheet(f"color: {GREY};")
        elif cpu >= 5:
            self.prog_activity.setText(f"● working — CPU {cpu:.0f} %")
            self.prog_activity.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
            if self.prog_frame.property("state") == "stuck":
                self._set_prog_state("")
        elif quiet > 45:
            self.prog_activity.setText(f"▲ no activity for {self._fmt_secs(quiet)} — may be stuck "
                                       "(Stop is safe)")
            self.prog_activity.setStyleSheet(f"color: {ORANGE}; font-weight: 600;")
            if self.prog_frame.property("state") != "stuck":
                self._set_prog_state("stuck")
        else:
            self.prog_activity.setText(f"○ waiting — CPU {cpu:.0f} %")
            self.prog_activity.setStyleSheet(f"color: {GREY};")

    def _progress_end(self, code: int, cmd: core.Command | None):
        self.activity.root_pid = 0
        if cmd is None or cmd.long_running or not self.prog_frame.isVisible():
            return
        elapsed = self._fmt_secs(time.time() - self._job_t0)
        if self.prog_bar.maximum() == 0:
            self.prog_bar.setRange(0, 100)
        if code == 0:
            self.prog_bar.setValue(100)
            self._set_prog_state("done")
            self.prog_activity.setText("✔ finished")
            self.prog_activity.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
            self.prog_detail.setText(f"Done in {elapsed}")
            self._prog_hide_at = time.time() + 12
        elif code == -2:
            self._set_prog_state("stuck")
            self.prog_activity.setText("■ stopped")
            self.prog_activity.setStyleSheet(f"color: {GREY}; font-weight: 600;")
            self.prog_detail.setText(f"Stopped by you after {elapsed}, during: {self._stage}")
            self._prog_hide_at = time.time() + 12
        else:
            why = core.describe_exit(code, getattr(self.runner, "crashed", False))
            self._set_prog_state("failed")
            self.prog_activity.setText(f"✘ {why.split(' — ')[0]}")
            self.prog_activity.setStyleSheet(f"color: {RED}; font-weight: 700;")
            self.prog_detail.setText(f"Stopped working after {elapsed}, during: {self._stage}. "
                                     f"{why[0].upper() + why[1:]}. The Log tab has the details.")
            self._prog_hide_at = 0.0          # keep a failure on screen

    def _on_finished(self, code: int, cmd: core.Command | None):
        self._progress_end(code, cmd)
        title = cmd.title if cmd else "command"
        if code == 0:
            text, colour, msg = "Idle", GREY, f"Finished: {title}"
        elif code == -2:
            text, colour, msg = "Idle (stopped)", GREY, f"Stopped: {title}"
        else:
            text, colour, msg = f"Failed: {title} (exit {code})", RED, f"Failed (exit {code}): {title} — see the Log tab"
            self.tabs.setCurrentIndex(4)
        self.run_label.setText(text)
        self.run_label.setStyleSheet(f"color: {colour};")
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage(msg, 15000)
        self._log(f"[{datetime.now():%H:%M:%S}] {msg}")
        self._refresh_files()
        self._refresh_sweeps()

    def _on_stop(self):
        if self.runner.busy():
            self.runner.stop()
        else:
            self._on_stop_all()

    def _on_stop_all(self):
        self._log("[containers] stopping every scanner container…")
        n = core.stop_containers()
        self._log(f"[containers] stopped {n}")
        self.statusBar().showMessage(f"Stopped {n} container(s)", 8000)

    # ------------------------------------------------------------ scan modes
    def _set_mode(self, key: str):
        if key not in core.SCAN_MODES:
            key = core.DEFAULT_SCAN_MODE
        self.scan_mode = key
        m = core.SCAN_MODES[key]
        for k, b in self.mode_btns.items():
            b.setChecked(k == key)
        self.mode_tips.setText(m["tips"])
        # keep the Export tab in step with the mode (unless the user chose Custom)
        if self.preset_combo.currentText() != "Custom":
            self.preset_combo.setCurrentText(m["export"])

    def _run_then(self, cmd: core.Command, on_ok, step: str = "") -> bool:
        """Run cmd; when it finishes with exit 0 call on_ok() (one shot).
        `step` ("Step 1 of 2 · Export") labels the progress panel."""
        def done(code, _cmd):
            self.runner.finished.disconnect(done)
            if code == 0:
                QTimer.singleShot(0, on_ok)
            else:
                self._chain_step = ""
                self._log(f"[finish] stopped: “{cmd.title}” ended with exit {code}")
        self._chain_step = step
        if self._run(cmd):
            self.runner.finished.connect(done)
            return True
        self._chain_step = ""
        return False

    def _on_scan(self):
        self._run(core.cmd_map(self.fresh_cb.isChecked(), self.scan_mode))

    def _on_scan_window(self):
        self._run(core.cmd_gui(self.fresh_cb.isChecked(), self.scan_mode))

    def _scanning(self) -> bool:
        c = self.runner.cmd
        return self.runner.busy() and c is not None and len(c.argv) > 1 and \
            c.argv[1] in ("map", "gui", "multi_slam")

    def _on_finish(self):
        """One click from a finished (or running) scan to a cleaned cloud + STL."""
        if self._scanning():
            self._log("[finish] stopping the scan first (RTAB-Map saves the map)…")

            def after_stop(_code, _cmd):
                self.runner.finished.disconnect(after_stop)
                QTimer.singleShot(500, self._on_finish)
            self.runner.finished.connect(after_stop)
            self.runner.stop()
            return
        if self.runner.busy():
            QMessageBox.information(self, "Busy", f"“{self.runner.cmd.title}” is still running.")
            return
        t = core.read_telemetry()
        if t["nodes"] == 0:
            QMessageBox.warning(self, "No map",
                                "The map is empty, so there is nothing to export.\n\n"
                                "Start a scan, move the camera until the keyframe count "
                                "grows, then press Finish.")
            return
        m = core.SCAN_MODES[self.scan_mode]
        preset = core.EXPORT_PRESETS[m["export"]]
        name = f"{m['name']}_{datetime.now():%Y%m%d_%H%M%S}"
        self.preset_combo.setCurrentText(m["export"])
        self.name_edit.setText(name)
        voxel, strategy = preset["voxel"], preset["strategy"]
        cloud = core.export_cloud_path(name)
        cleaned = cloud.with_name(f"{cloud.stem}_{strategy}.ply")
        stl = core.OUTPUT_DIR / f"{name}.stl"
        self._log(f"[finish] {m['label']}: export → {strategy} → {stl.name}")

        def view():
            self._chain_step = ""
            self._prog_hide_at = time.time() + 15
            self.prog_step.setText(f"Finished — {m['label']} scan exported, cleaned and meshed")
            self._refresh_files()
            target = cleaned if cleaned.exists() else cloud
            self._log(f"[finish] done. Cloud: {target.name}   Mesh: {stl.name if stl.exists() else '(none)'}")
            self.statusBar().showMessage(f"Finished: {target.name} and {stl.name}", 20000)
            self._view_file(target)

        def clean():
            self._run_then(core.cmd_isolate(cloud, strategy, voxel, stl), view,
                           "Step 2 of 2 · Clean + mesh")

        self._run_then(core.cmd_export(name, preset["max_range"], voxel,
                                       decimation=preset.get("decimation", 2)), clean,
                       "Step 1 of 2 · Export")

    # --------------------------------------------------------------- handlers
    def _decimation(self) -> int:
        return int(self.decim_combo.currentData() or 2)

    def _set_decimation(self, d: int):
        i = self.decim_combo.findData(int(d))
        if i >= 0:
            self.decim_combo.setCurrentIndex(i)

    def _on_export(self):
        name = self._export_name()
        self._run(core.cmd_export(name, self.range_spin.value(), self.voxel_spin.value() / 1000.0,
                                  decimation=self._decimation()))

    def _export_name(self) -> str:
        name = self.name_edit.text().strip() or "scan"
        name = name[:-4] if name.lower().endswith(".ply") else name
        return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)

    def _update_export_target(self):
        self.export_target.setText(f"→ {core.export_cloud_path(self._export_name())}")

    def _apply_preset(self, name: str):
        p = core.EXPORT_PRESETS.get(name)
        if p:
            self.range_spin.setValue(p["max_range"])
            self.voxel_spin.setValue(p["voxel"] * 1000.0)
            self._set_decimation(p.get("decimation", 2))
            self.strategy_combo.setCurrentText(p["strategy"])

    def _selected_file(self) -> Path | None:
        rows = self.files.selectionModel().selectedRows() if self.files.selectionModel() else []
        if rows:
            item = self.files.item(rows[0].row(), 0)
            if item:
                return core.OUTPUT_DIR / item.text()
        return None

    def _clean_source(self) -> Path | None:
        sel = self._selected_file()
        if sel and sel.suffix.lower() == ".ply":
            return sel
        cand = core.export_cloud_path(self._export_name())
        return cand if cand.exists() else None

    def _update_clean_input(self):
        src = self._clean_source()
        self.clean_input.setText(f"input: {src.name}" if src else
                                 "input: select a .ply above or export one first")

    def _on_clean(self, stl: bool):
        src = self._clean_source()
        if not src:
            QMessageBox.warning(self, "No cloud", "Export a cloud first, or select a .ply in the list.")
            return
        stl_out = src.with_name(src.stem.replace("_cloud", "") + ".stl") if stl else None
        self._run(core.cmd_isolate(src, self.strategy_combo.currentText(),
                                   self.voxel_spin.value() / 1000.0, stl_out))

    def _on_export_clean_stl(self):
        """Export, then (when that finishes) isolate + STL. Chained via the finished signal."""
        name = self._export_name()
        strategy = self.strategy_combo.currentText()
        voxel = self.voxel_spin.value() / 1000.0

        def after_export(code, cmd):
            self.runner.finished.disconnect(after_export)
            if code == 0:
                src = core.export_cloud_path(name)
                self._run(core.cmd_isolate(src, strategy, voxel, core.OUTPUT_DIR / f"{name}.stl"))

        if self._run(core.cmd_export(name, self.range_spin.value(), voxel,
                                     decimation=self._decimation())):
            self.runner.finished.connect(after_export)

    def _on_view(self):
        f = self._selected_file()
        if not f:
            QMessageBox.information(self, "Nothing selected", "Select a file in the list first.")
            return
        self._view_file(f)

    def _view_file(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, "Not found", f"{path} does not exist yet.")
            return
        cmd = core.cmd_view(path)
        self._log(f"$ {cmd.shell_preview()}   (viewer window, detached)")
        ok, _pid = QProcess.startDetached(cmd.argv[0], cmd.argv[1:], str(cmd.cwd))
        if not ok:
            QMessageBox.warning(self, "Viewer", "Could not start the viewer (is .venv set up?)")

    def _on_multi_slam(self):
        r = QMessageBox.question(
            self, "3-camera SLAM (experimental)",
            "Brings up all 3 cameras and starts fused SLAM.\n\n"
            "• ~40 s before the window appears (cameras open one at a time).\n"
            "• Needs GOOD calibration first, or the map stays empty.\n"
            "• Press Stop to abort (it frees the cameras).\n\nLaunch now?")
        if r == QMessageBox.StandardButton.Yes:
            self._run(core.cmd_multi_slam())

    def _on_station_build(self):
        if core.count_sweeps() == 0:
            QMessageBox.warning(self, "No sweeps", "Capture at least one sweep first.")
            return
        self._run(core.cmd_station("build", "--voxel", f"{self.voxel_spin.value() / 1000.0:.4f}"))

    def _on_station_clear(self):
        n = core.count_sweeps()
        if n and QMessageBox.question(self, "Clear sweeps", f"Delete all {n} sweep(s)?") \
                == QMessageBox.StandardButton.Yes:
            self._run(core.cmd_station("clear"))

    def _on_build(self):
        if QMessageBox.question(self, "Build image",
                                f"Build {core.DOCKER_IMAGE} from the Dockerfile?\n"
                                "This downloads ROS packages and takes several minutes.") \
                == QMessageBox.StandardButton.Yes:
            self._run(core.cmd_build_image())

    def _on_clear_db(self):
        if self.runner.busy():
            QMessageBox.warning(self, "Busy", "Stop the running command first.")
            return
        if QMessageBox.question(self, "Clear map", "Delete rtabmap.db (the current map)?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            if core.DB_PATH.exists():
                core.DB_PATH.unlink()
            self._log("[db] rtabmap.db deleted")
        except OSError as e:
            QMessageBox.warning(self, "Clear map", f"Could not delete the database:\n{e}")

    def _on_fake_toggled(self, on: bool):
        self._log(f"[setup] simulated camera {'ON' if on else 'OFF'}")
        self._on_cameras(self.cameras)

    # ---------------------------------------------------------------- doctor
    def _run_doctor(self, full: bool):
        if self.doctor_thread and self.doctor_thread.isRunning():
            return
        self.doctor_btn.setEnabled(False)
        self.doctor_thread = DoctorThread(full)
        self.doctor_thread.done.connect(self._on_doctor)
        self.doctor_thread.start()

    def _on_doctor(self, checks: list):
        self.doctor_checks = checks
        self.doctor_btn.setEnabled(True)
        t = self.doctor_table
        t.setRowCount(len(checks))
        for i, c in enumerate(checks):
            mark = "✔" if c.ok else ("✘" if c.required else "△")
            colour = GREEN if c.ok else (RED if c.required else ORANGE)
            t.setItem(i, 0, QTableWidgetItem(f"{mark}  {c.name}"))
            t.setItem(i, 1, QTableWidgetItem(c.detail))
            t.setItem(i, 2, QTableWidgetItem("" if c.ok else c.hint))
            for col in range(3):
                item = t.item(i, col)
                item.setToolTip(c.hint)
                if not c.ok:
                    item.setForeground(QColor(colour))
        t.resizeRowsToContents()
        problems = core.doctor_blocking_problems(checks)
        if problems:
            names = ", ".join(p.name for p in problems)
            self._log(f"[setup] needs attention: {names}")
            self.statusBar().showMessage(f"Setup needs attention: {names} (see Setup tab)", 20000)
        else:
            self._log("[setup] all required checks passed")

    # ----------------------------------------------------------------- polls
    def _on_cameras(self, cams: list):
        self.cameras = cams
        if self.fake_cb.isChecked():
            self.cam_label.setText("◐ Simulated camera (Setup tab)")
            self.cam_label.setStyleSheet(f"color: {BLUE};")
        elif cams:
            self.cam_label.setText("● " + core.camera_summary(cams))
            self.cam_label.setStyleSheet(f"color: {GREEN};")
        else:
            self.cam_label.setText("○ No Xtion detected — plug it in")
            self.cam_label.setStyleSheet(f"color: {RED};")

    def _on_telemetry(self, t: dict):
        self.telemetry = t
        scanning = self.runner.busy() and self.runner.cmd is not None and \
            self.runner.cmd.argv[-1:] and self.runner.cmd.argv[1] in ("map", "gui", "multi_slam")
        if not t["db_exists"]:
            self.map_label.setText("Map: none yet")
        else:
            self.map_label.setText(f"Map: {t['nodes']} nodes · {t['loop_closures']} loops · "
                                   f"{t['map_size_mb']} MB")
        self.stat_labels["nodes"].setText(str(t["nodes"]))
        self.stat_labels["links"].setText(str(t["links"]))
        lc = str(t["loop_closures"]) + (f"  (last {t['last_loop_closure']})" if t["last_loop_closure"] else "")
        self.stat_labels["loop_closures"].setText(lc)
        self.stat_labels["map_size_mb"].setText(f"{t['map_size_mb']} MB" if t["db_exists"] else "—")
        if t["mtime"]:
            age = time.time() - t["mtime"]
            self.stat_labels["updated"].setText("just now" if age < 5 else f"{age:.0f} s ago"
                                                if age < 3600 else datetime.fromtimestamp(t["mtime"]).strftime("%H:%M"))
        else:
            self.stat_labels["updated"].setText("—")
        hint = core.scan_hint(t, scanning)
        if t.get("error"):
            hint += f"  (db: {t['error']})"
        self.hint_label.setText(hint)

    def _tick_preview(self):
        running_cam = self.runner.busy() and self.runner.cmd is not None and \
            self.runner.cmd.needs_camera > 0
        self.preview.refresh(running_cam)

    def _refresh_files(self):
        if not hasattr(self, "files"):
            return
        selected = self._selected_file()
        files = list(reversed(core.list_outputs()))
        self.files.setRowCount(len(files))
        for i, p in enumerate(files):
            st = p.stat()
            self.files.setItem(i, 0, QTableWidgetItem(p.name))
            self.files.setItem(i, 1, QTableWidgetItem(core.human_size(st.st_size)))
            self.files.setItem(i, 2, QTableWidgetItem(
                datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")))
            if selected and p == selected:
                self.files.selectRow(i)
        self._update_clean_input()

    def _refresh_sweeps(self):
        if hasattr(self, "sweeps_label"):
            self.sweeps_label.setText(f"Sweeps captured: {core.count_sweeps()}")

    # ------------------------------------------------------------------ misc
    def _log(self, text: str):
        self.log.appendPlainText(text)
        if text and not text.startswith(("$", "PROGRESS ")):
            self.statusBar().showMessage(text[:160], 6000)

    def closeEvent(self, e):
        self._save_settings()
        if self.head_proc and self.head_proc.state() != QProcess.ProcessState.NotRunning:
            self.head_proc.terminate()
            if not self.head_proc.waitForFinished(8000):
                self.head_proc.kill()
        self.poller.stop()
        self.activity.stop()
        if self.runner.busy():
            r = QMessageBox.question(
                self, "Still running",
                f"“{self.runner.cmd.title}” is still running. Stop it and quit?")
            if r != QMessageBox.StandardButton.Yes:
                for t in (self.poller, self.activity):
                    t.wait(2000)
                    t._run = True       # stop() cleared it; restart the loops
                    t.start()
                e.ignore()
                return
            self.runner.stop()
            self.runner.proc.waitForFinished(30000)
        self.poller.wait(2000)
        self.activity.wait(2000)
        super().closeEvent(e)


# ---------------------------------------------------------------------- main
class ScriptDriver(QObject):
    """Drives the window from a step list (for end-to-end tests on a real display):
    "preview", "scan", "rtabmap", "stop", "wait N", "shot PATH", "tab N",
    "status", "quit". Steps call the same handlers the buttons do."""

    def __init__(self, win: ScannerWindow, steps: list[str]):
        super().__init__(win)
        self.win, self.steps, self.rc = win, steps, 0
        QTimer.singleShot(500, self.next)

    def _say(self, text: str):
        print(f"[script] {text}", flush=True)

    def next(self):
        if not self.steps:
            return
        step = self.steps.pop(0).strip()
        verb, _, arg = step.partition(" ")
        delay = 300
        w = self.win
        try:
            if verb == "wait":
                delay = int(float(arg) * 1000)
            elif verb == "preview":
                self._say("preview -> " + str(w._run(core.cmd_preview())))
            elif verb == "mode":
                w._set_mode(arg)
                self._say(f"mode -> {w.scan_mode}")
            elif verb == "scan":
                self._say("scan -> " + str(w._run(core.cmd_map(w.fresh_cb.isChecked(), w.scan_mode))))
            elif verb == "rtabmap":
                self._say("rtabmap window -> " + str(w._run(core.cmd_gui(w.fresh_cb.isChecked(), w.scan_mode))))
            elif verb == "clean":
                self._say("clean -> " + str(w._run(core.cmd_isolate(Path(arg), "raw_clean", 0.005))))
            elif verb == "finish":
                w._on_finish()
                self._say("finish requested")
            elif verb == "waitidle":
                if w.runner.busy() or w.runner.cmd is not None:
                    self.steps.insert(0, step)   # poll again
                    delay = 1000
                else:
                    self._say("idle")
            elif verb == "stop":
                w._on_stop()
                self._say("stop requested")
            elif verb == "tab":
                w.tabs.setCurrentIndex(int(arg))
            elif verb == "shot":
                QApplication.processEvents()
                w.grab().save(arg)
                self._say(f"screenshot {arg}")
            elif verb == "status":
                live = core.read_live_status()
                cams = [c["name"] for c in core.running_containers()]
                t = core.read_telemetry()
                self._say(f"running={w.runner.busy()} title={w.run_label.text()!r} "
                          f"containers={cams} live_fresh={None if live is None else live['fresh']} "
                          f"nodes={t['nodes']} loops={t['loop_closures']}")
            elif verb == "quit":
                self._say("quit")
                w.close()
                QApplication.instance().quit()
                return
            else:
                self._say(f"unknown step {step!r}")
                self.rc = 2
        except Exception as e:  # keep going; report at the end
            self._say(f"step {step!r} failed: {e}")
            self.rc = 1
        QTimer.singleShot(delay, self.next)


def smoke_test(win: ScannerWindow, shot_dir: Path | None) -> int:
    """Offscreen self-test: every tab builds, every command builder works,
    optional screenshots of each tab. Returns the exit code."""
    problems = []
    for name in ("cmd_preview", "cmd_map", "cmd_gui", "cmd_export", "cmd_snap", "cmd_multi"):
        try:
            c = getattr(core, name)()
            assert c.argv and c.title
        except Exception as e:
            problems.append(f"{name}: {e}")
    if shot_dir:
        shot_dir.mkdir(parents=True, exist_ok=True)
        for i in range(win.tabs.count()):
            win.tabs.setCurrentIndex(i)
            QApplication.processEvents()
            win.grab().save(str(shot_dir / f"tab{i}_{win.tabs.tabText(i).replace('&', '').replace(' ', '_')}.png"))
        win.tabs.setCurrentIndex(0)
    if win.doctor_thread:
        win.doctor_thread.wait(60000)
        QApplication.processEvents()
    print(f"smoke: {win.tabs.count()} tabs, {len(win.doctor_checks)} doctor checks, "
          f"{len(problems)} problems")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="3D scanner control panel")
    ap.add_argument("--smoke", action="store_true", help="build the UI offscreen, self-test, exit")
    ap.add_argument("--screenshot", metavar="DIR", help="with --smoke: save a PNG of each tab")
    ap.add_argument("--script", metavar="STEPS",
                    help="comma-separated steps to run (test harness): preview, scan, rtabmap, "
                         "stop, 'wait N', 'shot PATH', 'tab N', status, quit")
    ap.add_argument("--fake-camera", action="store_true",
                    help="start with the simulated camera switched on")
    args = ap.parse_args(argv)
    if args.smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication(sys.argv[:1])
    app.setApplicationName("3D Scanner")
    app.setStyleSheet(APP_QSS)
    win = ScannerWindow(smoke=args.smoke)
    if args.fake_camera:
        win.fake_cb.setChecked(True)
    if args.smoke:
        win.show()
        QApplication.processEvents()
        rc = smoke_test(win, Path(args.screenshot) if args.screenshot else None)
        win.poller.stop()
        win.activity.stop()
        win.poller.wait(2000)
        win.activity.wait(2000)
        return rc
    win.show()
    driver = ScriptDriver(win, args.script.split(",")) if args.script else None
    rc = app.exec()
    return driver.rc if driver else rc


if __name__ == "__main__":
    sys.exit(main())
