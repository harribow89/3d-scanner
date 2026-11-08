#!/usr/bin/env python3
"""head_record — record an xtion_head stream to disk. This is the tablet side
of "capture on the tablet, process on the PC": the head streams the cameras,
this writes every frame to a recording that head_replay.py plays back to the
scanner on the PC, or that `station_scan.py import` fuses into a sweep.

    head_record.py HOST[:PORT] --out DIR [--label NAME]
                   (--sweep N | --seconds S | until Ctrl-C)
                   [--fps 15] [--no-color] [--max-mb 8000] [--timeout 60]

Sweep mode stops once every camera in the head's hello has delivered N depth
frames (and N colour frames when colour is on): the Matterport-style "stand
still and stack frames" capture. Continuous mode records everything, for
RTAB-Map SLAM on replay. Each run makes DIR/<YYYYmmdd-HHMMSS>[_label]/
(format: head_recording.py). The last line printed is `RECORDING <path>`.
Exit 0 when the recording is complete, 1 when it stopped short.
"""
import argparse
import json
import os
import re
import signal
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_recording as rec  # noqa: E402


def safe_label(label):
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or "").strip()).strip("-.")
    return label[:40]


def record(host, port, out_root, label="", sweep=0, seconds=0.0, fps=15.0, color=True,
           max_mb=8000.0, timeout=60.0, name=None, quiet=False, log=print):
    """Record until the sweep/seconds/size/timeout condition. Returns
    (path, complete, meta)."""
    sock, hello, buf = rec.connect(host, port, fps=fps, color=color)
    sock.settimeout(5.0)
    cams = [c["ns"] for c in hello.get("cameras", [])]
    if not cams:
        log("head reports no cameras; recording whatever arrives")
    mode = "sweep" if sweep else "continuous"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = name or (stamp + (f"_{safe_label(label)}" if safe_label(label) else ""))
    path = os.path.join(out_root, name)
    if os.path.exists(path):
        path = os.path.join(out_root, f"{name}_{int(time.time()) % 1000:03d}")
    writer = rec.Writer(path, hello, {"fps": fps, "color": color}, mode=mode, label=label)
    log(f"recording {mode} from {hello.get('host', host)} ({', '.join(cams) or 'no cameras yet'})"
        + (f": {sweep} frames per camera" if sweep else (f" for {seconds:.0f} s" if seconds else " until stopped"))
        + f" -> {path}")

    stop = {"flag": False, "why": ""}

    def on_signal(signum, _frame):
        stop["flag"], stop["why"] = True, f"signal {signum}"
    old = [(s, signal.signal(s, on_signal)) for s in (signal.SIGINT, signal.SIGTERM)]

    def sweep_done():
        if not sweep or not cams:
            return False
        for ns in cams:
            if writer.count(ns, "depth") < sweep:
                return False
            if color and writer.count(ns, "rgb") < sweep:
                return False
        return True

    t0 = time.time()
    last_report = 0.0
    last_frame = t0
    complete = False
    why = ""
    try:
        while not stop["flag"]:
            now = time.time()
            if seconds and now - t0 >= seconds:
                complete, why = True, f"{seconds:.0f} s recorded"
                break
            if writer.bytes >= max_mb * 1e6:
                complete, why = (mode == "continuous"), f"size limit {max_mb:.0f} MB reached"
                break
            if timeout and now - last_frame > timeout:
                why = f"no frames for {timeout:.0f} s"
                break
            try:
                line, buf = rec.read_line(sock, buf)
            except socket.timeout:
                continue
            except ConnectionError as e:
                why = str(e)
                break
            msg = json.loads(line)
            now = time.time()
            if msg.get("type") == "frame":
                payload, buf = rec.read_exact(sock, msg["n"], buf)
                writer.frame(msg, payload, now)
                last_frame = now
                if sweep_done():
                    complete, why = True, f"{sweep} frames from every camera"
                    break
            elif msg.get("type") == "status":
                writer.status(msg, now)
            if not quiet and now - last_report >= 1.0:
                last_report = now
                parts = []
                for ns in cams or sorted(writer.counts):
                    parts.append(f"{ns} d:{writer.count(ns, 'depth')} c:{writer.count(ns, 'rgb')}")
                log(f"  {now - t0:5.1f} s  " + "  ".join(parts) + f"  {rec.human_bytes(writer.bytes)}")
        if stop["flag"]:
            why = why or stop["why"]
            complete = sweep_done() if sweep else True
    finally:
        for s, h in old:
            signal.signal(s, h)
        try:
            sock.close()
        except OSError:
            pass
        meta = writer.close(complete=complete, stopped=why or "stopped")
    total = sum(sum(v.values()) for v in writer.counts.values())
    log(f"{'complete' if complete else 'INCOMPLETE'}: {total} frames, {rec.human_bytes(writer.bytes)}, "
        f"{meta['duration_s']:.1f} s ({why or 'stopped'})")
    return path, complete, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description="record an xtion_head stream to disk")
    ap.add_argument("head", help="HOST[:PORT] of the running xtion_head (e.g. 127.0.0.1)")
    ap.add_argument("--out", required=True, help="directory that holds the recordings")
    ap.add_argument("--label", default="", help="short name appended to the recording folder")
    ap.add_argument("--name", default=None, help="exact folder name (default: timestamp[_label])")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sweep", type=int, default=0, help="stop after N depth frames per camera")
    g.add_argument("--seconds", type=float, default=0.0, help="stop after S seconds")
    ap.add_argument("--fps", type=float, default=15.0, help="frames per stream to request")
    ap.add_argument("--no-color", action="store_true", help="depth only")
    ap.add_argument("--max-mb", type=float, default=8000.0, help="stop at this many MB")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="give up after this many seconds without a frame (0 = never)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    host, port = rec.split_host(a.head)
    os.makedirs(a.out, exist_ok=True)
    try:
        path, complete, _ = record(host, port, a.out, label=a.label, sweep=a.sweep,
                                   seconds=a.seconds, fps=a.fps, color=not a.no_color,
                                   max_mb=a.max_mb, timeout=a.timeout, name=a.name,
                                   quiet=a.quiet, log=lambda *x: print(*x, file=sys.stderr, flush=True))
    except (OSError, ConnectionError) as e:
        print(f"head_record: cannot reach the head at {host}:{port}: {e}", file=sys.stderr)
        return 2
    print(f"RECORDING {path}", flush=True)
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
