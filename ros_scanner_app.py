#!/usr/bin/env python3
"""
ROS 3D Scanner — Precision Point Cloud App
Powered by RTAB-Map + OpenNI2 (PrimeSense / ASUS Xtion)
Optional AI agent (Claude / GPT-4) watches the scan and guides in real time.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

try:
    from point_cloud_tools import ISOLATION_PROFILES, isolate_point_cloud_variants
    _POINT_CLOUD_TOOLS_ERROR = None
except Exception as _pc_exc:
    ISOLATION_PROFILES = {
        "raw_clean": {"label": "Raw clean"},
        "largest_cluster": {"label": "Largest cluster"},
        "tabletop_object": {"label": "Tabletop object"},
        "center_focus": {"label": "Center focus"},
        "aggressive_hybrid": {"label": "Aggressive hybrid"},
        "all_variants": {"label": "All variants"},
    }
    isolate_point_cloud_variants = None
    _POINT_CLOUD_TOOLS_ERROR = str(_pc_exc)

ROOT_DIR    = "/home/jim/Desktop/scanner"
LAUNCHER    = os.path.join(ROOT_DIR, "run_prebuilt_stack.sh")
DEFAULT_DB  = os.path.join(ROOT_DIR, "rtabmap.db")
LOG_DIR     = os.path.join(ROOT_DIR, "ros_logs")
RTABMAP_LOG = os.path.join(LOG_DIR, "rtabmap.log")
SETTINGS_FILE = os.path.join(ROOT_DIR, "ros_scanner_settings.json")
ANTHROPIC_KEY_FILE  = "/home/jim/Documents/anthropic_api.txt"
OPENAI_KEY_FILES    = [
    "/home/jim/Documents/open_ai_API.txt",
    "/home/jim/Documents/apikey1.txt",
    "/home/jim/Documents/apikey2.txt",
    "/home/jim/Documents/apikey3.txt",
]

# ── Colour palette ────────────────────────────────────────────────────────
BG         = "#111118"
PANEL      = "#1a1a28"
CARD       = "#22223a"
BORDER     = "#33335a"
ACCENT     = "#00d4aa"
AI_ACCENT  = "#a06aff"
TEXT       = "#dde0f0"
DIM        = "#6666a0"
GREEN      = "#00c853"
YELLOW     = "#ffb300"
RED        = "#e53935"
BTN_START  = "#00796b"
BTN_START_H= "#00bfa5"
BTN_STOP   = "#c62828"
BTN_STOP_H = "#ef5350"
BTN_EXPORT = "#1565c0"
BTN_EXPORT_H="#1976d2"
BTN_TOOLS  = "#2c2c44"
BTN_TOOLS_H= "#3c3c60"
AI_BG      = "#0d0d20"
AI_TEXT    = "#c8aaff"

F_TITLE  = ("Segoe UI", 15, "bold")
F_LABEL  = ("Segoe UI", 9)
F_SMALL  = ("Segoe UI", 8)
F_BOLD   = ("Segoe UI", 9, "bold")
F_MONO   = ("Courier New", 8)
F_BIGBTN = ("Segoe UI", 13, "bold")
F_STAT   = ("Segoe UI", 10)

# ── Scan presets ──────────────────────────────────────────────────────────
PRESETS = {
    "Object": {
        "label": "Object  (table-top, < 1.2 m)",
        "depth_min": 150, "depth_max": 1200,
        "export_max_range": 1.5, "export_min_range": 0.10,
    },
    "Room": {
        "label": "Room / Interior",
        "depth_min": 300, "depth_max": 4000,
        "export_max_range": 4.0, "export_min_range": 0.30,
    },
}

QUALITY = {
    "Fast (5 mm)":      {"voxel": 0.005, "noise_r": 0.025, "noise_k": 5},
    "Balanced (3 mm)":  {"voxel": 0.003, "noise_r": 0.015, "noise_k": 5},
    "Precision (1 mm)": {"voxel": 0.001, "noise_r": 0.008, "noise_k": 5},
}

ISOLATION_TOOLTIPS = {
    "raw_clean": "Light cleanup only. Best when the raw export already looks tight.",
    "largest_cluster": "Keeps the strongest cluster. Good for hanging objects or cluttered backgrounds.",
    "tabletop_object": "Removes the dominant support plane and keeps the main object.",
    "center_focus": "Biases toward the object near the middle of the scan volume.",
    "aggressive_hybrid": "Strongest cleanup for noisy table-top scans.",
    "all_variants": "Writes several isolated versions so you can compare them quickly.",
}

MODELS = {
    "Claude Sonnet 4.6 (best)":  ("claude-sonnet-4-6",           "anthropic"),
    "Claude Haiku 4.5 (fast)":   ("claude-haiku-4-5-20251001",   "anthropic"),
    "GPT-4o Mini (OpenAI)":      ("gpt-4o-mini",                 "openai"),
}

# ── AI skill prompt for ROS/RTAB-Map ─────────────────────────────────────
ROS_SCANNER_SKILL = """
You are the real-time AI controller for a 3D object scanner.
Hardware: PrimeSense / ASUS Xtion depth camera.
Software: ROS 2 Jazzy + RTAB-Map SLAM + OpenNI2.

=== HOW THIS SCANNER WORKS ===
The user holds the camera and moves it around an object.
RTAB-Map builds a map of keyframes (nodes) with loop-closure detection.
When done, rtabmap-export assembles a dense point cloud from the database.

Key metrics you will see each tick:
  scanning       — True while the ROS stack is running
  elapsed_s      — seconds since scan started
  wm_nodes       — RTAB-Map working memory (keyframes stored). Needs 30+ for decent coverage.
  odom_quality   — visual odometry quality: 0=lost, 1-19=weak, 20+=good. Must be mostly >0.
  odom_zero_streak — consecutive ticks where odom_quality==0 (tracking fully lost)
  loop_closures  — detected loop closures. ≥2 means good spatial coverage.
  preset         — Object or Room
  quality        — export voxel preset (Fast/Balanced/Precision)
    depth_min_mm / depth_max_mm — current export depth gate in millimeters
    isolation_profile — which object-isolation strategy will run after export
    latest_export_ready — whether a saved cloud is ready for preview/re-isolation
    latest_isolation_points — points in the recommended isolated cloud from the last export
  db_exists      — whether a database file is present for export

=== COMMANDS YOU CAN ISSUE (one per line, starts the line) ===
  START_SCAN       — press the start button (only if not scanning)
  STOP_SCAN        — press the stop button (only if scanning)
  EXPORT_CLOUD     — trigger point-cloud export from existing database
  SET_QUALITY:x    — change quality (x = "Fast (5 mm)", "Balanced (3 mm)", "Precision (1 mm)")
  SET_PRESET:x     — change preset (x = "Object" or "Room")
    SET_ISOLATION:x  — set isolation profile (raw_clean, largest_cluster, tabletop_object, center_focus, aggressive_hybrid, all_variants)
    SET_DEPTH_WINDOW:min:max — set export depth gate in millimeters
    SET_AUTO_EXPORT:on|off — toggle auto-export after stopping the scan
    ISOLATE_LATEST   — rerun isolation on the last saved cloud
    OPEN_LATEST      — preview the latest recommended cloud
  WAIT             — take no action this tick
  SAY:<text>       — show a message to the user in the UI (keep it short and actionable)

=== DECISION RULES ===
1. If not scanning and no db_exists → SAY a brief welcome/start tip.
2. If scanning and odom_zero_streak >= 3 → SAY "Tracking lost — improve lighting or slow down"
3. If scanning and elapsed_s > 20 and wm_nodes == 0 → SAY "No map progress — check camera is connected and lit"
4. If scanning and wm_nodes >= 40 and loop_closures >= 2 and elapsed_s >= 45 → SAY "Good coverage achieved! You can stop now." (then WAIT — don't auto-stop)
5. If scanning and wm_nodes >= 80 → STOP_SCAN (coverage complete)
6. If not scanning and db_exists and latest_export_ready is false:
    - preset=Object → SET_ISOLATION:tabletop_object unless already set
    - preset=Room → SET_ISOLATION:largest_cluster unless already set
    Then EXPORT_CLOUD.
7. If not scanning and latest_export_ready is true and latest_isolation_points < 12000 and preset=Object:
    - SET_ISOLATION:all_variants and ISOLATE_LATEST.
8. If odom_quality is consistently weak (1-10) → SAY "Tracking is weak — light it better and move slower"
9. If scanning and preset=Object and elapsed_s < 20 and depth_max_mm > 1800 → SET_DEPTH_WINDOW:150:1400
10. Otherwise → WAIT

=== RESPONSE FORMAT ===
First, 2-3 sentences of brief analysis (what you observe and why).
Then on separate lines, issue 0-2 commands.
Keep SAY messages under 80 characters and actionable.
Do not spam SAY — only say something new if the situation changed.
""".strip()


# ── ANSI strip ────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _strip_ansi(s):
    return _ANSI_RE.sub("", s)

def _ts():
    return datetime.now().strftime("%H:%M:%S")


# ── API key loader ────────────────────────────────────────────────────────
def _load_key(path):
    try:
        return open(path).read().strip()
    except Exception:
        return ""


# ── Helpers to run launcher commands ─────────────────────────────────────
def _run_launcher(args, log_cb, done_cb=None):
    def _worker():
        try:
            proc = subprocess.Popen(
                [LAUNCHER] + args,
                cwd=ROOT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                log_cb(line.rstrip())
            proc.wait()
            if done_cb:
                done_cb(proc.returncode)
        except Exception as exc:
            log_cb(f"[error] {exc}")
            if done_cb:
                done_cb(1)
    threading.Thread(target=_worker, daemon=True).start()


# ── Live RTAB-Map log monitor ─────────────────────────────────────────────
class LogMonitor:
    _RE_RTAB = re.compile(r"rtabmap \((\d+)\):.*local map=(\d+), WM=(\d+)")
    _RE_ODOM = re.compile(r"Odom: quality=(\d+)")
    _RE_LC   = re.compile(r"Loop closure detected!")
    _ANSI    = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, on_stats):
        self._on_stats = on_stats
        self._active   = False
        self._thread   = None
        self._stats    = self._blank()

    def _blank(self):
        return {"iteration": 0, "wm": 0, "local_map": 0,
                "odom_quality": -1, "loop_closures": 0}

    def get_stats(self):
        return dict(self._stats)

    def start(self):
        self._active = True
        self._stats  = self._blank()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._active = False

    def _run(self):
        for _ in range(30):
            if not self._active:
                return
            if os.path.isfile(RTABMAP_LOG):
                break
            time.sleep(0.5)
        try:
            with open(RTABMAP_LOG, "r", errors="replace") as fh:
                fh.seek(0, 2)
                while self._active:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.15)
                        continue
                    line = self._ANSI.sub("", line)
                    self._parse(line)
        except Exception:
            pass

    def _parse(self, line):
        m = self._RE_RTAB.search(line)
        if m:
            self._stats["iteration"] = int(m.group(1))
            self._stats["local_map"] = int(m.group(2))
            self._stats["wm"]        = int(m.group(3))
            self._on_stats(dict(self._stats))
            return
        m = self._RE_ODOM.search(line)
        if m:
            self._stats["odom_quality"] = int(m.group(1))
            self._on_stats(dict(self._stats))
            return
        if self._RE_LC.search(line):
            self._stats["loop_closures"] += 1
            self._on_stats(dict(self._stats))


# ── ROS scanner bridge (for the AI agent) ────────────────────────────────
class ROSScannerBridge:
    """Adapter so the AI agent can read state and dispatch commands to the App."""

    def __init__(self, app):
        self._app = app
        self._odom_zero_streak = 0
        self._last_odom = -1

    def get_state(self):
        app = self._app
        stats = app._monitor.get_stats()
        odom = stats["odom_quality"]

        # Track consecutive zero-quality readings
        if odom == 0:
            self._odom_zero_streak += 1
        elif odom > 0:
            self._odom_zero_streak = 0
        self._last_odom = odom

        elapsed = int(time.time() - app._scan_start) if app._scanning else 0

        return {
            "scanning":          app._scanning,
            "elapsed_s":         elapsed,
            "wm_nodes":          stats["wm"],
            "odom_quality":      odom,
            "odom_zero_streak":  self._odom_zero_streak,
            "loop_closures":     stats["loop_closures"],
            "preset":            app._preset_var.get(),
            "quality":           app._quality_var.get(),
            "depth_min_mm":      int(app._dmin_var.get() or 0),
            "depth_max_mm":      int(app._dmax_var.get() or 0),
            "isolation_profile": app._isolation_var.get(),
            "latest_export_ready": bool(app._latest_export_path),
            "latest_isolation_points": int(app._latest_isolation_points),
            "db_exists":         os.path.isfile(app._db_var.get().strip() or DEFAULT_DB),
        }

    def dispatch(self, verb, arg=None):
        app = self._app
        verb = verb.upper().strip()
        if verb == "START_SCAN" and not app._scanning:
            app.after(0, app._start_scan)
        elif verb == "STOP_SCAN" and app._scanning:
            app.after(0, app._stop_scan)
        elif verb == "EXPORT_CLOUD":
            app.after(0, app._do_export)
        elif verb == "SET_QUALITY" and arg:
            # Accept "Balanced" or "Balanced (3 mm)" etc.
            match = next((k for k in QUALITY if arg.lower() in k.lower()), None)
            if match:
                app.after(0, lambda m=match: app._quality_var.set(m))
        elif verb == "SET_PRESET" and arg:
            match = next((k for k in PRESETS if arg.lower() in k.lower()), None)
            if match:
                app.after(0, lambda m=match: [app._preset_var.set(m), app._apply_preset()])
        elif verb == "SET_ISOLATION" and arg:
            match = next((k for k in ISOLATION_PROFILES if arg.lower() == k.lower()), None)
            if match:
                app.after(0, lambda m=match: app._set_isolation_profile(m))
        elif verb == "SET_DEPTH_WINDOW" and arg:
            nums = [int(n) for n in re.findall(r"\d+", arg)]
            if len(nums) >= 2:
                app.after(0, lambda lo=nums[0], hi=nums[1]: app._set_depth_window(lo, hi))
        elif verb == "SET_AUTO_EXPORT" and arg:
            enable = arg.strip().lower() in ("on", "true", "1", "yes")
            app.after(0, lambda v=enable: app._set_auto_export(v))
        elif verb == "ISOLATE_LATEST":
            app.after(0, app._reprocess_latest_export)
        elif verb == "OPEN_LATEST":
            app.after(0, app._open_latest_cloud)
        elif verb == "SAY" and arg:
            app._log_q.put(("log", f"[AI] {arg}", "ai"))
            app._log_q.put(("ai_say", arg))


# ── AI agent runner (thin wrapper that imports AIAgentController) ─────────
class ROSAgentRunner:
    """Wraps AIAgentController for the ROS scanner context."""

    def __init__(self, bridge, on_thinking, on_command, on_error,
                 model, api_key, provider, tick_interval=8.0):
        from ai_scanner_agent import VALID_COMMANDS, parse_commands
        self._parse_commands = parse_commands
        self._VALID_COMMANDS = VALID_COMMANDS
        self._bridge = bridge
        self._pending_instruction = None
        self._instr_lock = threading.Lock()
        self._on_thinking = on_thinking
        self._on_command  = on_command
        self._on_error    = on_error
        self._model       = model
        self._api_key     = api_key
        self._provider    = provider
        self._tick_interval = tick_interval
        self._running     = False
        self._thread      = None
        self._last_say    = ""

        try:
            import anthropic as _ant
            self._anthropic = _ant
        except ImportError:
            self._anthropic = None
        try:
            from openai import OpenAI as _oai
            self._OpenAI = _oai
        except ImportError:
            self._OpenAI = None

        if provider == "anthropic":
            if not self._anthropic:
                raise ImportError("anthropic package not installed")
            self._client = self._anthropic.Anthropic(api_key=api_key)
        else:
            if not self._OpenAI:
                raise ImportError("openai package not installed")
            self._client = self._OpenAI(api_key=api_key)

    def send_instruction(self, text):
        with self._instr_lock:
            self._pending_instruction = text

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="ROSAgent")
        self._thread.start()

    def stop(self):
        self._running = False

    def is_alive(self):
        return self._running and self._thread is not None and self._thread.is_alive()

    def _loop(self):
        self._on_thinking("AI agent started — monitoring scan…\n")
        history = []
        while self._running:
            try:
                state = self._bridge.get_state()
                user_msg = "=== SCANNER STATE ===\n" + "\n".join(
                    f"  {k}: {v}" for k, v in state.items()
                )
                with self._instr_lock:
                    instr = self._pending_instruction
                    self._pending_instruction = None
                if instr:
                    user_msg += f"\n\n=== USER INSTRUCTION ===\n{instr}"
                history.append({"role": "user", "content": user_msg})
                if len(history) > 10:
                    history = history[-10:]

                full = ""
                self._on_thinking("\n── AI ──\n")

                if self._provider == "anthropic":
                    with self._client.messages.stream(
                        model=self._model,
                        max_tokens=400,
                        system=ROS_SCANNER_SKILL,
                        messages=history,
                    ) as stream:
                        for chunk in stream.text_stream:
                            full += chunk
                            self._on_thinking(chunk)
                else:
                    msgs = [{"role": "system", "content": ROS_SCANNER_SKILL}] + history
                    stream = self._client.chat.completions.create(
                        model=self._model, messages=msgs, max_tokens=400, stream=True
                    )
                    for ev in stream:
                        try:
                            delta = ev.choices[0].delta.content
                        except Exception:
                            delta = None
                        if delta:
                            full += delta
                            self._on_thinking(delta)

                self._on_thinking("\n")
                history.append({"role": "assistant", "content": full})

                for verb, arg in self._parse_commands(full):
                    if verb == "WAIT":
                        continue
                    self._on_command(verb, arg)
                    try:
                        self._bridge.dispatch(verb, arg)
                    except Exception as e:
                        self._on_error(f"Dispatch error ({verb}): {e}")

            except Exception as exc:
                self._on_error(f"Agent error: {exc}")

            time.sleep(self._tick_interval)


# ═══════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("3D Scanner  —  ROS / RTAB-Map")
        self.configure(bg=BG)
        self.geometry("1020x740")
        self.minsize(860, 600)
        self.resizable(True, True)

        self._log_q      = queue.Queue()
        self._scanning   = False
        self._scan_start = 0.0
        self._auto_export= tk.BooleanVar(value=True)
        self._reset_db   = tk.BooleanVar(value=True)
        self._ai_enabled = tk.BooleanVar(value=False)
        self._agent      = None

        self._wm_var      = tk.StringVar(value="—")
        self._lc_var      = tk.StringVar(value="0")
        self._odom_var    = tk.StringVar(value="—")
        self._elapsed_var = tk.StringVar(value="00:00")
        self._status_var  = tk.StringVar(value="Ready")
        self._ai_status_var = tk.StringVar(value="Off")
        self._guidance_var = tk.StringVar(value="Start a scan and orbit the object at a steady speed.")
        self._latest_summary_var = tk.StringVar(value="No export yet")

        self._latest_raw_export_path = ""
        self._latest_export_path = ""
        self._latest_isolation_summary = None
        self._latest_isolation_points = 0

        self._monitor  = LogMonitor(self._on_stats)
        self._bridge   = ROSScannerBridge(self)
        self._settings = self._load_settings()

        self._build_ui()
        self._after_window_visible()
        self._poll_logs()
        self._tick()

    # ── Persistence ───────────────────────────────────────────────────────
    def _load_settings(self):
        d = {
            "preset": "Object",
            "quality": "Precision (1 mm)",
            "depth_min": 150,
            "depth_max": 1200,
            "output_dir": os.path.join(ROOT_DIR, "output"),
            "db_path": DEFAULT_DB,
            "ai_model": "Claude Sonnet 4.6 (best)",
            "isolation_profile": "tabletop_object",
            "auto_open_viewer": False,
        }
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            d.update({k: v for k, v in data.items() if k in d})
        except Exception:
            pass
        return d

    def _save_settings(self):
        data = {
            "preset":   self._preset_var.get(),
            "quality":  self._quality_var.get(),
            "depth_min": int(self._dmin_var.get() or 150),
            "depth_max": int(self._dmax_var.get() or 1200),
            "output_dir": self._outdir_var.get(),
            "db_path":  self._db_var.get(),
            "ai_model": self._model_var.get(),
            "isolation_profile": self._isolation_var.get(),
            "auto_open_viewer": bool(self._auto_view_var.get()),
        }
        os.makedirs(data["output_dir"], exist_ok=True)
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        hdr = tk.Frame(self, bg=PANEL, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="3D Scanner", font=F_TITLE, bg=PANEL, fg=ACCENT).pack(
            side="left", padx=16, pady=6)
        tk.Label(hdr, text="ROS · RTAB-Map · OpenNI2", font=F_SMALL, bg=PANEL, fg=DIM).pack(
            side="left", pady=6)

        status_fr = tk.Frame(hdr, bg=PANEL)
        status_fr.pack(side="right", padx=16)
        self._dot_camera = self._dot(status_fr, "Camera")
        self._dot_stack  = self._dot(status_fr, "Stack")
        self._dot_odom   = self._dot(status_fr, "Tracking")
        self._dot_ai     = self._dot(status_fr, "AI")

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=8)
        main.columnconfigure(0, minsize=230, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self._build_settings(main)
        self._build_scan_panel(main)
        self._build_log()

    def _dot(self, parent, label):
        fr = tk.Frame(parent, bg=PANEL)
        fr.pack(side="left", padx=6)
        c = tk.Canvas(fr, width=10, height=10, bg=PANEL, highlightthickness=0)
        c.pack(side="left")
        c.create_oval(1, 1, 9, 9, fill=DIM, outline="", tags="dot")
        tk.Label(fr, text=label, font=F_SMALL, bg=PANEL, fg=DIM).pack(side="left", padx=(3, 0))
        return c

    def _set_dot(self, canvas, color):
        canvas.itemconfig("dot", fill=color)

    # ── Settings sidebar ──────────────────────────────────────────────────
    def _build_settings(self, parent):
        col = tk.Frame(parent, bg=PANEL)
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # scrollable inner frame for the sidebar
        canvas = tk.Canvas(col, bg=PANEL, highlightthickness=0)
        vsb    = tk.Scrollbar(col, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=PANEL)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_config(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(win_id, width=e.width)
        inner.bind("<Configure>", _on_config)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

        def section(text, color=ACCENT):
            tk.Label(inner, text=text, font=F_BOLD, bg=PANEL, fg=color).pack(
                anchor="w", padx=12, pady=(12, 2))

        def sep():
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=5)

        # ── Scan preset ──────────────────────────────────────────────────
        section("SCAN PRESET")
        self._preset_var = tk.StringVar(value=self._settings["preset"])
        for name in PRESETS:
            tk.Radiobutton(inner, text=PRESETS[name]["label"],
                variable=self._preset_var, value=name,
                command=self._apply_preset,
                bg=PANEL, fg=TEXT, selectcolor=CARD,
                activebackground=PANEL, activeforeground=ACCENT,
                font=F_LABEL, anchor="w",
            ).pack(anchor="w", padx=16)

        sep()

        # ── Quality ──────────────────────────────────────────────────────
        section("EXPORT QUALITY")
        self._quality_var = tk.StringVar(value=self._settings["quality"])
        for q in QUALITY:
            tk.Radiobutton(inner, text=q,
                variable=self._quality_var, value=q,
                bg=PANEL, fg=TEXT, selectcolor=CARD,
                activebackground=PANEL, activeforeground=ACCENT,
                font=F_LABEL, anchor="w",
            ).pack(anchor="w", padx=16)

        sep()

        # ── Depth range ──────────────────────────────────────────────────
        section("DEPTH RANGE")
        self._dmin_var = tk.StringVar(value=str(self._settings["depth_min"]))
        self._dmax_var = tk.StringVar(value=str(self._settings["depth_max"]))

        def depth_row(label, var):
            fr = tk.Frame(inner, bg=PANEL)
            fr.pack(fill="x", padx=12, pady=2)
            tk.Label(fr, text=label, width=6, anchor="w", font=F_LABEL, bg=PANEL, fg=DIM).pack(side="left")
            tk.Entry(fr, textvariable=var, width=6, bg=CARD, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=F_LABEL).pack(side="left", padx=4)
            tk.Label(fr, text="mm", font=F_SMALL, bg=PANEL, fg=DIM).pack(side="left")

        depth_row("Min:", self._dmin_var)
        depth_row("Max:", self._dmax_var)

        sep()

        # ── Options ──────────────────────────────────────────────────────
        section("OPTIONS")
        for text, var in [
            ("Reset DB each scan",     self._reset_db),
            ("Auto-export when stopped", self._auto_export),
        ]:
            tk.Checkbutton(inner, text=text, variable=var,
                bg=PANEL, fg=TEXT, selectcolor=CARD,
                activebackground=PANEL, activeforeground=ACCENT,
                font=F_LABEL, anchor="w",
            ).pack(anchor="w", padx=16, pady=1)

        sep()

        # ── AI AGENT section ─────────────────────────────────────────────
        section("AI AGENT", color=AI_ACCENT)

        ai_toggle_fr = tk.Frame(inner, bg=PANEL)
        ai_toggle_fr.pack(fill="x", padx=12, pady=2)
        tk.Checkbutton(ai_toggle_fr, text="Enable AI agent",
            variable=self._ai_enabled,
            command=self._on_ai_toggle,
            bg=PANEL, fg=TEXT, selectcolor=CARD,
            activebackground=PANEL, activeforeground=AI_ACCENT,
            font=F_BOLD, anchor="w",
        ).pack(side="left")
        tk.Label(ai_toggle_fr, textvariable=self._ai_status_var,
            font=F_SMALL, bg=PANEL, fg=DIM).pack(side="left", padx=6)

        # Model selector
        tk.Label(inner, text="Model:", font=F_LABEL, bg=PANEL, fg=DIM).pack(
            anchor="w", padx=16, pady=(6, 1))
        self._model_var = tk.StringVar(value=self._settings.get("ai_model", "Claude Sonnet 4.6 (best)"))
        model_cb = ttk.Combobox(inner, textvariable=self._model_var,
            values=list(MODELS.keys()), state="readonly", width=24,
            font=F_LABEL)
        model_cb.pack(anchor="w", padx=16, pady=2)

        # Tick interval
        tk.Label(inner, text="Tick every (s):", font=F_LABEL, bg=PANEL, fg=DIM).pack(
            anchor="w", padx=16, pady=(6, 1))
        self._tick_var = tk.StringVar(value="8")
        tk.Entry(inner, textvariable=self._tick_var, width=5, bg=CARD, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=F_LABEL).pack(anchor="w", padx=16)

        # API key source info
        tk.Label(inner,
            text="Keys: anthropic_api.txt / open_ai_API.txt\nor ANTHROPIC_API_KEY env var",
            font=F_SMALL, bg=PANEL, fg=DIM, justify="left", wraplength=190).pack(
            anchor="w", padx=16, pady=(4, 0))

        sep()

        # ── Tools ────────────────────────────────────────────────────────
        section("TOOLS")
        for label, cmd in [
            ("NiViewer (depth test)", lambda: self._cmd("niviewer")),
            ("RTAB-Map DB viewer",    self._open_db_viewer),
            ("Open output folder",    self._open_outdir),
            ("Stack status",          lambda: self._cmd("ros_status")),
        ]:
            self._small_btn(inner, label, cmd).pack(fill="x", padx=10, pady=2)

    # ── Scan panel (right side) ───────────────────────────────────────────
    def _build_scan_panel(self, parent):
        right = tk.Frame(parent, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        # Main scan card
        card = tk.Frame(right, bg=PANEL)
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)   # stats row

        # ── START / STOP button ──────────────────────────────────────────
        btn_fr = tk.Frame(card, bg=PANEL)
        btn_fr.grid(row=0, column=0, pady=(20, 8))
        self._scan_btn = tk.Button(btn_fr,
            text="▶  START SCAN", font=F_BIGBTN,
            bg=BTN_START, fg="white",
            activebackground=BTN_START_H, activeforeground="white",
            relief="flat", cursor="hand2", padx=48, pady=18,
            command=self._toggle_scan,
        )
        self._scan_btn.pack()
        tk.Label(btn_fr,
            text="Move camera slowly around the object from all angles",
            font=F_SMALL, bg=PANEL, fg=DIM).pack(pady=(5, 0))
        tk.Label(btn_fr,
            textvariable=self._guidance_var,
            font=F_SMALL, bg=PANEL, fg=ACCENT,
            wraplength=460, justify="center").pack(pady=(8, 0))

        # ── Stats grid ──────────────────────────────────────────────────
        stats = tk.Frame(card, bg=PANEL)
        stats.grid(row=1, column=0, sticky="ew", padx=20, pady=4)
        stats.columnconfigure(0, weight=1)
        stats.columnconfigure(1, weight=1)

        def stat_block(p, title, var, row, col):
            fr = tk.Frame(p, bg=CARD, padx=12, pady=8)
            fr.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            tk.Label(fr, text=title, font=F_SMALL, bg=CARD, fg=DIM).pack(anchor="w")
            tk.Label(fr, textvariable=var, font=F_STAT, bg=CARD, fg=TEXT).pack(anchor="w")
            p.rowconfigure(row, weight=1)
            p.columnconfigure(col, weight=1)

        stat_block(stats, "Elapsed",        self._elapsed_var, 0, 0)
        stat_block(stats, "Map nodes (WM)", self._wm_var,      0, 1)
        stat_block(stats, "Tracking",       self._odom_var,    1, 0)
        stat_block(stats, "Loop closures",  self._lc_var,      1, 1)

        # ── AI thinking panel ────────────────────────────────────────────
        ai_card = tk.Frame(card, bg="#12122a")
        ai_card.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 8))
        ai_card.columnconfigure(0, weight=1)
        ai_card.rowconfigure(1, weight=1)

        ai_hdr = tk.Frame(ai_card, bg="#12122a")
        ai_hdr.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        tk.Label(ai_hdr, text="AI ASSISTANT", font=F_BOLD, bg="#12122a", fg=AI_ACCENT).pack(side="left")
        self._ai_badge = tk.Label(ai_hdr, text="● Off",
            font=F_SMALL, bg="#12122a", fg=DIM)
        self._ai_badge.pack(side="left", padx=8)
        self._small_btn(ai_hdr, "Clear", self._clear_ai_text).pack(side="right")

        self._ai_text = tk.Text(
            ai_card, height=6, bg=AI_BG, fg=AI_TEXT,
            font=F_MONO, relief="flat", state="disabled",
            wrap="word", insertbackground=AI_TEXT,
        )
        self._ai_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 6))
        ai_sb = tk.Scrollbar(ai_card, command=self._ai_text.yview, bg=PANEL)
        ai_sb.grid(row=1, column=1, sticky="ns", pady=(0, 6))
        self._ai_text.config(yscrollcommand=ai_sb.set)

        # Instruction entry (user → AI)
        instr_fr = tk.Frame(ai_card, bg="#12122a")
        instr_fr.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
        instr_fr.columnconfigure(0, weight=1)
        self._instr_var = tk.StringVar()
        instr_e = tk.Entry(instr_fr, textvariable=self._instr_var,
            bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=F_LABEL)
        instr_e.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        instr_e.bind("<Return>", lambda e: self._send_instruction())
        self._small_btn(instr_fr, "Send", self._send_instruction).grid(row=0, column=1)

        # ── Export bar ───────────────────────────────────────────────────
        exp = tk.Frame(right, bg=PANEL)
        exp.grid(row=1, column=0, sticky="ew")
        exp.columnconfigure(1, weight=1)

        tk.Label(exp, text="EXPORT", font=F_BOLD, bg=PANEL, fg=ACCENT).grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w", columnspan=3)

        tk.Label(exp, text="Output:", font=F_LABEL, bg=PANEL, fg=DIM).grid(
            row=1, column=0, padx=(12, 4), pady=4, sticky="w")
        self._outdir_var = tk.StringVar(value=self._settings["output_dir"])
        tk.Entry(exp, textvariable=self._outdir_var, bg=CARD, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=F_LABEL).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew")
        self._small_btn(exp, "Browse", self._browse_out).grid(
            row=1, column=2, padx=(0, 8), pady=4)

        tk.Label(exp, text="Isolation:", font=F_LABEL, bg=PANEL, fg=DIM).grid(
            row=2, column=0, padx=(12, 4), pady=4, sticky="w")
        self._isolation_var = tk.StringVar(value=self._settings.get("isolation_profile", "tabletop_object"))
        ttk.Combobox(
            exp,
            textvariable=self._isolation_var,
            values=list(ISOLATION_PROFILES.keys()),
            state="readonly",
            width=24,
            font=F_LABEL,
        ).grid(row=2, column=1, padx=4, pady=4, sticky="ew")
        self._auto_view_var = tk.BooleanVar(value=bool(self._settings.get("auto_open_viewer", False)))
        tk.Checkbutton(
            exp,
            text="Open after export",
            variable=self._auto_view_var,
            bg=PANEL, fg=TEXT, selectcolor=CARD,
            activebackground=PANEL, activeforeground=ACCENT,
            font=F_SMALL, anchor="w",
            command=self._save_settings,
        ).grid(row=2, column=2, padx=(0, 8), pady=4, sticky="w")

        self._db_var = tk.StringVar(value=self._settings["db_path"])
        tk.Button(exp,
            text="  EXPORT POINT CLOUD  ", font=F_BOLD,
            bg=BTN_EXPORT, fg="white",
            activebackground=BTN_EXPORT_H, activeforeground="white",
            relief="flat", cursor="hand2", padx=16, pady=8,
            command=self._do_export,
        ).grid(row=3, column=0, columnspan=3, padx=12, pady=(4, 8), sticky="ew")

        actions = tk.Frame(exp, bg=PANEL)
        actions.grid(row=4, column=0, columnspan=3, padx=12, pady=(0, 4), sticky="ew")
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self._small_btn(actions, "Preview Latest", self._open_latest_cloud).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self._small_btn(actions, "Re-isolate Latest", self._reprocess_latest_export).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        tk.Label(exp, textvariable=self._latest_summary_var,
            font=F_SMALL, bg=PANEL, fg=DIM, wraplength=520, justify="left").grid(
            row=5, column=0, columnspan=3, padx=12, pady=(0, 12), sticky="w")

    def _build_log(self):
        log_fr = tk.Frame(self, bg=PANEL)
        log_fr.pack(fill="x", padx=10, pady=(0, 8))

        bar = tk.Frame(log_fr, bg=PANEL)
        bar.pack(fill="x")
        tk.Label(bar, text="LOG", font=F_BOLD, bg=PANEL, fg=DIM).pack(side="left", padx=8, pady=4)
        self._small_btn(bar, "Clear", self._clear_log).pack(side="right", padx=8, pady=2)
        tk.Label(bar, textvariable=self._status_var, font=F_SMALL, bg=PANEL, fg=ACCENT).pack(
            side="right", padx=4)

        self._log_text = tk.Text(
            log_fr, height=6, bg="#0d0d1a", fg=TEXT,
            font=F_MONO, relief="flat", state="disabled", wrap="word",
        )
        self._log_text.pack(fill="x", padx=4, pady=(0, 4))
        sb = tk.Scrollbar(log_fr, command=self._log_text.yview, bg=PANEL)
        sb.pack(side="right", fill="y")
        self._log_text.config(yscrollcommand=sb.set)
        self._log_text.tag_config("err",  foreground=RED)
        self._log_text.tag_config("warn", foreground=YELLOW)
        self._log_text.tag_config("ok",   foreground=GREEN)
        self._log_text.tag_config("ai",   foreground=AI_ACCENT)

    def _small_btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd,
            bg=BTN_TOOLS, fg=TEXT, font=F_SMALL,
            activebackground=BTN_TOOLS_H, activeforeground=TEXT,
            relief="flat", cursor="hand2", padx=8, pady=4)

    def _after_window_visible(self):
        self.after(120, lambda: (
            self.deiconify(), self.update_idletasks(), self.lift(),
            self.attributes("-topmost", True),
            self.after(250, lambda: self.attributes("-topmost", False)),
        ))

    # ── Preset ────────────────────────────────────────────────────────────
    def _apply_preset(self):
        p = PRESETS.get(self._preset_var.get())
        if p:
            self._dmin_var.set(str(p["depth_min"]))
            self._dmax_var.set(str(p["depth_max"]))
            if self._preset_var.get() == "Object" and self._isolation_var.get() == "largest_cluster":
                self._isolation_var.set("tabletop_object")
        self._save_settings()

    def _set_isolation_profile(self, profile):
        if profile in ISOLATION_PROFILES:
            self._isolation_var.set(profile)
            self._latest_summary_var.set(ISOLATION_TOOLTIPS.get(profile, ""))
            self._save_settings()

    def _set_depth_window(self, depth_min, depth_max):
        depth_min = max(50, int(depth_min))
        depth_max = max(depth_min + 50, int(depth_max))
        self._dmin_var.set(str(depth_min))
        self._dmax_var.set(str(depth_max))
        self._save_settings()
        self._log(f"Depth export window set to {depth_min}–{depth_max} mm", "ai")

    def _set_auto_export(self, enabled):
        self._auto_export.set(bool(enabled))
        self._log(f"Auto-export {'enabled' if enabled else 'disabled'}.", "ai")

    # ── Scan ──────────────────────────────────────────────────────────────
    def _toggle_scan(self):
        if self._scanning:
            self._stop_scan()
        else:
            self._start_scan()

    def _start_scan(self):
        if not os.path.isfile(LAUNCHER):
            messagebox.showerror("Error", f"Launcher not found:\n{LAUNCHER}")
            return
        self._save_settings()
        self._scanning   = True
        self._scan_start = time.time()
        self._scan_btn.config(text="■  STOP SCAN", bg=BTN_STOP, activebackground=BTN_STOP_H)
        self._set_dot(self._dot_stack, YELLOW)
        self._status_var.set("Starting ROS stack…")

        if self._reset_db.get() and os.path.isfile(DEFAULT_DB):
            try:
                os.remove(DEFAULT_DB)
                self._log("Removed previous scan database.", "ok")
            except Exception as e:
                self._log(f"Warning: could not remove DB: {e}", "warn")

        self._monitor.start()
        self._set_dot(self._dot_camera, GREEN)

        _run_launcher(["ros_all"],
            log_cb=lambda ln: self._log(_strip_ansi(ln)),
            done_cb=self._stack_started_cb)

        if self._ai_enabled.get() and self._agent and self._agent.is_alive():
            pass  # already running
        elif self._ai_enabled.get():
            self._start_agent()

    def _stack_started_cb(self, rc):
        if rc == 0:
            self._log("Stack running. Move camera around the object slowly.", "ok")
            self._set_dot(self._dot_stack, GREEN)
            self._status_var.set("Scanning…  move camera slowly from all angles")
        else:
            self._log(f"Stack exited with code {rc}", "err")
            self._set_dot(self._dot_stack, RED)
            self._status_var.set(f"Stack error (code {rc})")

    def _stop_scan(self):
        self._scanning = False
        self._monitor.stop()
        self._scan_btn.config(text="▶  START SCAN", bg=BTN_START, activebackground=BTN_START_H)
        self._set_dot(self._dot_stack, YELLOW)
        self._status_var.set("Stopping stack…")
        _run_launcher(["ros_stop"],
            log_cb=lambda ln: self._log(_strip_ansi(ln)),
            done_cb=self._stack_stopped_cb)

    def _stack_stopped_cb(self, rc):
        self._set_dot(self._dot_stack, DIM)
        self._odom_var.set("—")
        self._status_var.set("Stack stopped.  Ready to export.")
        if self._auto_export.get():
            self._log("Auto-export started…", "ok")
            self.after(800, self._do_export)

    # ── Export ────────────────────────────────────────────────────────────
    def _do_export(self):
        db = self._db_var.get().strip() or DEFAULT_DB
        if not os.path.isfile(db):
            messagebox.showerror("No scan data",
                f"Database not found:\n{db}\n\nRun a scan first.")
            return
        preset = PRESETS.get(self._preset_var.get(), PRESETS["Object"])
        q      = QUALITY.get(self._quality_var.get(), QUALITY["Precision (1 mm)"])
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir  = self._outdir_var.get().strip() or ROOT_DIR
        out_name = f"scan_{ts}_cloud"
        out_path = os.path.join(out_dir, out_name + ".ply")
        os.makedirs(out_dir, exist_ok=True)

        self._log(f"Exporting → {out_path}", "ok")
        self._log(f"  voxel={q['voxel']*1000:.0f}mm  "
                  f"depth {preset['export_min_range']:.2f}–{preset['export_max_range']:.1f}m")
        self._status_var.set("Exporting point cloud…")

        def worker():
            try:
                cmd = (
                    "source /opt/ros/jazzy/setup.bash && "
                    f"rtabmap-export --cloud "
                    f"--decimation 1 "
                    f"--voxel {q['voxel']} "
                    f"--noise_radius {q['noise_r']} "
                    f"--noise_k {q['noise_k']} "
                    f"--max_range {preset['export_max_range']} "
                    f"--min_range {preset['export_min_range']} "
                    f'--output "{out_name}" '
                    f'--output_dir "{out_dir}" '
                    f'"{db}"'
                )
                proc = subprocess.run(
                    ["bash", "-lc", cmd], cwd=ROOT_DIR,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, check=False,
                    env={**os.environ, "FASTDDS_BUILTIN_TRANSPORTS": "UDPv4"},
                )
                for line in proc.stdout.splitlines():
                    self._log(_strip_ansi(line))
                for candidate in [out_path, os.path.join(out_dir, out_name + "_cloud.ply")]:
                    if os.path.isfile(candidate):
                        mb = os.path.getsize(candidate) / 1_048_576
                        self._latest_raw_export_path = candidate
                        self._log(f"Saved: {candidate}  ({mb:.1f} MB)", "ok")
                        self._status_var.set(f"Saved: {os.path.basename(candidate)}")
                        self._run_isolation_pipeline(candidate, q["voxel"])
                        return
                self._log(f"Export ended (code {proc.returncode}) — check log", "warn")
                self._status_var.set("Export finished — check log")
            except Exception as exc:
                self._log(f"Export error: {exc}", "err")
        threading.Thread(target=worker, daemon=True).start()

    def _run_isolation_pipeline(self, cloud_path, voxel):
        if isolate_point_cloud_variants is None:
            self._latest_export_path = cloud_path
            self._latest_summary_var.set("Isolation tools unavailable in this environment.")
            if _POINT_CLOUD_TOOLS_ERROR:
                self._log(f"Isolation unavailable: {_POINT_CLOUD_TOOLS_ERROR}", "warn")
            return

        profile = self._isolation_var.get()
        self._log(f"Running isolation profile: {profile}", "ok")
        try:
            summary = isolate_point_cloud_variants(
                cloud_path,
                output_dir=os.path.dirname(cloud_path),
                profile=profile,
                voxel=max(voxel, 0.001),
            )
        except Exception as exc:
            self._latest_export_path = cloud_path
            self._latest_summary_var.set(f"Isolation failed, raw cloud kept: {os.path.basename(cloud_path)}")
            self._log(f"Isolation error: {exc}", "warn")
            return

        self._latest_isolation_summary = summary
        self._latest_export_path = summary.get("recommended_path") or cloud_path
        self._latest_isolation_points = 0
        for entry in summary.get("profiles", []):
            self._log(
                f"Isolation {entry['profile']}: {entry['points']:,} pts → {os.path.basename(entry['path'])}",
                "ok",
            )
            if entry["path"] == self._latest_export_path:
                self._latest_isolation_points = int(entry["points"])

        chosen = summary.get("recommended_profile", profile)
        self._latest_summary_var.set(
            f"Latest export ready. Recommended: {chosen} with {self._latest_isolation_points:,} points."
        )
        self._status_var.set(f"Isolation ready: {os.path.basename(self._latest_export_path)}")
        if self._auto_view_var.get():
            self.after(0, self._open_latest_cloud)

    def _reprocess_latest_export(self):
        if not self._latest_raw_export_path or not os.path.isfile(self._latest_raw_export_path):
            self._log("No raw export available to re-isolate yet.", "warn")
            return
        q = QUALITY.get(self._quality_var.get(), QUALITY["Precision (1 mm)"])
        threading.Thread(
            target=lambda: self._run_isolation_pipeline(self._latest_raw_export_path, q["voxel"]),
            daemon=True,
        ).start()

    def _open_cloud_path(self, path):
        if not path or not os.path.isfile(path):
            self._log("No saved cloud available to preview.", "warn")
            return
        try:
            subprocess.Popen([sys.executable, os.path.join(ROOT_DIR, "view_cloud.py"), path], cwd=ROOT_DIR)
        except Exception as exc:
            self._log(f"Preview failed: {exc}", "warn")

    def _open_latest_cloud(self):
        self._open_cloud_path(self._latest_export_path or self._latest_raw_export_path)

    # ── AI agent lifecycle ────────────────────────────────────────────────
    def _on_ai_toggle(self):
        if self._ai_enabled.get():
            self._start_agent()
        else:
            self._stop_agent()

    def _start_agent(self):
        model_key = self._model_var.get()
        model_id, provider = MODELS.get(model_key, ("claude-sonnet-4-6", "anthropic"))

        # Resolve API key
        _openai_key = next((k for f in OPENAI_KEY_FILES if (k := _load_key(f))), "")
        if provider == "anthropic":
            key = (os.environ.get("ANTHROPIC_API_KEY", "")
                   or _load_key(ANTHROPIC_KEY_FILE)
                   or _openai_key)
        else:
            key = os.environ.get("OPENAI_API_KEY", "") or _openai_key

        if not key:
            messagebox.showerror("API key missing",
                f"No API key found for {provider}.\n"
                f"Add key to one of:\n  " + "\n  ".join(OPENAI_KEY_FILES) + "\n"
                f"or set the OPENAI_API_KEY environment variable.")
            self._ai_enabled.set(False)
            return

        try:
            tick = max(5, int(self._tick_var.get() or 8))
        except ValueError:
            tick = 8

        self._write_ai(f"Starting AI agent: {model_key}  (every {tick}s)\n")
        self._ai_badge.config(text="● Starting…", fg=YELLOW)
        self._ai_status_var.set("Starting…")
        self._set_dot(self._dot_ai, YELLOW)

        try:
            self._agent = ROSAgentRunner(
                bridge=self._bridge,
                on_thinking=lambda t: self._log_q.put(("ai_text", t)),
                on_command=lambda v, a: self._log_q.put(("log", f"[AI cmd] {v} {a or ''}", "ai")),
                on_error=lambda e: self._log_q.put(("log", f"[AI err] {e}", "err")),
                model=model_id,
                api_key=key,
                provider=provider,
                tick_interval=tick,
            )
            self._agent.start()
            self._ai_badge.config(text="● Active", fg=GREEN)
            self._ai_status_var.set("Active")
            self._set_dot(self._dot_ai, GREEN)
        except Exception as exc:
            self._log(f"Failed to start AI agent: {exc}", "err")
            self._ai_enabled.set(False)
            self._ai_badge.config(text="● Error", fg=RED)
            self._ai_status_var.set("Error")
            self._set_dot(self._dot_ai, RED)

    def _stop_agent(self):
        if self._agent:
            self._agent.stop()
            self._agent = None
        self._ai_badge.config(text="● Off", fg=DIM)
        self._ai_status_var.set("Off")
        self._set_dot(self._dot_ai, DIM)
        self._write_ai("\n[Agent stopped]\n")

    def _send_instruction(self):
        if not self._agent or not self._agent.is_alive():
            self._log("AI agent is not running. Enable it first.", "warn")
            return
        text = self._instr_var.get().strip()
        if text:
            self._instr_var.set("")
            self._write_ai(f"\n[You] {text}\n")
            self._agent.send_instruction(text)

    def _write_ai(self, text):
        try:
            self._ai_text.config(state="normal")
            self._ai_text.insert("end", text)
            self._ai_text.see("end")
            self._ai_text.config(state="disabled")
        except Exception:
            pass

    def _clear_ai_text(self):
        self._ai_text.config(state="normal")
        self._ai_text.delete("1.0", "end")
        self._ai_text.config(state="disabled")

    # ── Tools ─────────────────────────────────────────────────────────────
    def _cmd(self, mode, extra=None):
        _run_launcher([mode] + (extra or []),
            log_cb=lambda ln: self._log(_strip_ansi(ln)))

    def _open_db_viewer(self):
        self._cmd("ros_db", [self._db_var.get().strip() or DEFAULT_DB])

    def _open_outdir(self):
        d = self._outdir_var.get().strip() or ROOT_DIR
        try:
            subprocess.Popen(["xdg-open", d])
        except Exception:
            pass

    def _browse_out(self):
        d = filedialog.askdirectory(
            title="Select output folder",
            initialdir=self._outdir_var.get() or ROOT_DIR)
        if d:
            self._outdir_var.set(d)
            self._save_settings()

    def _derive_guidance(self, stats):
        if not self._scanning:
            if self._latest_export_path:
                return "Scan complete. Preview the isolated cloud or re-run a different isolation profile."
            return "Start a scan, circle the object once at mid-height, then add a top pass."

        odom = stats["odom_quality"]
        wm = stats["wm"]
        loops = stats["loop_closures"]
        if odom == 0:
            return "Tracking lost. Pause movement, show more texture, and reacquire from a known angle."
        if 0 < odom < 12:
            return "Tracking is weak. Slow down and keep 40-60% overlap between viewpoints."
        if wm < 20:
            return "Build base coverage first: make one smooth ring around the object."
        if loops < 1:
            return "Revisit your starting angle to create a loop closure before moving higher."
        if loops < 2:
            return "Coverage is building. Add high 3/4 views to improve object isolation."
        return "Coverage looks good. Capture a top pass, then stop and export isolated variants."

    # ── Stats callback ────────────────────────────────────────────────────
    def _on_stats(self, stats):
        self._log_q.put(("stats", stats))

    def _apply_stats(self, stats):
        self._wm_var.set(str(stats["wm"]) if stats["wm"] else "—")
        self._lc_var.set(str(stats["loop_closures"]))
        self._guidance_var.set(self._derive_guidance(stats))
        q = stats["odom_quality"]
        if q < 0:
            self._odom_var.set("—");   self._set_dot(self._dot_odom, DIM)
        elif q == 0:
            self._odom_var.set("Lost (0)"); self._set_dot(self._dot_odom, RED)
        elif q < 20:
            self._odom_var.set(f"Weak ({q})"); self._set_dot(self._dot_odom, YELLOW)
        else:
            self._odom_var.set(f"Good ({q})"); self._set_dot(self._dot_odom, GREEN)

    # ── Log ───────────────────────────────────────────────────────────────
    def _log(self, msg, tag=""):
        self._log_q.put(("log", msg, tag))

    def _poll_logs(self):
        while True:
            try:
                item = self._log_q.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "log":
                _, msg = item[0], item[1]
                tag = item[2] if len(item) > 2 else ""
                self._write_log(msg, tag)
            elif kind == "stats":
                self._apply_stats(item[1])
            elif kind == "ai_text":
                self._write_ai(item[1])
            elif kind == "ai_say":
                self._status_var.set(f"[AI] {item[1]}")
            elif kind == "ai_instr":
                pass  # consumed by bridge on next tick (future: store for context)

        self.after(80, self._poll_logs)

    def _write_log(self, msg, tag=""):
        self._log_text.config(state="normal")
        if not tag:
            ml = msg.lower()
            if any(w in ml for w in ("error", "failed", "fatal", "exception")):
                tag = "err"
            elif "warn" in ml:
                tag = "warn"
            elif any(w in ml for w in ("complete", "export", "online", "ready", "start")):
                tag = "ok"
            elif "[ai]" in ml:
                tag = "ai"
        self._log_text.insert("end", f"[{_ts()}] {msg}\n", tag or "")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    # ── Clock ─────────────────────────────────────────────────────────────
    def _tick(self):
        if self._scanning:
            elapsed = int(time.time() - self._scan_start)
            m, s = divmod(elapsed, 60)
            self._elapsed_var.set(f"{m:02d}:{s:02d}")
        self.after(1000, self._tick)


if __name__ == "__main__":
    app = App()
    app.mainloop()
