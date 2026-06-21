#!/usr/bin/env python3
"""
AI Scanner Agent — Claude-powered real-time controller for the 3D object scanner.

Runs in a background thread.  Each tick it:
  1. Reads the current scanner state (via ScannerBridge injected from scanner_gui.py).
  2. Sends the state to Claude with an embedded skill prompt about how the scanner stack works.
  3. Streams the response so the UI shows Claude's reasoning in real time.
  4. Parses commands from the response and dispatches them back through the bridge.

Commands Claude can issue (one or more per response, each on its own line, first token is the verb):
  CAPTURE          — take a single frame
  START_AUTO       — begin auto-capture
  STOP_AUTO        — stop auto-capture
  BUILD_MESH       — run ICP + Poisson reconstruction on captured frames
  EXPORT_LIVE      — quick-save the live TSDF mesh
  SAVE_BEST        — smart export (live mesh now or full reconstruct later)
  OPEN_BLENDER     — send the last export to Blender
    SET_ISOLATION:x  — change the object-isolation strategy
    SET_DEPTH_WINDOW:min:max — adjust near/far depth gates in mm
    SET_AUTO_EXPORT:on|off — toggle automatic export after stopping
    ISOLATE_LATEST   — rerun isolation on the latest export
    OPEN_LATEST      — preview the latest saved cloud
  SET_MODE:<mode>  — change scan mode (handheld / turntable / surface)
  CLEAR_SESSION    — discard frames and restart
  WAIT             — do nothing this tick
  SAY:<text>       — post a status message without other action

The agent also emits a free-text "thinking" stream that the UI displays.
"""

import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional


def _find_claude_cli() -> Optional[str]:
    """Locate the Claude Code CLI binary, if installed.

    Lets the agent run through the user's existing Claude Code auth
    (`claude -p`) instead of a raw ANTHROPIC_API_KEY.
    """
    found = shutil.which("claude")
    if found:
        return found
    for cand in (
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
    ):
        if os.path.exists(cand):
            return cand
    return None

_ANTHROPIC_IMPORT_ERROR = None
try:
    import anthropic
except ImportError as _e:
    anthropic = None  # type: ignore
    _ANTHROPIC_IMPORT_ERROR = str(_e)

_OPENAI_IMPORT_ERROR = None
try:
    from openai import OpenAI
except ImportError as _e:
    OpenAI = None  # type: ignore
    _OPENAI_IMPORT_ERROR = str(_e)

# Model alias passed to `claude -p --model <alias>` for the CLI provider.
# haiku keeps per-tick latency/cost low for a ~4 s agent loop.
CLAUDE_CLI_MODEL = os.environ.get("SCANNER_CLAUDE_CLI_MODEL", "haiku")


# ── Skill prompt — the agent's working knowledge of the scanner stack ──────────
SCANNER_SKILL = """
You are the real-time AI controller for a 3D object scanner built on PrimeSense / ASUS Xtion
depth cameras.  Your job is to drive the scanner to produce clean 3D meshes of objects with as
little human intervention as possible.

=== HOW THE SCANNER WORKS ===
• Scan modes:
  - handheld: user moves the camera around the object. Auto-capture continuously grabs depth frames.
    The TSDF volume builds a live mesh in real time from frame-to-frame ICP odometry.
    After enough frames are captured, BUILD_MESH runs ICP+Poisson reconstruction.
  - turntable: camera is fixed, object rotates. Scanner auto-steps through N angles.
  - surface: single averaged patch from a fixed viewpoint. Best for flat surfaces.

• Depth quality score is 0..1 — a score above 0.5 is good. Below 0.3 is too noisy/sparse.
• Motion line tells you how far the camera moved between frames.  "tracking weak" means ICP lost.
  "—" means nothing captured yet.
• Live mesh vertex count tells you how much detail has been integrated into the TSDF volume.
  At least 500 vertices before EXPORT_LIVE. At least 2000 before BUILD_MESH is worth running.
• Frames captured is the number of unified depth frames in the queue for reconstruction.
  Aim for at least 20 frames before BUILD_MESH for a handheld scan.

=== DECISION RULES ===
1. If cameras are not on — wait. You cannot control hardware startup.
2. If quality score < 0.3 and no frames yet — SAY a hint and WAIT (sensor warming or object too far).
3. If auto-capture is off and cameras are on — START_AUTO to begin building the mesh.
4. While building: prefer WAIT. Only intervene if:
   - Motion shows "tracking weak" many times in a row → STOP_AUTO, BUILD_MESH (save what you have)
   - Frame count >= 40 and live mesh has >= 5000 vertices → STOP_AUTO, BUILD_MESH
   - Frame count >= 20 and the user hasn't produced any mesh yet → BUILD_MESH (starter mesh)
5. Surface mode: CAPTURE (not auto) when cameras are on and quality is decent.
6. After BUILD_MESH or EXPORT_LIVE is confirmed: OPEN_BLENDER to hand off.
7. Don't spam commands. Issue at most 2 commands per tick. Prefer WAIT when scan is progressing.

=== RESPONSE FORMAT ===
Think step by step in plain English first (2-4 sentences). Then on clearly-separated lines, each
starting with exactly one command verb from the list (CAPTURE, START_AUTO, STOP_AUTO, BUILD_MESH,
EXPORT_LIVE, SAVE_BEST, OPEN_BLENDER, SET_MODE:<mode>, CLEAR_SESSION, WAIT, SAY:<text>).
Commands are parsed from lines that START with these keywords — anything else is treated as thinking.

Example response:
  The camera is running with good quality (0.62) and 15 frames captured. Auto-capture is active so
  the mesh is growing. I'll let it run until we have 30+ frames.
  WAIT

Another example:
  Quality is 0.71 and we've hit 42 frames. The live mesh has 8000 vertices — enough for a good mesh.
  I'll stop auto-capture and run full reconstruction.
  STOP_AUTO
  BUILD_MESH
""".strip()


# ── State snapshot (what the bridge exposes each tick) ────────────────────────
class ScannerState:
    """Immutable snapshot of scanner telemetry passed to the agent each tick."""
    __slots__ = (
        "camera_on", "scan_mode", "workflow", "frames_captured",
        "live_mesh_vertices", "auto_capture_on", "turntable_running",
        "surface_running", "last_export", "cameras_count", "calibrated",
        "motion", "depth_quality", "tracking_weak_streak",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    def to_prompt_text(self) -> str:
        d = self.to_dict()
        lines = ["=== CURRENT SCANNER STATE ==="]
        for k, v in d.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


# ── Command names ──────────────────────────────────────────────────────────────
VALID_COMMANDS = frozenset([
    "CAPTURE", "START_AUTO", "STOP_AUTO", "BUILD_MESH",
    "EXPORT_LIVE", "SAVE_BEST", "OPEN_BLENDER", "SET_MODE",
    "SET_ISOLATION", "SET_DEPTH_WINDOW", "SET_AUTO_EXPORT",
    "ISOLATE_LATEST", "OPEN_LATEST", "CLEAR_SESSION", "WAIT", "SAY",
])


def parse_commands(text: str):
    """
    Extract (verb, arg) pairs from the agent's text response.
    Only lines whose first token is a known command verb are parsed.
    Returns list of (verb:str, arg:str|None).
    """
    commands = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(":", 1)
        verb = parts[0].strip().upper()
        arg = parts[1].strip() if len(parts) == 2 else None
        if verb in VALID_COMMANDS:
            commands.append((verb, arg))
    return commands


# ── Agent ──────────────────────────────────────────────────────────────────────

class AIAgentController:
    """
    Background thread that drives the scanner using Claude.

    bridge: ScannerBridge instance (or any object with get_state() and dispatch(verb, arg))
    on_thinking: callback(str) — called with streamed text chunks (for the UI thought panel)
    on_command: callback(verb, arg) — called when a parsed command is dispatched
    on_error: callback(str) — called on any recoverable error
    tick_interval: seconds between agent ticks (default 3.5)
    model: model ID (Anthropic or OpenAI)
    api_key: API key (Anthropic or OpenAI)
    provider: 'auto', 'anthropic', or 'openai'
    """

    def __init__(
        self,
        bridge,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_command: Optional[Callable[[str, Optional[str]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        tick_interval: float = 4.0,
        model: str = "claude-3-5-haiku-20241022",
        api_key: Optional[str] = None,
        provider: str = "auto",
    ):
        self.bridge = bridge
        self.on_thinking = on_thinking or (lambda t: None)
        self.on_command = on_command or (lambda v, a: None)
        self.on_error = on_error or (lambda e: None)
        self.tick_interval = tick_interval
        self.model = model

        resolved_key = (
            api_key
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        )

        self.provider = self._resolve_provider(provider, resolved_key, model)

        # No API key needed when routing through the local Claude Code CLI.
        if self.provider == "claude_cli":
            self._claude_bin = _find_claude_cli()
            if not self._claude_bin:
                raise RuntimeError(
                    "provider 'claude_cli' requested but the 'claude' CLI was not "
                    "found on PATH (looked for ~/.local/bin/claude). Install Claude "
                    "Code or set ANTHROPIC_API_KEY / OPENAI_API_KEY instead."
                )
            # Map the configured model to a CLI alias (claude -p --model <alias>).
            self._cli_model = CLAUDE_CLI_MODEL
            self._client = None
        elif not resolved_key:
            raise ValueError(
                "No API key. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, install the "
                "Claude Code CLI (provider='claude_cli'), or pass api_key=."
            )
        elif self.provider == "anthropic":
            if anthropic is None:
                raise ImportError(
                    f"anthropic package not installed: {_ANTHROPIC_IMPORT_ERROR}"
                )
            self._client = anthropic.Anthropic(api_key=resolved_key)
            if self.model.startswith("gpt-"):
                self.model = "claude-3-5-haiku-20241022"
        elif self.provider == "openai":
            if OpenAI is None:
                raise ImportError(
                    f"openai package not installed: {_OPENAI_IMPORT_ERROR}"
                )
            self._client = OpenAI(api_key=resolved_key)
            if self.model.startswith("claude"):
                self.model = "gpt-4o-mini"
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._paused = False
        self._history = []      # keep last N user+assistant turns for context
        self._max_history = 6   # pairs
        self._tracking_weak_count = 0
        self._last_motion_seen = ""
        self._pending_instruction: Optional[str] = None
        self._instruction_lock = threading.Lock()

    def _resolve_provider(self, provider: str, key: str, model: str) -> str:
        p = (provider or "auto").strip().lower()
        if p in ("anthropic", "openai", "claude_cli"):
            return p
        # Auto: prefer a real API key; otherwise fall back to the local
        # Claude Code CLI so the agent works with no key configured.
        if not key and _find_claude_cli():
            return "claude_cli"
        if model.startswith("gpt-"):
            return "openai"
        if model.startswith("claude"):
            return "anthropic"
        if key.startswith("sk-ant-"):
            return "anthropic"
        if key.startswith("sk-"):
            return "openai"
        return "anthropic"

    def _run_claude_cli(self) -> str:
        """Run one completion via `claude -p` (Claude Code CLI).

        The CLI uses the user's existing Claude Code auth, so no API key is
        required. The skill prompt is supplied via --append-system-prompt and
        the running conversation is piped in on stdin. Returns the model's
        text (not streamed — the CLI yields the full answer at once).
        """
        convo = []
        for turn in self._history:
            tag = "SCANNER" if turn["role"] == "user" else "YOU"
            convo.append(f"=== {tag} ===\n{turn['content']}")
        prompt = "\n\n".join(convo)

        cmd = [
            self._claude_bin,
            "-p",
            "--model", self._cli_model,
            "--append-system-prompt", SCANNER_SKILL,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            self.on_error("claude CLI timed out (90s); issuing WAIT")
            return "WAIT"
        if proc.returncode != 0:
            self.on_error(
                f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}"
            )
            return "WAIT"
        return proc.stdout.strip()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="AIAgentController"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def is_alive(self):
        return self._running and (self._thread is not None and self._thread.is_alive())

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def send_instruction(self, text: str):
        """Queue a one-shot user instruction to be included in the next tick."""
        with self._instruction_lock:
            self._pending_instruction = text.strip()

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _run_loop(self):
        self.on_thinking("AI agent started — watching the scanner…\n")
        while self._running:
            if not self._paused:
                try:
                    self._tick()
                except Exception as e:
                    self.on_error(f"Agent tick error: {e}")
            time.sleep(self.tick_interval)

    def _tick(self):
        state: ScannerState = self.bridge.get_state()

        # Track consecutive "tracking weak" detections independently of Claude
        motion = state.motion or ""
        if "tracking weak" in motion.lower():
            self._tracking_weak_count += 1
        elif motion and motion != "—" and motion != self._last_motion_seen:
            self._tracking_weak_count = 0
        self._last_motion_seen = motion

        # Inject tracking streak into state for Claude
        state.tracking_weak_streak = self._tracking_weak_count

        user_msg = state.to_prompt_text()

        # Attach any pending user instruction and clear it
        with self._instruction_lock:
            instruction = self._pending_instruction
            self._pending_instruction = None
        if instruction:
            user_msg += f"\n\n=== USER INSTRUCTION ===\n{instruction}"

        # Maintain short history so Claude has some context across ticks
        self._history.append({"role": "user", "content": user_msg})
        if len(self._history) > self._max_history * 2:
            # keep only the last N pairs
            self._history = self._history[-(self._max_history * 2):]

        # Stream the response
        full_response = ""
        self.on_thinking("\n─── Agent thinking ───\n")

        if self.provider == "anthropic":
            with self._client.messages.stream(
                model=self.model,
                max_tokens=512,
                system=SCANNER_SKILL,
                messages=self._history,
            ) as stream:
                for chunk in stream.text_stream:
                    full_response += chunk
                    self.on_thinking(chunk)
        elif self.provider == "claude_cli":
            # Route through the local Claude Code CLI (no API key needed).
            full_response = self._run_claude_cli()
            self.on_thinking(full_response)
        else:
            msgs = [{"role": "system", "content": SCANNER_SKILL}] + self._history
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=msgs,
                max_tokens=512,
                stream=True,
            )
            for event in stream:
                try:
                    delta = event.choices[0].delta.content
                except Exception:
                    delta = None
                if delta:
                    full_response += delta
                    self.on_thinking(delta)

        self.on_thinking("\n")

        # Record assistant turn for history
        self._history.append({"role": "assistant", "content": full_response})

        # Parse and dispatch commands
        commands = parse_commands(full_response)
        for verb, arg in commands:
            if verb == "WAIT":
                continue
            self.on_command(verb, arg)
            try:
                self.bridge.dispatch(verb, arg)
            except Exception as e:
                self.on_error(f"Command dispatch failed ({verb}): {e}")


# ── Bridge (adapter between the agent and the scanner GUI) ────────────────────
class ScannerBridge:
    """
    Thin adapter so the AI agent can read scanner state and issue commands
    without depending on Tkinter internals.

    Instantiated inside scanner_gui.py and passed to AIAgentController.
    """

    def __init__(self, app):
        """app: the ScannerApp (Tkinter root) instance."""
        self._app = app

    # ── State ──────────────────────────────────────────────────────────────────

    def get_state(self) -> ScannerState:
        app = self._app
        with app._lock:
            frames = len(app._combined_clouds)
            motion = str(app._last_motion_text)
            live_verts = int(getattr(app, "_live_mesh_vertices_est", 0))
        quality = str(getattr(app, "_last_quality_text", "—"))

        return ScannerState(
            camera_on=bool(app._camera_on),
            scan_mode=str(app._mode.get()),
            workflow=str(app._workflow_mode.get()) if hasattr(app, "_workflow_mode") else "unknown",
            frames_captured=frames,
            live_mesh_vertices=live_verts,
            auto_capture_on=bool(app._auto_capture_on),
            turntable_running=bool(app._turntable_running),
            surface_running=bool(app._surface_running),
            last_export=str(app._last_export_obj) if app._last_export_obj else None,
            cameras_count=int(app._num_cams),
            calibrated=app._transforms is not None,
            motion=motion,
            depth_quality=quality,
            tracking_weak_streak=0,
        )

    # ── Commands ───────────────────────────────────────────────────────────────

    def dispatch(self, verb: str, arg: Optional[str] = None):
        """Schedule a command on the Tkinter main thread (thread-safe)."""
        self._app.after(0, self._run_command, verb, arg)

    def _run_command(self, verb: str, arg: Optional[str]):
        app = self._app
        verb = verb.upper()
        if verb == "CAPTURE":
            if app._camera_on and not app._auto_capture_on:
                app._capture_frame()
        elif verb == "START_AUTO":
            if app._camera_on and not app._auto_capture_on:
                app._start_auto_capture()
        elif verb == "STOP_AUTO":
            if app._auto_capture_on:
                app._stop_auto_capture()
        elif verb == "BUILD_MESH":
            with app._lock:
                has_cloud = (
                    len(app._combined_clouds) > 0
                    or any(len(c) > 0 for c in app._clouds)
                )
            if has_cloud:
                app._reconstruct()
        elif verb == "EXPORT_LIVE":
            app._export_live_mesh()
        elif verb == "SAVE_BEST":
            app._finish_best_object()
        elif verb == "OPEN_BLENDER":
            app._open_last_in_blender()
        elif verb == "SET_MODE":
            if arg in ("handheld", "turntable", "surface"):
                app._mode.set(arg)
                app._on_mode_change()
        elif verb == "CLEAR_SESSION":
            app._clear_session()
        elif verb == "SAY":
            if arg:
                app._set_status(f"[AI] {arg}")
