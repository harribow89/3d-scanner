#!/usr/bin/env python3
"""head_probe — check a running xtion_head: frame rates per camera/stream,
the head's own status, and (optionally) one depth PNG per camera.

    python3 head_probe.py HOST [--port 5599] [--seconds 20] [--fps 15] [--save DIR]

Exit 0 when every camera in the hello delivered depth frames at >= 50 % of
the requested rate, else 1. Needs only numpy (and Pillow for --save).
"""
import argparse
import json
import socket
import sys
import time

import numpy as np


def read_line(sock, buf):
    while b"\n" not in buf:
        chunk = sock.recv(1 << 16)
        if not chunk:
            raise ConnectionError("head closed the connection")
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return line, buf


def read_exact(sock, n, buf):
    while len(buf) < n:
        chunk = sock.recv(max(1 << 16, n - len(buf)))
        if not chunk:
            raise ConnectionError("head closed the connection")
        buf += chunk
    return buf[:n], buf[n:]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=5599)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--save", default=None, help="write <ns>_depth.png here")
    a = ap.parse_args(argv)

    sock = socket.create_connection((a.host, a.port), timeout=30)
    sock.sendall((json.dumps({"fps": a.fps, "color": True}) + "\n").encode())
    buf = b""
    line, buf = read_line(sock, buf)
    hello = json.loads(line)
    cams = [c["ns"] for c in hello["cameras"]]
    print(f"head {hello['host']}: {len(cams)} camera(s)")
    for c in hello["cameras"]:
        print(f"  {c['ns']}: USB port {c['port']}  {c['width']}x{c['height']}  "
              f"fx={c['fx']} state={c['state']}")
    counts, saved, status, nbytes = {}, set(), None, 0
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        line, buf = read_line(sock, buf)
        msg = json.loads(line)
        if msg["type"] == "status":
            status = msg
            continue
        payload, buf = read_exact(sock, msg["n"], buf)
        nbytes += msg["n"]
        key = (msg["ns"], msg["kind"])
        counts[key] = counts.get(key, 0) + 1
        if a.save and msg["kind"] == "rgb" and (msg["ns"], "rgb") not in saved:
            from PIL import Image
            Image.fromarray(np.frombuffer(payload, np.uint8).reshape(msg["h"], msg["w"], 3)) \
                .save(f"{a.save}/{msg['ns']}_rgb.png")
            saved.add((msg["ns"], "rgb"))
        if a.save and msg["kind"] == "depth" and msg["ns"] not in saved:
            from PIL import Image
            d = np.frombuffer(payload, np.uint16).reshape(msg["h"], msg["w"])
            img = (np.clip(d / 4000.0, 0, 1) * 255).astype(np.uint8)
            Image.fromarray(img).save(f"{a.save}/{msg['ns']}_depth.png")
            fill = (d > 0).mean() * 100
            print(f"  saved {msg['ns']}_depth.png (fill {fill:.0f} %, median "
                  f"{np.median(d[d > 0]) / 1000 if (d > 0).any() else 0:.2f} m)")
            saved.add(msg["ns"])
    dt = time.time() - t0
    ok = True
    print(f"\nover {dt:.0f} s ({nbytes / dt / 1e6:.1f} MB/s):")
    for ns in cams:
        d = counts.get((ns, "depth"), 0) / dt
        r = counts.get((ns, "rgb"), 0) / dt
        st = (status or {}).get("cameras", {}).get(ns, {})
        good = d >= 0.5 * a.fps
        ok &= good
        print(f"  {ns}: depth {d:5.1f} fps  rgb {r:5.1f} fps   head says {st.get('state')} "
              f"(camera {st.get('fps_depth')}/{st.get('fps_rgb')} fps, restarts {st.get('restarts')})"
              + ("" if good else "   <-- TOO SLOW"))
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
