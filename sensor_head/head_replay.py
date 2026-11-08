#!/usr/bin/env python3
"""head_replay — serve a recording as if it were a live xtion_head.

    head_replay.py RECORDING_DIR [--port 5599] [--bind 127.0.0.1]
                   [--speed 1.0] [--loop | --once]
    head_replay.py RECORDING_DIR --list        # what is in it, then exit

Speaks the head's wire protocol (see xtion_head.py), so everything on the PC
that works with a sensor head works with a recording, unchanged:

    SCANNER_HEAD=127.0.0.1 ./run_scanner_docker.sh multi        # live view
    SCANNER_HEAD=127.0.0.1 ./run_scanner_docker.sh station capture
    .venv/bin/python calibrate_head.py --head 127.0.0.1

Sweep recordings loop by default (a station capture needs a steady stream
while its container starts); continuous recordings play once and then idle,
reporting state "ended" in the status line. Clients may still ask for a lower
fps or no colour, as with the real head.
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_recording as rec  # noqa: E402


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Latest:
    """Latest frame per (ns, kind) + a condition for the senders (as in xtion_head)."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frames = {}
        self.gen = 0

    def put(self, ns, kind, header, payload):
        with self.cond:
            key = (ns, kind)
            seq = self.frames.get(key, (0,))[0] + 1
            header["seq"] = seq
            self.frames[key] = (seq, (json.dumps(header) + "\n").encode(), payload)
            self.gen += 1
            self.cond.notify_all()


class Client(threading.Thread):
    def __init__(self, sock, addr, replay):
        super().__init__(daemon=True)
        self.sock, self.addr, self.replay = sock, addr, replay
        self.alive = True
        self.color = True
        self.min_period = 0.0

    def _recv_controls(self):
        buf = b""
        try:
            while self.alive:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "fps" in msg:
                        self.min_period = 1.0 / max(float(msg["fps"]), 0.5)
                    if "color" in msg:
                        self.color = bool(msg["color"])
        except OSError:
            pass
        self.alive = False

    def run(self):
        log(f"client {self.addr[0]} connected")
        threading.Thread(target=self._recv_controls, daemon=True).start()
        latest = self.replay.latest
        sent_seq, last_sent, last_status = {}, {}, 0.0
        try:
            self.sock.sendall((json.dumps(self.replay.hello()) + "\n").encode())
            while self.alive and not self.replay.stopped:
                with latest.cond:
                    latest.cond.wait(timeout=0.5)
                    snapshot = dict(latest.frames)
                now = time.time()
                for key, (seq, hdr, payload) in snapshot.items():
                    if sent_seq.get(key) == seq:
                        continue
                    if key[1] == "rgb" and not self.color:
                        continue
                    if now - last_sent.get(key, 0.0) < self.min_period:
                        continue
                    self.sock.sendall(hdr)
                    self.sock.sendall(payload)
                    sent_seq[key] = seq
                    last_sent[key] = now
                if now - last_status > 1.0:
                    last_status = now
                    self.sock.sendall((json.dumps(self.replay.status()) + "\n").encode())
        except OSError:
            pass
        finally:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            log(f"client {self.addr[0]} disconnected")


class Replay:
    """Plays a recording into `latest` on a timer and serves clients."""

    def __init__(self, reader, port=rec.DEFAULT_PORT, bind="127.0.0.1", speed=1.0, loop=None):
        self.reader = reader
        self.port = port
        self.bind = bind
        self.speed = max(float(speed), 0.05)
        if loop is None:
            loop = reader.meta.get("mode") == "sweep"
        self.loop = loop
        self.latest = Latest()
        self.stopped = False
        self.state = "streaming"
        self.passes = 0
        self.played = 0
        self._stamps = {}
        self._srv = None
        self._threads = []

    # -- protocol messages
    def hello(self):
        cams = []
        for c in self.reader.hello.get("cameras", []):
            c = dict(c)
            c["state"] = self.state
            cams.append(c)
        name = os.path.basename(os.path.normpath(self.reader.dir))
        return {"proto": 1, "type": "hello", "host": f"replay:{name}", "replay": True,
                "cameras": cams}

    def status(self):
        now = time.time()
        cams = {}
        for c in self.reader.hello.get("cameras", []):
            ns = c["ns"]
            fps = {}
            for kind in ("depth", "rgb"):
                st = self._stamps.setdefault((ns, kind), [])
                st[:] = [t for t in st if now - t < 2.0]
                fps[kind] = round(len(st) / 2.0, 1)
            cams[ns] = {"state": self.state, "fps_depth": fps["depth"], "fps_rgb": fps["rgb"],
                        "restarts": 0, "last_error": "", "uri": c.get("uri"),
                        "fx": c.get("fx"), "fy": c.get("fy"), "cx": c.get("cx"), "cy": c.get("cy"),
                        "width": c.get("width"), "height": c.get("height")}
        return {"type": "status", "stamp": now, "replay": True, "pass": self.passes,
                "frames_played": self.played, "cameras": cams}

    # -- playback
    def _play(self):
        while not self.stopped:
            base = time.time()
            first_t = None
            for header, payload in self.reader.records(decode=True):
                if self.stopped:
                    return
                if payload is None:
                    continue
                t = header.get("t", 0.0)
                if first_t is None:
                    first_t = t
                due = base + (t - first_t) / self.speed
                delay = due - time.time()
                if delay > 0:
                    time.sleep(min(delay, 1.0))
                    if delay > 1.0:
                        continue_at = due
                        while time.time() < continue_at and not self.stopped:
                            time.sleep(min(0.2, continue_at - time.time()))
                now = time.time()
                wire = {"type": "frame", "ns": header["ns"], "kind": header["kind"],
                        "stamp": now, "w": header["w"], "h": header["h"],
                        "enc": header["enc"], "n": len(payload)}
                self.latest.put(header["ns"], header["kind"], wire, payload)
                self._stamps.setdefault((header["ns"], header["kind"]), []).append(now)
                self.played += 1
            self.passes += 1
            if not self.loop:
                self.state = "ended"
                log("recording played once; idling (status says 'ended'). --loop repeats it.")
                while not self.stopped:
                    time.sleep(0.5)
                return
            if self.passes == 1:
                log("recording finished; looping")

    def _serve(self):
        while not self.stopped:
            try:
                sock, addr = self._srv.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            Client(sock, addr, self).start()

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(4)
        self.port = srv.getsockname()[1]
        self._srv = srv
        for fn in (self._play, self._serve):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self.stopped = True
        with self.latest.cond:
            self.latest.cond.notify_all()
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass


def describe(reader):
    s = reader.summary()
    lines = [f"{reader.dir}", f"  mode {s['mode']}, {s['duration_s']:.1f} s, "
             f"{rec.human_bytes(s['bytes'])}, {'complete' if s['complete'] else 'INCOMPLETE'}"
             + (f", label '{s['label']}'" if s["label"] else "")]
    for c in reader.hello.get("cameras", []):
        n = (s["frames"] or {}).get(c["ns"], {})
        lines.append(f"  {c['ns']}: {c['width']}x{c['height']} fx={c['fx']}  "
                     f"depth {n.get('depth', 0)}  rgb {n.get('rgb', 0)}  (USB port {c.get('port')})")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="serve a recording over the xtion_head protocol")
    ap.add_argument("recording", help="recording directory (holds meta.json + frames.bin)")
    ap.add_argument("--port", type=int, default=rec.DEFAULT_PORT)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on (127.0.0.1; use 0.0.0.0 for the Docker host network)")
    ap.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--loop", dest="loop", action="store_true", default=None,
                   help="repeat forever (default for sweep recordings)")
    g.add_argument("--once", dest="loop", action="store_false",
                   help="play once, then idle (default for continuous recordings)")
    ap.add_argument("--list", action="store_true", help="describe the recording and exit")
    a = ap.parse_args(argv)
    if not rec.is_recording(a.recording):
        sys.exit(f"head_replay: {a.recording} is not a recording (no meta.json/frames.bin)")
    reader = rec.Reader(a.recording)
    print(describe(reader))
    if a.list:
        return 0
    rp = Replay(reader, port=a.port, bind=a.bind, speed=a.speed, loop=a.loop).start()
    log(f"serving on tcp/{rp.port} ({'looping' if rp.loop else 'once'}, x{rp.speed:g}); "
        f"point the scanner at SCANNER_HEAD={'127.0.0.1' if a.bind in ('127.0.0.1', '0.0.0.0') else a.bind}"
        + (f":{rp.port}" if rp.port != rec.DEFAULT_PORT else ""))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        rp.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
