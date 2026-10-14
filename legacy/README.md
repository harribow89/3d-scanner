# legacy/

Code from earlier iterations that the current app does not use. Kept for
reference; nothing here is wired into `scanner_gui.py` or `run_scanner_docker.sh`.

- **run_prebuilt_stack.sh** — launcher for a ROS 2 install on the *host*
  (`/opt/ros/jazzy`), with hard-coded `/home/jim/Desktop/scanner` paths. The
  project moved to Docker (`run_scanner_docker.sh`), which needs no host ROS.
- **ai_scanner_agent.py** — a Claude/GPT "scan controller" thread written for
  the old Tkinter app (`ros_scanner_app.py`, deleted). It expects a
  `ScannerBridge` with `get_state()` / `dispatch(verb, arg)` that no longer
  exists. The `claude_cli` provider (routing through `claude -p`) is still a
  reasonable design if an AI advisor is ever re-added on top of
  `scanner_core.read_telemetry()`.
