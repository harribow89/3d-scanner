#!/usr/bin/env python3
"""pc_receiver — the PC end of "capture on the tablet, send it home".

    pc_receiver.py [--bind auto] [--port 5600] [--out output/recordings]
                   [--token TOKEN] [--on-receive 'cmd {path}']

The tablet UI (sensor_head/tablet/tablet_ui.py) POSTs each recording as a tar
stream to /api/upload; it lands in output/recordings/<name>/ and is ready for

    .venv/bin/python sensor_head/head_replay.py output/recordings/<name>
    .venv/bin/python station_scan.py import output/recordings/<name>

`--bind auto` (default) listens on this PC's Tailscale address when tailscale
is up (so the port is reachable only from your tailnet), else on every
interface. Uploads must carry the shared token (header X-Token). The token is
read from --token, $XTION_TOKEN, or output/recordings/.token, which is created
with a random value on first start; sensor_head/tablet/deploy_tablet.sh copies
it to the tablet. Standard library only.
"""
import argparse
import io
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_recording as rec  # noqa: E402

ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "output", "recordings")
DEFAULT_PORT = 5600
TOKEN_FILE = ".token"


def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                             timeout=5)
        ip = out.stdout.strip().splitlines()
        return ip[0].strip() if out.returncode == 0 and ip else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def load_token(out_dir, explicit=None):
    tok = explicit or os.environ.get("XTION_TOKEN")
    path = os.path.join(out_dir, TOKEN_FILE)
    if tok:
        return tok.strip(), path
    if os.path.isfile(path):
        with open(path) as fh:
            tok = fh.read().strip()
        if tok:
            return tok, path
    tok = secrets.token_urlsafe(18)
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(tok + "\n")
    os.chmod(path, 0o600)
    return tok, path


def safe_name(name):
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    if not name or name in (".", "..") or name.startswith("."):
        return None
    return "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in name)[:80]


class Limited(io.RawIOBase):
    """Reads at most n bytes from the request body."""

    def __init__(self, raw, n):
        self.raw, self.left = raw, n

    def readable(self):
        return True

    def readinto(self, b):
        if self.left <= 0:
            return 0
        chunk = self.raw.read(min(len(b), self.left))
        if not chunk:
            return 0
        self.left -= len(chunk)
        b[:len(chunk)] = chunk
        return len(chunk)


def extract_recording(stream, length, dest):
    """Safely extract a tar stream of one recording into dest (a fresh dir)."""
    os.makedirs(dest, exist_ok=False)
    n_files = 0
    try:
        with tarfile.open(fileobj=io.BufferedReader(Limited(stream, length)), mode="r|*") as tar:
            for m in tar:
                parts = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".")]
                if not parts or ".." in parts:
                    raise ValueError(f"unsafe path in upload: {m.name!r}")
                # the tablet sends '<name>/meta.json'; keep what is below the top dir
                rel = parts[1:] if len(parts) > 1 else parts
                if not rel:
                    continue
                target = os.path.join(dest, *rel)
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.isreg():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    src = tar.extractfile(m)
                    with open(target, "wb") as fh:
                        shutil.copyfileobj(src, fh, 1 << 20)
                    n_files += 1
                else:
                    raise ValueError(f"unsupported entry in upload: {m.name!r}")
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if not rec.is_recording(dest):
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError("upload did not contain meta.json + frames.bin")
    return n_files


def recordings(out_dir):
    out = []
    for d in rec.list_recordings(out_dir):
        try:
            r = rec.Reader(d)
            s = r.summary()
        except (OSError, ValueError, KeyError):
            continue
        s["name"] = os.path.basename(d)
        s["path"] = d
        s["received"] = os.path.getmtime(os.path.join(d, rec.META))
        out.append(s)
    out.sort(key=lambda s: s["received"], reverse=True)
    return out


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Scanner recordings</title>
<style>body{font:15px system-ui,sans-serif;margin:24px;background:#0f141a;color:#e6edf3}
table{border-collapse:collapse;width:100%%}td,th{padding:6px 10px;border-bottom:1px solid #2a333d;text-align:left}
code{background:#1c242e;padding:2px 5px;border-radius:4px}h1{font-size:20px}.dim{color:#8b98a5}</style>
<h1>Recordings received from the tablet</h1>
<p class=dim>Listening on %(addr)s &middot; folder <code>%(out)s</code></p>
<table><tr><th>name</th><th>mode</th><th>cameras</th><th>frames</th><th>size</th><th>received</th></tr>%(rows)s</table>
<p>Use one with:<br><code>.venv/bin/python sensor_head/head_replay.py output/recordings/NAME</code>
then <code>SCANNER_HEAD=127.0.0.1 ./run_scanner_docker.sh multi</code> (or <code>station capture</code>),<br>
or for a sweep: <code>.venv/bin/python station_scan.py import output/recordings/NAME</code></p>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "xtion-pc-receiver/1"

    def log_message(self, fmt, *args):  # quieter default log
        sys.stderr.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), self.server.token)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/ping":
            if not self._authed():
                return self._json(401, {"ok": False, "error": "bad token"})
            return self._json(200, {"ok": True, "host": socket.gethostname(),
                                    "recordings": len(recordings(self.server.out_dir))})
        if u.path == "/api/recordings":
            if not self._authed():
                return self._json(401, {"ok": False, "error": "bad token"})
            return self._json(200, {"ok": True, "recordings": recordings(self.server.out_dir)})
        if u.path == "/":
            rows = "".join(
                f"<tr><td>{s['name']}</td><td>{s['mode']}</td><td>{', '.join(s['cameras'])}</td>"
                f"<td>{sum(sum(v.values()) for v in (s['frames'] or {}).values())}</td>"
                f"<td>{rec.human_bytes(s['bytes'])}</td>"
                f"<td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['received']))}</td></tr>"
                for s in recordings(self.server.out_dir)) or "<tr><td colspan=6 class=dim>none yet</td></tr>"
            body = (PAGE % {"addr": f"{self.server.server_address[0]}:{self.server.server_address[1]}",
                            "out": self.server.out_dir, "rows": rows}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/upload":
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._authed():
            return self._json(401, {"ok": False, "error": "bad token"})
        q = parse_qs(u.query)
        name = safe_name((q.get("name") or [""])[0])
        if not name:
            return self._json(400, {"ok": False, "error": "missing ?name="})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(411, {"ok": False, "error": "Content-Length required"})
        dest = os.path.join(self.server.out_dir, name)
        replace = (q.get("replace") or ["0"])[0] == "1"
        if os.path.exists(dest):
            if not replace:
                return self._json(409, {"ok": False, "error": f"{name} already received",
                                        "path": dest})
            shutil.rmtree(dest)
        t0 = time.time()
        try:
            n = extract_recording(self.rfile, length, dest)
        except (ValueError, tarfile.TarError, OSError) as e:
            return self._json(400, {"ok": False, "error": str(e)})
        summary = rec.Reader(dest).summary()
        dt = time.time() - t0
        self.log_message("received %s: %d files, %s in %.1f s", name, n, rec.human_bytes(length), dt)
        hook = self.server.on_receive
        if hook:
            cmd = [a.replace("{path}", dest).replace("{name}", name) for a in shlex.split(hook)]
            try:
                subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                self.log_message("on-receive: %s", " ".join(cmd))
            except OSError as e:
                self.log_message("on-receive failed: %s", e)
        self._json(200, {"ok": True, "path": dest, "name": name, "files": n, "summary": summary,
                         "seconds": round(dt, 1)})


def serve(bind, port, out_dir, token, on_receive=None):
    os.makedirs(out_dir, exist_ok=True)
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    srv.out_dir, srv.token, srv.on_receive = out_dir, token, on_receive
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="receive recordings from the tablet")
    ap.add_argument("--bind", default="auto",
                    help="address to listen on: auto = the Tailscale IP, else 0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--token", default=None, help="shared secret (default: $XTION_TOKEN or output/recordings/.token)")
    ap.add_argument("--on-receive", default=None,
                    help="command to run after each upload; {path} and {name} are replaced")
    a = ap.parse_args(argv)
    bind = a.bind
    if bind == "auto":
        bind = tailscale_ip() or "0.0.0.0"
    token, tpath = load_token(a.out, a.token)
    srv = serve(bind, a.port, a.out, token, a.on_receive)
    print(f"pc_receiver: listening on http://{bind}:{a.port}/  (recordings -> {a.out})")
    print(f"pc_receiver: token in {tpath}; the tablet must send the same value")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
