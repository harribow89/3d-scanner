#!/usr/bin/env python3
"""scanner_core — the GUI-free brain of the 3D scanner control panel.

Everything the PySide6 GUI (scanner_gui.py) needs that is *not* a widget lives
here so it can be unit-tested without Qt (see tests/test_core.py):

  * project paths and constants
  * ASUS Xtion / PrimeSense detection straight from sysfs (no lsusb needed)
  * Docker helpers (is the daemon up, is the image built, stop our containers)
  * `run_doctor()` — the pass/fail checklist shown on the Setup tab
  * RTAB-Map telemetry (read-only, lock-free SQLite poll)
  * the live-preview status written by live_preview_node.py inside the container
  * command builders that return argv LISTS, never shell strings

The last point is the one that matters most. This project lives in a folder with
a space in its name ("3d scanning"); the previous GUI built commands like
`cd /home/jim/Desktop/3d scanning/scanner && ...`, so every button silently
failed at `cd`. A list such as ["./run_scanner_docker.sh", "map"] handed to
QProcess/subprocess has no such problem.
"""
from __future__ import annotations

import grp
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- paths
HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
SWEEP_DIR = OUTPUT_DIR / "sweeps"
DB_PATH = HERE / "rtabmap.db"
LIVE_DIR = HERE / ".live"                  # live_preview_node.py writes here
LOG_DIR = HERE / "ros_logs"                # ROS launch logs (host-persisted)
CONTAINER_HOME = HERE / ".container_home"  # $HOME inside the container
CAMERAS_JSON = HERE / "cameras.json"
EXTRINSICS_JSON = OUTPUT_DIR / "camera_extrinsics.json"
LAUNCHER = HERE / "run_scanner_docker.sh"
VENV_PY = HERE / ".venv" / "bin" / "python"
ICON_PNG = HERE / "scanner_icon.png"

DOCKER_IMAGE = "scanner-ros:jazzy"
CONTAINER_LABEL = "scanner-ros"            # every container we start carries this label

# PrimeSense vendor id; the Xtion family and Carmine sensors all use it.
XTION_VENDOR = "1d27"
XTION_PRODUCTS = {
    "0600": "Xtion PRO",
    "0601": "Xtion PRO LIVE",
    "0609": "Carmine 1.09",
    "0400": "PS1080 sensor",
    "0401": "PS1080 sensor",
    "0500": "PS1080 sensor",
    "0300": "Carmine 1.08",
    "0200": "PSDK 5.0",
}

# Keep in sync with point_cloud_tools.ISOLATION_PROFILES (tests check this) so the
# GUI can list strategies without importing Open3D (slow, ~1 s).
ISOLATION_STRATEGIES = [
    "raw_clean",
    "largest_cluster",
    "tabletop_object",
    "center_focus",
    "aggressive_hybrid",
    "all_variants",
]

# Presets applied by the Export tab. voxel and max_range in metres.
# decimation: depth-image step used by rtabmap-export (its default 4 made every
# cloud blocky; 1 = full 640x480 Xtion resolution).
EXPORT_PRESETS = {
    "Room / scene (default)": {"voxel": 0.005, "max_range": 4.0, "decimation": 2,
                               "strategy": "raw_clean"},
    "Single object on a table": {"voxel": 0.002, "max_range": 1.6, "decimation": 1,
                                 "strategy": "tabletop_object"},
    "Small object, fine detail": {"voxel": 0.0015, "max_range": 1.2, "decimation": 1,
                                  "strategy": "largest_cluster"},
}

# The two things people actually scan. Each mode sets BOTH the RTAB-Map tuning
# used while scanning (passed to the launcher as RTAB_TUNE / ODOM_TUNE env) and
# the export/clean preset used when you press Finish.
#
# Remember the split: rtabmap_args (RTAB_TUNE) go to the SLAM node, odom_args
# (ODOM_TUNE) to rgbd_odometry. Odom/* in RTAB_TUNE crashes the SLAM node.
# Odometry keeps the full depth range in both modes: when you walk around an
# object, the room behind it is what keeps tracking alive.
SCAN_MODES = {
    "room": {
        "label": "Room / space",
        "rtab_tune": ("--Vis/MaxDepth 3.5 --Vis/MinInliers 15 --RGBD/LinearUpdate 0.05 "
                      "--RGBD/AngularUpdate 0.05 --Rtabmap/DetectionRate 2 "
                      "--RGBD/NeighborLinkRefining true --Mem/SaveDepth16Format true"),
        "odom_tune": "--Vis/MaxDepth 3.5 --Vis/MinInliers 15 --Odom/ResetCountdown 5",
        "export": "Room / scene (default)",
        "name": "room",
        "tips": ("Stand in the middle and turn slowly, then walk the edges. Keep the "
                 "camera 0.8–3.5 m from walls, sweep floor and ceiling edges, and finish "
                 "back where you started so RTAB-Map closes the loop."),
    },
    "object": {
        "label": "Object",
        "rtab_tune": ("--Vis/MinInliers 12 --RGBD/LinearUpdate 0.02 --RGBD/AngularUpdate 0.03 "
                      "--Rtabmap/DetectionRate 3 --RGBD/NeighborLinkRefining true "
                      "--Mem/SaveDepth16Format true"),
        "odom_tune": "--Vis/MaxDepth 3.5 --Vis/MinInliers 12 --Odom/ResetCountdown 5",
        "export": "Single object on a table",
        "name": "object",
        "tips": ("Put the object on a table with some clutter or a newspaper around it "
                 "(the Xtion tracks texture, not the object). Keep it 0.6–1.2 m away — "
                 "the Xtion sees nothing closer than ~0.5 m — and walk a slow full circle, "
                 "then a second circle higher up looking down."),
    },
}
DEFAULT_SCAN_MODE = "room"

LIVE_FRESH_SECONDS = 3.0   # preview status older than this is shown as stale

FAKE_CAMERA_ENV = "SCANNER_FAKE_CAMERA"   # =1 → simulated RGB-D camera (tests/fake_camera.py)


# --------------------------------------------------------------------- python/docker
def python_exe() -> str:
    """Interpreter for host-side tools (Open3D, viewer). Prefer the project venv."""
    if VENV_PY.exists():
        return str(VENV_PY)
    return sys.executable


def _in_docker_group() -> bool:
    try:
        gid = grp.getgrnam("docker").gr_gid
    except KeyError:
        return False
    return gid in os.getgroups()


def docker_prefix() -> list[str]:
    """How to invoke docker: directly when the user is in the docker group,
    otherwise through non-interactive sudo (passwordless on this box)."""
    if os.geteuid() == 0 or _in_docker_group():
        return ["docker"]
    return ["sudo", "-n", "docker"]


def _run(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def docker_daemon_ok() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker binary not found (install docker.io)"
    try:
        r = _run(docker_prefix() + ["version", "--format", "{{.Server.Version}}"], timeout=15)
    except subprocess.TimeoutExpired:
        return False, "docker did not answer within 15 s"
    except OSError as e:
        return False, f"cannot run docker: {e}"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        return False, (msg[-1] if msg else "docker returned an error")
    return True, f"server {r.stdout.strip()}"


def image_present(image: str = DOCKER_IMAGE) -> bool:
    try:
        r = _run(docker_prefix() + ["image", "inspect", "--format", "{{.Id}}", image], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def running_containers() -> list[dict]:
    """Our containers (by label; also anything from our image as a fallback)."""
    out: dict[str, dict] = {}
    for flt in (f"label={CONTAINER_LABEL}=1", f"ancestor={DOCKER_IMAGE}"):
        try:
            r = _run(docker_prefix() + ["ps", "--filter", flt, "--format",
                                        "{{.ID}}\t{{.Names}}\t{{.Status}}"], timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0]:
                out[parts[0]] = {"id": parts[0], "name": parts[1], "status": parts[2]}
    return list(out.values())


def stop_containers(timeout_s: int = 8) -> int:
    """Stop every scanner container. Returns how many were stopped."""
    ids = [c["id"] for c in running_containers()]
    if not ids:
        return 0
    try:
        _run(docker_prefix() + ["stop", "-t", str(timeout_s)] + ids, timeout=timeout_s + 30)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return len(ids)


# ---------------------------------------------------------------------- cameras
@dataclass(frozen=True)
class Xtion:
    port: str        # stable USB port path, e.g. "1-3" (what cameras.json pins)
    bus: int
    dev: int
    product: str     # PID hex, e.g. "0601"

    @property
    def name(self) -> str:
        return XTION_PRODUCTS.get(self.product, f"PrimeSense {XTION_VENDOR}:{self.product}")

    @property
    def uri(self) -> str:
        """OpenNI2 device URI (bus/address change on every re-plug)."""
        return f"{XTION_VENDOR}/{self.product}@{self.bus}/{self.dev}"


def port_key(port: str) -> list:
    """Natural order for USB port paths: 1-7 < 1-9 < 1-10 < 1-10.2."""
    return [(0, int(x), "") if x.isdigit() else (1, 0, x)
            for x in port.replace("-", ".").replace(":", ".").split(".")]


def detect_xtions(sysfs: Path = Path("/sys/bus/usb/devices")) -> list[Xtion]:
    """Every PrimeSense device currently enumerated, in natural port order."""
    found: list[Xtion] = []
    try:
        entries = sorted(os.listdir(sysfs), key=port_key)
    except OSError:
        return found
    for name in entries:
        if ":" in name or name.startswith("usb"):
            continue  # interfaces / root hubs
        d = Path(sysfs) / name
        try:
            if (d / "idVendor").read_text().strip().lower() != XTION_VENDOR:
                continue
            product = (d / "idProduct").read_text().strip().lower()
            bus = int((d / "busnum").read_text().strip())
            dev = int((d / "devnum").read_text().strip())
        except (OSError, ValueError):
            continue
        found.append(Xtion(port=name, bus=bus, dev=dev, product=product))
    return found


def xtion_audio_bound(sysfs: Path = Path("/sys/bus/usb/devices")) -> list[str]:
    """Xtion microphone interfaces (class 01) that still have a USB-audio driver.
    snd-usb-audio probing them knocks the sensor off the bus; the launcher
    detaches them before every camera mode."""
    bound = []
    try:
        entries = sorted(os.listdir(sysfs))
    except OSError:
        return bound
    for name in entries:
        if ":" not in name:
            continue
        d = Path(sysfs) / name
        try:
            if (d / "bInterfaceClass").read_text().strip() != "01":
                continue
            parent = Path(sysfs) / name.split(":")[0]
            if (parent / "idVendor").read_text().strip().lower() != XTION_VENDOR:
                continue
            if (d / "authorized").read_text().strip() == "1" and (d / "driver").exists():
                bound.append(name)
        except OSError:
            continue
    return bound


def fake_camera_enabled(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return env.get(FAKE_CAMERA_ENV, "") == "1"


def camera_summary(cams: Optional[list[Xtion]] = None) -> str:
    cams = detect_xtions() if cams is None else cams
    if not cams:
        return "No Xtion detected"
    if len(cams) == 1:
        c = cams[0]
        return f"{c.name} on USB port {c.port}"
    return f"{len(cams)} cameras: " + ", ".join(f"{c.name} @ {c.port}" for c in cams)


def load_roster() -> list[dict]:
    try:
        return json.loads(CAMERAS_JSON.read_text()).get("cameras", [])
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------------- doctor
@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    required: bool = True   # False = informational; a failure doesn't block scanning


def run_doctor(full: bool = True) -> list[Check]:
    """The Setup-tab checklist. `full=False` skips the slow venv import test."""
    checks: list[Check] = []

    ok, detail = docker_daemon_ok()
    checks.append(Check("Docker daemon", ok, detail,
                        "Start Docker: sudo systemctl start docker"))

    if ok:
        have = image_present()
        checks.append(Check(f"Image {DOCKER_IMAGE}", have,
                            "built" if have else "not built",
                            "Build it from the Setup tab or: ./run_scanner_docker.sh build"))
    else:
        checks.append(Check(f"Image {DOCKER_IMAGE}", False, "skipped (no docker)",
                            "Fix Docker first"))

    cams = detect_xtions()
    if fake_camera_enabled():
        checks.append(Check("Depth camera (Xtion)", True,
                            "SIMULATED camera enabled (" + camera_summary(cams) + ")",
                            "", required=True))
    else:
        checks.append(Check("Depth camera (Xtion)", bool(cams), camera_summary(cams),
                            "Plug the Xtion into a USB 2/3 port on this machine. "
                            "Camera modes refuse to start without one (tick 'simulated "
                            "camera' on the Setup tab to try the app without hardware)."))

    if cams:
        bound = xtion_audio_bound()
        checks.append(Check("Xtion microphone driver", not bound,
                            "detached" if not bound else "snd-usb-audio attached to " + ", ".join(bound),
                            "Harmless until it knocks the camera off USB. Every camera mode "
                            "detaches it automatically; to make it permanent: sudo cp "
                            "udev/56-xtion-no-audio.rules /etc/udev/rules.d/ && "
                            "sudo udevadm control --reload-rules", required=False))

    disp = os.environ.get("DISPLAY", "")
    checks.append(Check("X display", bool(disp), disp or "DISPLAY not set",
                        "The RTAB-Map / RViz windows need an X11 display.",
                        required=False))

    xhost = shutil.which("xhost") is not None
    checks.append(Check("xhost", xhost, "present" if xhost else "missing",
                        "sudo apt install x11-xserver-utils (lets the container open windows)",
                        required=False))

    gpu = shutil.which("nvidia-smi") is not None or Path("/dev/dri").exists()
    checks.append(Check("GPU for 3D windows", gpu,
                        "nvidia" if shutil.which("nvidia-smi") else
                        ("/dev/dri" if Path("/dev/dri").exists() else "none (software GL)"),
                        "Optional: without a GPU rtabmap_viz/RViz are laggy.", required=False))

    checks.append(Check("Launcher script", LAUNCHER.exists() and os.access(LAUNCHER, os.X_OK),
                        str(LAUNCHER.name), "chmod +x run_scanner_docker.sh"))

    if full:
        py = python_exe()
        try:
            r = _run([py, "-c", "import open3d, numpy, scipy; print(open3d.__version__)"],
                     timeout=90)
            ok = r.returncode == 0
            detail = (f"open3d {r.stdout.strip()}" if ok
                      else (r.stderr.strip().splitlines() or ["import failed"])[-1])
        except (subprocess.TimeoutExpired, OSError) as e:
            ok, detail = False, str(e)
        checks.append(Check("Python venv (Open3D, SciPy)", ok, detail,
                            "Run ./setup.sh to create .venv with the requirements"))

    roster = load_roster()
    checks.append(Check("cameras.json", bool(roster),
                        f"{len(roster)} camera(s) listed" if roster else "missing or invalid",
                        "Only needed for multi-camera modes", required=False))

    checks.append(Check("Multi-camera calibration", EXTRINSICS_JSON.exists(),
                        "output/camera_extrinsics.json present" if EXTRINSICS_JSON.exists()
                        else "not calibrated yet",
                        "Multi-Camera tab → Check overlap → Auto-calibrate", required=False))
    return checks


def doctor_blocking_problems(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.required and not c.ok]


# ------------------------------------------------------------------- telemetry
def read_telemetry(db_path: Path = DB_PATH) -> dict:
    """Lock-free snapshot of the RTAB-Map database.

    Opened read-only + immutable so this poll NEVER takes a SQLite lock: a normal
    connection would make RTAB-Map's COMMIT fail ("database is locked") and crash
    the mapping process mid-scan. Counts may lag by a moment; that's fine.
    """
    t = {
        "db_exists": False, "nodes": 0, "keyframes": 0, "links": 0,
        "loop_closures": 0, "last_loop_closure": None,
        "map_size_mb": 0.0, "mtime": 0.0, "error": None,
    }
    p = Path(db_path)
    if not p.exists():
        return t
    t["db_exists"] = True
    try:
        st = p.stat()
        t["map_size_mb"] = round(st.st_size / 1048576, 2)
        t["mtime"] = st.st_mtime
        conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
        try:
            cur = conn.cursor()
            t["nodes"] = cur.execute("SELECT COUNT(*) FROM Node").fetchone()[0]
            # weight >= 0 are keyframes, -1 = intermediate
            t["keyframes"] = cur.execute("SELECT COUNT(*) FROM Node WHERE weight >= 0").fetchone()[0]
            t["links"] = cur.execute("SELECT COUNT(*) FROM Link WHERE type = 0").fetchone()[0]
            t["loop_closures"] = cur.execute(
                "SELECT COUNT(*) FROM Link WHERE type IN (1, 2, 3)").fetchone()[0]
            lc = cur.execute("SELECT from_id, to_id FROM Link WHERE type IN (1, 2, 3) "
                             "ORDER BY rowid DESC LIMIT 1").fetchone()
            if lc:
                t["last_loop_closure"] = f"{lc[0]} ↔ {lc[1]}"
        finally:
            conn.close()
    except Exception as e:  # DB being created / schema not there yet
        t["error"] = str(e)[:80]
    return t


def scan_hint(t: dict, scanning: bool) -> str:
    """One plain sentence of advice from the real numbers (no fake percentages)."""
    if not t.get("db_exists"):
        return "No map yet. Start a scan to create one."
    n, lc = t["nodes"], t["loop_closures"]
    if n == 0:
        return ("Waiting for the first keyframe — point the camera at a textured scene "
                "0.5–3 m away." if scanning else "The map is empty. Start a scan.")
    if n < 10:
        return "Map started. Move slowly and keep plenty of overlap between views."
    if lc == 0:
        return ("Good progress, no loop closures yet — return to a spot you already "
                "scanned so RTAB-Map can correct drift.")
    return (f"{n} keyframes and {lc} loop closure(s): drift is being corrected. "
            "Export when coverage looks complete.")


# ---------------------------------------------------------------- live preview
def read_live_status(live_dir: Path = LIVE_DIR) -> Optional[dict]:
    """status.json written by live_preview_node.py, plus its age in seconds."""
    p = Path(live_dir) / "status.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    now = time.time()
    data["age_s"] = max(0.0, now - float(data.get("ts", 0)))
    data["fresh"] = data["age_s"] <= LIVE_FRESH_SECONDS
    return data


def live_image_paths(live_dir: Path = LIVE_DIR, ns: str = "camera") -> tuple[Path, Path]:
    return Path(live_dir) / f"{ns}_rgb.jpg", Path(live_dir) / f"{ns}_depth.jpg"


# -------------------------------------------------------------------- commands
@dataclass
class Command:
    argv: list[str]
    title: str
    long_running: bool = False     # keeps running until Stop (camera streams, SLAM)
    needs_camera: int = 0          # minimum Xtions required (0 = none)
    env: dict = field(default_factory=dict)
    cwd: Path = HERE

    def shell_preview(self) -> str:
        import shlex
        return " ".join(shlex.quote(a) for a in self.argv)


def _launcher(*args: str) -> list[str]:
    return [str(LAUNCHER), *args]


def rel(p: Path) -> str:
    """Path relative to the project (what the launcher maps to /scanner/...)."""
    p = Path(p)
    try:
        return str(p.resolve().relative_to(HERE))
    except ValueError:
        return str(p)


def cmd_doctor() -> Command:
    return Command(_launcher("doctor"), "Checking the setup")


def cmd_preview() -> Command:
    return Command(_launcher("preview"), "Live camera preview", long_running=True, needs_camera=1)


def cmd_camera_test(seconds: int = 20) -> Command:
    return Command(_launcher("camera", str(int(seconds))), f"Camera test ({seconds} s)",
                   needs_camera=1)


def cmd_snap(out: Path = OUTPUT_DIR / "single_frame.ply") -> Command:
    return Command(_launcher("snap", rel(out)), "Capturing one frame", needs_camera=1)


def scan_env(fresh: bool, mode: str = DEFAULT_SCAN_MODE) -> dict:
    m = SCAN_MODES.get(mode, SCAN_MODES[DEFAULT_SCAN_MODE])
    return {"MAP_FRESH": "1" if fresh else "0",
            "RTAB_TUNE": m["rtab_tune"], "ODOM_TUNE": m["odom_tune"]}


def cmd_map(fresh: bool = True, mode: str = DEFAULT_SCAN_MODE) -> Command:
    label = SCAN_MODES.get(mode, SCAN_MODES[DEFAULT_SCAN_MODE])["label"]
    return Command(_launcher("map"), f"Scanning — {label} (RTAB-Map SLAM)", long_running=True,
                   needs_camera=1, env=scan_env(fresh, mode))


def cmd_gui(fresh: bool = True, mode: str = DEFAULT_SCAN_MODE) -> Command:
    label = SCAN_MODES.get(mode, SCAN_MODES[DEFAULT_SCAN_MODE])["label"]
    return Command(_launcher("gui"), f"Scanning — {label} with the RTAB-Map 3D window",
                   long_running=True, needs_camera=1, env=scan_env(fresh, mode))


def cmd_viz() -> Command:
    return Command(_launcher("viz"), "RViz live cloud", long_running=True, needs_camera=1)


def export_cloud_path(stem: str) -> Path:
    """rtabmap-export writes <stem>_cloud.ply next to the requested name."""
    return OUTPUT_DIR / f"{stem}_cloud.ply"


def cmd_export(stem: str = "scan", max_range_m: float = 4.0, voxel_m: float = 0.005,
               db: Path = DB_PATH, decimation: int = 2) -> Command:
    out = OUTPUT_DIR / f"{stem}.ply"
    return Command(_launcher("export", rel(db), rel(out), f"{max_range_m:.2f}", f"{voxel_m:.4f}",
                             str(max(1, int(decimation)))),
                   f"Exporting {export_cloud_path(stem).name}")


def cmd_view(ply: Path) -> Command:
    return Command([python_exe(), str(HERE / "view_cloud.py"), str(ply)],
                   f"Viewing {Path(ply).name}")


def cmd_isolate(ply: Path, strategy: str, voxel_m: float,
                stl_out: Optional[Path] = None) -> Command:
    argv = [python_exe(), "-u", str(HERE / "point_cloud_tools.py"), str(ply),
            "--strategy", strategy, "--voxel", f"{voxel_m:.4f}"]
    title = f"Cleaning {Path(ply).name} ({strategy})"
    if stl_out is not None:
        argv += ["--mesh-output", str(stl_out), "--mesh-voxel", f"{voxel_m:.4f}"]
        title = f"Building {Path(stl_out).name} ({strategy})"
    return Command(argv, title)


def cmd_multi() -> Command:
    return Command(_launcher("multi"), "Multi-camera RViz", long_running=True, needs_camera=1)


def cmd_multi_slam() -> Command:
    return Command(_launcher("multi_slam"), "3-camera SLAM (experimental)", long_running=True,
                   needs_camera=2)


def cmd_overlap() -> Command:
    return Command(_launcher("overlap"), "Checking camera overlap", needs_camera=2)


def cmd_calibrate() -> Command:
    return Command(_launcher("calibrate"), "Auto-calibrating cameras", needs_camera=2)


def cmd_station(sub: str, *extra: str) -> Command:
    titles = {"capture": "Capturing a sweep", "build": "Building the room from sweeps",
              "list": "Listing sweeps", "clear": "Clearing sweeps"}
    return Command(_launcher("station", sub, *extra), titles.get(sub, f"station {sub}"),
                   needs_camera=1 if sub == "capture" else 0)


HEAD_PORT = 5599
HEAD_ENV = "SCANNER_HEAD"     # host[:port] of a network sensor head (empty = USB here)


def split_head(head: str) -> tuple[str, int]:
    host, _, port = head.strip().partition(":")
    return host, int(port) if port.isdigit() else HEAD_PORT


def head_local_argv() -> list[str]:
    """The sensor head on THIS PC (cameras plugged in here), via the launcher."""
    return _launcher("head")


def cmd_head_probe(head: str) -> Command:
    host, port = split_head(head)
    return Command([python_exe(), "-u", str(HERE / "sensor_head" / "head_probe.py"), host,
                    "--port", str(port), "--seconds", "10"],
                   f"Checking the sensor head at {host}")


def cmd_calibrate_head(head: str) -> Command:
    return Command([python_exe(), "-u", str(HERE / "calibrate_head.py"), "--head", head.strip()],
                   "Calibrating the cameras (feature matching via the sensor head)")


def cmd_build_image() -> Command:
    return Command(_launcher("build"), "Building the Docker image (several minutes)")


def cmd_stop() -> Command:
    return Command(_launcher("stop"), "Stopping scanner containers")


# ------------------------------------------------------------------- progress
# rtabmap-export prints a start and an end line per stage; percentages are
# weighted by how long each stage took on a real 469-node map (voxelling is
# the slow one). Matched with startswith() on the line.
EXPORT_STAGES = [
    (">> exporting", 1, "Starting the export container"),
    ("Opening database", 3, "Opening the map database"),
    ("Optimizing the map", 5, "Optimizing the pose graph"),
    ("Create and assemble the clouds...", 8, "Building the cloud from every keyframe"),
    ("Create and assemble the clouds... done", 35, "Clouds assembled"),
    ("Voxel grid filtering", 36, "Voxel filtering (slowest step)"),
    ("Voxel grid filtering of the assembled cloud.... done", 75, "Voxel filtering done"),
    ("Computing normals", 76, "Computing normals"),
    ("Computing normals of the assembled cloud... done", 85, "Normals done"),
    ("Adjust normals", 86, "Orienting normals"),
    ("Saving ", 94, "Writing the PLY file"),
    ("Exported:", 100, "Export finished"),
]


def parse_progress(line: str, stages=None) -> Optional[tuple[int, str]]:
    """(percent, label) from one output line, or None.

    Understands the explicit `PROGRESS <pct> <label>` lines that
    point_cloud_tools.py prints, and rtabmap-export's stage lines."""
    s = line.strip()
    if s.startswith("PROGRESS "):
        parts = s.split(" ", 2)
        try:
            return max(0, min(100, int(float(parts[1])))), (parts[2] if len(parts) > 2 else "")
        except ValueError:
            return None
    best = None
    for prefix, pct, label in (stages if stages is not None else EXPORT_STAGES):
        # the longest matching prefix wins ("…clouds... done" over "…clouds...")
        if s.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), pct, label)
    return (best[1], best[2]) if best else None


def _children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                st = fh.read()
            ppid = int(st[st.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    return kids


def tree_cpu_ticks(roots: list[int]) -> int:
    """utime+stime clock ticks of every process under `roots` (inclusive).
    Polled twice, the difference says whether anything is still working —
    the point is telling 'busy in a long Open3D call' from 'hung/crashed'."""
    kids = _children_map()
    seen, stack, total = set(), [p for p in roots if p], 0
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(f"/proc/{p}/stat") as fh:
                st = fh.read()
            f = st[st.rindex(")") + 2:].split()
            total += int(f[11]) + int(f[12])
        except (OSError, ValueError, IndexError):
            pass
        stack.extend(kids.get(p, []))
    return total


def mem_available_gb(meminfo: str = "/proc/meminfo") -> Optional[float]:
    try:
        with open(meminfo) as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1048576.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def describe_exit(code: int, crashed: bool = False) -> str:
    """Plain words for a job's exit code (137 / SIGKILL is almost always the
    kernel's out-of-memory killer on a big cloud or mesh)."""
    if code == 0:
        return "finished"
    if code in (137, -9, 9) or (crashed and code in (-3, 0)):
        return "killed — most likely ran out of memory (try a coarser Detail / bigger voxel)"
    if code in (139, -11, 11):
        return "crashed (segmentation fault) — a corrupt map? try Setup → Clear the map database"
    if code == 4:
        return "nothing to export — the map is empty"
    if code == 3:
        return "no camera found"
    return f"failed (exit {code})"


def container_pids(name_prefix: str = "scanner-") -> list[int]:
    """Host PIDs of our running containers' init processes (their work runs
    under containerd, not under the launcher, so it is not in our own tree)."""
    pids = []
    for c in running_containers():
        if not c["name"].startswith(name_prefix):
            continue
        try:
            r = _run(docker_prefix() + ["inspect", "-f", "{{.State.Pid}}", c["id"]], timeout=10)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                pids.append(int(r.stdout.strip()))
        except (subprocess.TimeoutExpired, OSError):
            continue
    return pids


# ---------------------------------------------------------------------- files
def list_outputs() -> list[Path]:
    """PLY/STL files in output/, newest last."""
    if not OUTPUT_DIR.exists():
        return []
    files = [p for p in OUTPUT_DIR.iterdir()
             if p.suffix.lower() in (".ply", ".stl") and p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def count_sweeps() -> int:
    return len(list(SWEEP_DIR.glob("sweep_*.ply"))) if SWEEP_DIR.exists() else 0


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ------------------------------------------------------------------------ CLI
def _print_doctor(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks) + 2
    bad = 0
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.required else "warn")
        print(f"[{mark}] {c.name:<{width}} {c.detail}")
        if not c.ok and c.hint:
            print(f"       -> {c.hint}")
        if c.required and not c.ok:
            bad += 1
    print()
    print("All required checks passed." if bad == 0 else f"{bad} required check(s) failed.")
    return 0 if bad == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "doctor"
    if cmd == "doctor":
        return _print_doctor(run_doctor(full="--quick" not in argv))
    if cmd == "cameras":
        cams = detect_xtions()
        print(camera_summary(cams))
        for c in cams:
            print(f"  port {c.port}: {c.name}  uri={c.uri}")
        return 0 if cams else 1
    if cmd == "telemetry":
        print(json.dumps(read_telemetry(), indent=2))
        return 0
    if cmd == "containers":
        cs = running_containers()
        for c in cs:
            print(f"{c['id']}  {c['name']}  {c['status']}")
        print(f"{len(cs)} scanner container(s) running")
        return 0
    if cmd == "stop":
        print(f"stopped {stop_containers()} container(s)")
        return 0
    print(__doc__)
    print("usage: scanner_core.py doctor [--quick] | cameras | telemetry | containers | stop")
    return 2


if __name__ == "__main__":
    sys.exit(main())
