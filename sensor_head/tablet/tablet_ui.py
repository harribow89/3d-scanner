#!/usr/bin/env python3
"""tablet_ui — the capture app that runs ON the tablet (inside its Linux
chroot), as a touch web page you open in the tablet's browser and pin to the
home screen.

    tablet_ui.py [--config /etc/xtion-tablet.json] [--listen 127.0.0.1:8080]

What it does: starts/stops the cameras (xtion_head.py), shows each camera's
state, frame rate and a live depth thumbnail, captures a sweep (N stacked
frames per camera) or a continuous recording (head_record.py), lists the
recordings on the tablet, and sends each one to the PC's receiver
(sensor_head/pc_receiver.py) over Tailscale or Wi-Fi.

Config (JSON, every key optional):
    recordings     folder for recordings (default /sdcard/xtion-scans, so the
                   Android Files app and `adb pull` see them too)
    head           HOST:PORT of the head (default 127.0.0.1:5599)
    head_cmd       argv that starts the head (default: xtion_head.py next to
                   this file, 320x240 at 15 fps, one camera expected)
    head_autostart start the cameras when the UI starts (default true)
    sweep_frames   default frames per camera for a sweep (12)
    record_fps     frames per second to record (15)
    color          record colour too (true)
    pc_url         http://<pc tailscale ip>:5600  (the receiver)
    token          shared secret for the receiver
    listen         address:port for this page (127.0.0.1:8080)

Standard library only (plus numpy/Pillow for the depth thumbnails).
"""
import argparse
import collections
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SENSOR_HEAD = os.path.dirname(HERE)
sys.path.insert(0, SENSOR_HEAD)
import head_recording as rec  # noqa: E402

try:
    import numpy as np
    from PIL import Image
    HAVE_PREVIEW = True
except ImportError:  # pragma: no cover
    HAVE_PREVIEW = False

DEFAULT_CONFIG = "/etc/xtion-tablet.json"
DEFAULTS = {
    "recordings": "/sdcard/xtion-scans",
    "listen": "127.0.0.1:8080",
    "head": "127.0.0.1:5599",
    "head_cmd": [sys.executable, os.path.join(SENSOR_HEAD, "xtion_head.py"),
                 "--expect", "1", "--settle", "8", "--width", "320", "--height", "240",
                 "--fps", "15", "--config", "/etc/xtion-head.json"],
    "head_autostart": True,
    "sweep_frames": 12,
    "record_fps": 15,
    "color": True,
    "pc_url": "",
    "token": "",
}
SENT_MARK = ".sent.json"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        with open(path) as fh:
            cfg.update(json.load(fh))
    return cfg


def safe_name(name):
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    if not name or name in (".", "..") or name.startswith("."):
        return None
    return name


# ------------------------------------------------------------------ head process
class HeadProcess:
    def __init__(self, cmd, logdir):
        self.cmd = cmd
        self.proc = None
        self.lines = collections.deque(maxlen=120)
        self.logdir = logdir
        self.started = 0.0
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if self.running():
                return False
            os.makedirs(self.logdir, exist_ok=True)
            self.lines.clear()
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True)
            self.started = time.time()
            threading.Thread(target=self._pump, daemon=True).start()
            log("head started:", " ".join(self.cmd))
            return True

    def _pump(self):
        proc = self.proc
        path = os.path.join(self.logdir, "head.log")
        with open(path, "a") as fh:
            for line in proc.stdout:
                line = line.rstrip("\n")
                self.lines.append(line)
                fh.write(line + "\n")
                fh.flush()
        rc = proc.wait()
        self.lines.append(f"[head exited with code {rc}]")
        log(f"head exited with code {rc}")

    def stop(self):
        with self.lock:
            if not self.running():
                return False
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    self.proc.kill()
                self.proc.wait(timeout=3)
            log("head stopped")
            return True

    def state(self):
        return {"running": self.running(), "pid": self.proc.pid if self.running() else None,
                "uptime_s": round(time.time() - self.started) if self.running() else 0,
                "log": list(self.lines)[-12:]}


# ------------------------------------------------------------------ head monitor
class HeadMonitor(threading.Thread):
    """Keeps a low-rate (1 fps, depth only) connection to the head for status
    and thumbnails. Never back-pressures the cameras."""

    def __init__(self, host, port):
        super().__init__(daemon=True, name="monitor")
        self.host, self.port = host, port
        self.hello = None
        self.status = None
        self.depth = {}          # ns -> (w, h, bytes)
        self.last_seen = 0.0
        self.error = "not connected"
        self.lock = threading.Lock()
        self._stop = False

    def run(self):
        buf = b""
        while not self._stop:
            sock = None
            try:
                sock, hello, buf = rec.connect(self.host, self.port, fps=1.0, color=False, timeout=5)
                sock.settimeout(5.0)
                with self.lock:
                    self.hello, self.error = hello, ""
                while not self._stop:
                    try:
                        line, buf = rec.read_line(sock, buf)
                    except socket.timeout:
                        with self.lock:
                            self.error = "head silent"
                        continue
                    msg = json.loads(line)
                    if msg.get("type") == "frame":
                        payload, buf = rec.read_exact(sock, msg["n"], buf)
                        if msg["kind"] == "depth":
                            with self.lock:
                                self.depth[msg["ns"]] = (msg["w"], msg["h"], payload)
                                self.last_seen = time.time()
                    elif msg.get("type") == "status":
                        with self.lock:
                            self.status, self.error = msg, ""
                            self.last_seen = time.time()
                    elif msg.get("type") == "hello":
                        with self.lock:
                            self.hello = msg
            except (OSError, ConnectionError, ValueError) as e:
                with self.lock:
                    self.error = f"no head: {e}"
                    self.status = None
                    self.depth = {}
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            for _ in range(20):
                if self._stop:
                    break
                time.sleep(0.1)

    def snapshot(self):
        with self.lock:
            cams = []
            names = [c["ns"] for c in (self.hello or {}).get("cameras", [])]
            st = (self.status or {}).get("cameras", {})
            for ns in names + [n for n in st if n not in names]:
                s = st.get(ns, {})
                info = next((c for c in (self.hello or {}).get("cameras", []) if c["ns"] == ns), {})
                cams.append({"ns": ns, "port": info.get("port"), "state": s.get("state", info.get("state", "?")),
                             "fps_depth": s.get("fps_depth", 0), "fps_rgb": s.get("fps_rgb", 0),
                             "restarts": s.get("restarts", 0), "last_error": s.get("last_error", ""),
                             "width": s.get("width", info.get("width")), "height": s.get("height", info.get("height")),
                             "preview": ns in self.depth})
            return {"connected": bool(self.status) and time.time() - self.last_seen < 5,
                    "error": self.error, "host": (self.hello or {}).get("host"), "cameras": cams}

    def preview_png(self, ns):
        with self.lock:
            d = self.depth.get(ns)
        if not d or not HAVE_PREVIEW:
            return None
        w, h, payload = d
        depth = np.frombuffer(payload, np.uint16).reshape(h, w)
        img = (np.clip(depth / 4000.0, 0, 1) * 255).astype(np.uint8)
        img[depth == 0] = 0
        bio = io.BytesIO()
        Image.fromarray(img).save(bio, format="PNG")
        return bio.getvalue()


# ------------------------------------------------------------------ capture jobs
class Job:
    """One head_record run at a time (a sweep or a continuous recording)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.kind = None
        self.label = ""
        self.started = 0.0
        self.lines = collections.deque(maxlen=60)
        self.result = None
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self, kind, label, extra):
        with self.lock:
            if self.running():
                return False, "a capture is already running"
            host, port = rec.split_host(self.cfg["head"])
            cmd = [sys.executable, os.path.join(SENSOR_HEAD, "head_record.py"), f"{host}:{port}",
                   "--out", self.cfg["recordings"], "--label", label or kind,
                   "--fps", str(self.cfg.get("record_fps", 15)), "--timeout", "45"] + extra
            if not self.cfg.get("color", True):
                cmd.append("--no-color")
            self.lines.clear()
            self.result = None
            self.kind, self.label, self.started = kind, label, time.time()
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1, start_new_session=True)
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            log(f"{kind} started:", " ".join(cmd))
            return True, "started"

    def start_sweep(self, frames, label):
        return self._start("sweep", label, ["--sweep", str(int(frames))])

    def start_record(self, label, seconds=0):
        extra = ["--seconds", str(float(seconds))] if seconds else []
        return self._start("record", label, extra)

    def _pump(self, proc):
        for line in proc.stderr:
            self.lines.append(line.rstrip("\n"))
        out = proc.stdout.read()
        rc = proc.wait()
        path = None
        for line in out.splitlines():
            if line.startswith("RECORDING "):
                path = line[len("RECORDING "):].strip()
        with self.lock:
            self.result = {"kind": self.kind, "label": self.label, "ok": rc == 0, "code": rc,
                           "path": path, "name": os.path.basename(path) if path else None,
                           "finished": time.time(), "seconds": round(time.time() - self.started, 1)}
        log(f"{self.kind} finished code {rc}: {path}")

    def stop(self):
        with self.lock:
            if not self.running():
                return False
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            return True

    def state(self):
        return {"running": self.running(), "kind": self.kind if self.running() else None,
                "label": self.label if self.running() else "",
                "elapsed_s": round(time.time() - self.started, 1) if self.running() else 0,
                "log": list(self.lines)[-6:], "last": self.result}


# ------------------------------------------------------------------ sending to the PC
class Sender:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs = {}           # name -> dict(state)
        self.lock = threading.Lock()

    def state(self):
        with self.lock:
            return {k: dict(v) for k, v in self.jobs.items()}

    def pc_ping(self, timeout=6):
        url = (self.cfg.get("pc_url") or "").rstrip("/")
        if not url:
            return {"ok": False, "error": "no pc_url configured"}
        req = urllib.request.Request(url + "/api/ping", headers={"X-Token": self.cfg.get("token", "")})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": str(getattr(e, "reason", e))}

    def send(self, name):
        path = os.path.join(self.cfg["recordings"], name)
        if not rec.is_recording(path):
            return False, "not a recording"
        with self.lock:
            j = self.jobs.get(name)
            if j and j.get("running"):
                return False, "already sending"
            self.jobs[name] = {"running": True, "sent": 0, "total": 0, "error": "", "started": time.time()}
        threading.Thread(target=self._send, args=(name, path), daemon=True).start()
        return True, "sending"

    def _send(self, name, path):
        url = (self.cfg.get("pc_url") or "").rstrip("/")
        tmpdir = os.path.join(self.cfg["recordings"], ".tmp")
        os.makedirs(tmpdir, exist_ok=True)
        tmp = os.path.join(tmpdir, name + ".tar")
        try:
            if not url:
                raise RuntimeError("no pc_url configured")
            with tarfile.open(tmp, "w") as tar:
                for fn in (rec.META, rec.FRAMES):
                    tar.add(os.path.join(path, fn), arcname=f"{name}/{fn}")
            total = os.path.getsize(tmp)
            with self.lock:
                self.jobs[name]["total"] = total
            sender = self

            class Body:
                def __init__(self, fh):
                    self.fh, self.sent = fh, 0

                def read(self, n=-1):
                    chunk = self.fh.read(n if n and n > 0 else 1 << 20)
                    self.sent += len(chunk)
                    with sender.lock:
                        sender.jobs[name]["sent"] = self.sent
                    return chunk

                def __len__(self):
                    return total

            with open(tmp, "rb") as fh:
                req = urllib.request.Request(
                    f"{url}/api/upload?name={name}&replace=1", data=Body(fh), method="POST",
                    headers={"X-Token": self.cfg.get("token", ""), "Content-Type": "application/x-tar",
                             "Content-Length": str(total)})
                with urllib.request.urlopen(req, timeout=1800) as r:
                    resp = json.loads(r.read().decode())
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "receiver refused"))
            with open(os.path.join(path, SENT_MARK), "w") as fh:
                json.dump({"sent": time.time(), "to": url, "path": resp.get("path")}, fh)
            with self.lock:
                self.jobs[name].update(running=False, error="", finished=time.time())
            log(f"sent {name} ({rec.human_bytes(total)}) -> {resp.get('path')}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get("error", "")
            except Exception:  # noqa: BLE001
                detail = ""
            with self.lock:
                self.jobs[name].update(running=False, error=f"HTTP {e.code} {detail}".strip())
            log(f"send {name} failed: HTTP {e.code} {detail}")
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.jobs[name].update(running=False, error=str(getattr(e, "reason", e))[:200])
            log(f"send {name} failed: {e}")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------ the app
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg["recordings"], exist_ok=True)
        self.head = HeadProcess(cfg["head_cmd"], os.path.join(cfg["recordings"], ".logs"))
        host, port = rec.split_host(cfg["head"])
        self.monitor = HeadMonitor(host, port)
        self.monitor.start()
        self.job = Job(cfg)
        self.sender = Sender(cfg)
        self.pc = {"ok": None, "checked": 0.0, "error": ""}
        if cfg.get("head_autostart", True):
            self.head.start()
        threading.Thread(target=self._pc_loop, daemon=True).start()

    def _pc_loop(self):
        while True:
            r = self.sender.pc_ping()
            self.pc = {"ok": bool(r.get("ok")), "checked": time.time(),
                       "error": "" if r.get("ok") else r.get("error", ""), "host": r.get("host", "")}
            time.sleep(15 if r.get("ok") else 10)

    def recordings(self):
        out = []
        for d in rec.list_recordings(self.cfg["recordings"]):
            try:
                s = rec.Reader(d).summary()
            except (OSError, ValueError, KeyError):
                continue
            s["name"] = os.path.basename(d)
            s["sent"] = os.path.isfile(os.path.join(d, SENT_MARK))
            s["total_frames"] = sum(sum(v.values()) for v in (s["frames"] or {}).values())
            out.append(s)
        out.sort(key=lambda s: s["name"], reverse=True)
        return out

    def delete(self, name):
        name = safe_name(name)
        path = os.path.join(self.cfg["recordings"], name or "")
        if not name or not rec.is_recording(path):
            return False, "no such recording"
        shutil.rmtree(path)
        log(f"deleted {name}")
        return True, "deleted"

    def state(self):
        try:
            du = shutil.disk_usage(self.cfg["recordings"])
            free = du.free
        except OSError:
            free = 0
        return {"time": time.time(), "head": self.head.state(), "cameras": self.monitor.snapshot(),
                "job": self.job.state(), "recordings": self.recordings(),
                "sending": self.sender.state(), "pc": dict(self.pc, url=self.cfg.get("pc_url", "")),
                "disk_free": free, "disk_free_h": rec.human_bytes(free),
                "defaults": {"sweep_frames": self.cfg.get("sweep_frames", 12)},
                "hostname": socket.gethostname()}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes"><meta name="theme-color" content="#0f141a">
<title>Xtion capture</title>
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f141a;--card:#161d26;--line:#263040;--fg:#e6edf3;--dim:#8b98a5;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#2f81f7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,-apple-system,sans-serif;padding:12px 12px 40px}
h1{font-size:18px;margin:4px 0 12px;display:flex;justify-content:space-between;align-items:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.card h2{font-size:14px;color:var(--dim);margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:0 18px;min-height:56px;color:#fff;background:var(--acc);cursor:pointer}
button.big{width:100%;font-size:19px;min-height:68px}button.sec{background:#2a3444}button.bad{background:var(--bad)}button.ok{background:var(--ok)}
button:disabled{opacity:.45}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{flex:1}
input{font:inherit;min-height:56px;border-radius:10px;border:1px solid var(--line);background:#0f141a;color:var(--fg);padding:0 12px;width:100%}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px;background:var(--dim)}.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.cams{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}.cam{background:#0f141a;border:1px solid var(--line);border-radius:10px;padding:8px;text-align:center}
.cam img{width:100%;aspect-ratio:4/3;background:#000;border-radius:6px;object-fit:cover}.cam .n{font-weight:600;margin:6px 0 2px}.cam .s{color:var(--dim);font-size:13px}
.log{font:12px ui-monospace,monospace;color:var(--dim);white-space:pre-wrap;max-height:120px;overflow:auto;margin-top:8px}
.rec{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.rec:first-child{border-top:0}
.rec .info{flex:1;min-width:0}.rec .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rec .meta{color:var(--dim);font-size:13px}
.rec button{min-height:48px;padding:0 14px;flex:none}.dim{color:var(--dim)}.bar{height:6px;background:#0f141a;border-radius:3px;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;background:var(--acc)}
.status{font-size:15px;margin-top:8px}
</style></head><body>
<h1><span>Xtion capture <span class="dim" id="host"></span></span><span class="dim" id="disk"></span></h1>

<div class="card"><h2>Cameras</h2>
 <div class="status"><span class="dot" id="camdot"></span><span id="camtext">…</span></div>
 <div class="cams" id="cams"></div>
 <div class="row" style="margin-top:10px"><button class="sec" id="headbtn" onclick="toggleHead()">Start cameras</button></div>
 <div class="log" id="headlog"></div>
</div>

<div class="card"><h2>Capture</h2>
 <div class="row"><input id="label" placeholder="label (room, corner…)"><input id="frames" type="number" min="1" max="120" inputmode="numeric" style="max-width:120px"></div>
 <div class="row" style="margin-top:10px"><button class="big" id="sweepbtn" onclick="sweep()">Capture sweep</button></div>
 <div class="row" style="margin-top:10px"><button class="big sec" id="recbtn" onclick="toggleRecord()">Start recording</button></div>
 <div class="status" id="jobtext"></div><div class="log" id="joblog"></div>
</div>

<div class="card"><h2>Recordings on this tablet</h2>
 <div class="status"><span class="dot" id="pcdot"></span><span id="pctext">PC…</span> <button class="sec" style="min-height:40px;padding:0 10px;margin-left:8px" onclick="post('/api/pc/ping')">check</button></div>
 <div id="recs" style="margin-top:8px"></div>
</div>

<script>
let S=null, busy=false, confirmDel=null, framesTouched=false;
const $=id=>document.getElementById(id);
async function post(url,body){busy=true;try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const j=await r.json();if(!j.ok&&j.error)toast(j.error);}catch(e){toast(String(e))}busy=false;refresh();}
function toast(t){$('jobtext').textContent=t;}
function toggleHead(){post(S&&S.head.running?'/api/head/stop':'/api/head/start');}
function sweep(){post('/api/sweep',{frames:parseInt($('frames').value||S.defaults.sweep_frames),label:$('label').value});}
function toggleRecord(){if(S&&S.job.running)post('/api/record/stop');else post('/api/record/start',{label:$('label').value});}
function send(n){post('/api/send',{name:n});}
function del(n){if(confirmDel===n){confirmDel=null;post('/api/delete',{name:n});}else{confirmDel=n;render();setTimeout(()=>{if(confirmDel===n){confirmDel=null;render();}},4000);}}
function fmtB(b){return b<1048576?(b/1024).toFixed(0)+' KB':b<1073741824?(b/1048576).toFixed(1)+' MB':(b/1073741824).toFixed(2)+' GB';}
function render(){if(!S)return;
 $('host').textContent=S.hostname?('· '+S.hostname):''; $('disk').textContent=S.disk_free_h+' free';
 const c=S.cameras, streaming=c.cameras.filter(x=>x.state==='streaming').length;
 const ext=c.connected&&!S.head.running;
 $('camdot').className='dot '+(streaming?'ok':(S.head.running?'warn':'bad'));
 $('camtext').textContent=c.connected?(streaming+' of '+c.cameras.length+' camera(s) streaming'+(ext?' (head started elsewhere)':'')):(S.head.running?'starting… '+(c.error||''):'cameras off');
 $('headbtn').textContent=S.head.running?'Stop cameras':'Start cameras'; $('headbtn').className=S.head.running?'bad':'ok'; $('headbtn').disabled=ext;
 $('headlog').textContent=(S.head.log||[]).slice(-4).join('\n');
 $('cams').innerHTML=c.cameras.map(x=>'<div class="cam">'+(x.preview?'<img src="/api/preview/'+x.ns+'.png?t='+Math.floor(S.time)+'">':'<div style="aspect-ratio:4/3;background:#000;border-radius:6px"></div>')+'<div class="n">'+x.ns+'</div><div class="s">'+x.state+(x.state==='streaming'?' · '+x.fps_depth+' fps':'')+(x.port?' · USB '+x.port:'')+'</div>'+(x.last_error&&x.state!=='streaming'?'<div class="s" style="color:var(--bad)">'+x.last_error+'</div>':'')+'</div>').join('')||'<div class="dim">no cameras found yet — plug an Xtion into the USB-C port</div>';
 if(!framesTouched&&document.activeElement!==$('frames'))$('frames').value=S.defaults.sweep_frames;
 const j=S.job, canCap=streaming>0&&!j.running;
 $('sweepbtn').disabled=!canCap; $('recbtn').disabled=!(canCap||j.running&&j.kind==='record');
 $('recbtn').textContent=j.running&&j.kind==='record'?'Stop recording ('+j.elapsed_s+' s)':'Start recording'; $('recbtn').className='big '+(j.running&&j.kind==='record'?'bad':'sec');
 $('sweepbtn').textContent=j.running&&j.kind==='sweep'?'Capturing sweep… '+j.elapsed_s+' s':'Capture sweep';
 if(j.running)$('jobtext').textContent=(j.kind==='sweep'?'Hold still…':'Recording… move slowly')+(j.label?' ('+j.label+')':'');
 else if(j.last)$('jobtext').textContent=(j.last.ok?'✓ ':'⚠ ')+j.last.kind+' '+(j.last.name||'')+(j.last.ok?' saved':' stopped short (code '+j.last.code+')');
 $('joblog').textContent=(j.log||[]).slice(-3).join('\n');
 $('pcdot').className='dot '+(S.pc.ok===true?'ok':S.pc.ok===false?'bad':''); $('pctext').textContent=S.pc.url?('PC '+(S.pc.ok?'reachable ('+(S.pc.host||'')+')':'unreachable: '+(S.pc.error||'')) ):'no PC configured (pc_url)';
 $('recs').innerHTML=S.recordings.map(r=>{const s=S.sending[r.name];const prog=s&&s.running?Math.round(100*s.sent/Math.max(s.total,1)):null;
  return '<div class="rec"><div class="info"><div class="name">'+(r.sent?'✓ ':'')+r.name+'</div><div class="meta">'+r.mode+' · '+r.cameras.length+' cam · '+r.total_frames+' frames · '+fmtB(r.bytes)+(r.complete?'':' · incomplete')+(s&&s.error?' · <span style="color:var(--bad)">'+s.error+'</span>':'')+'</div>'+(prog!==null?'<div class="bar"><i style="width:'+prog+'%"></i></div>':'')+'</div>'
  +'<button '+(prog!==null||!S.pc.url?'disabled':'')+' onclick="send(\''+r.name+'\')">'+(prog!==null?prog+'%':(r.sent?'Resend':'Send'))+'</button><button class="'+(confirmDel===r.name?'bad':'sec')+'" onclick="del(\''+r.name+'\')">'+(confirmDel===r.name?'Sure?':'✕')+'</button></div>';}).join('')||'<div class="dim">none yet</div>';
}
async function refresh(){if(busy)return;try{const r=await fetch('/api/state');S=await r.json();render();}catch(e){$('camtext').textContent='UI server not reachable';}}
$('frames').addEventListener('input',()=>framesTouched=true);
refresh();setInterval(refresh,1000);
</script></body></html>
"""


MANIFEST = json.dumps({"name": "Xtion capture", "short_name": "Xtion", "start_url": "/",
                       "display": "standalone", "background_color": "#0f141a",
                       "theme_color": "#0f141a",
                       "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#0f141a"/>'
        '<circle cx="32" cy="30" r="15" fill="none" stroke="#2f81f7" stroke-width="5"/>'
        '<circle cx="32" cy="30" r="5" fill="#3fb950"/>'
        '<rect x="14" y="50" width="36" height="6" rx="3" fill="#8b98a5"/></svg>')


class Handler(BaseHTTPRequestHandler):
    server_version = "xtion-tablet-ui/1"

    def log_message(self, fmt, *args):
        if "/api/state" in (args[0] if args else "") or "/api/preview" in (args[0] if args else ""):
            return
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return {}

    def do_GET(self):
        app = self.server.app
        p = urlparse(self.path).path
        if p == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if p == "/api/state":
            return self._json(200, app.state())
        if p == "/manifest.webmanifest":
            return self._send(200, MANIFEST.encode(), "application/manifest+json")
        if p in ("/icon.svg", "/favicon.ico"):
            return self._send(200, ICON.encode(), "image/svg+xml")
        if p == "/api/config":
            cfg = dict(app.cfg)
            cfg["token"] = "***" if cfg.get("token") else ""
            return self._json(200, cfg)
        m = re.fullmatch(r"/api/preview/([A-Za-z0-9_-]+)\.png", p)
        if m:
            png = app.monitor.preview_png(m.group(1))
            if png is None:
                return self._json(404, {"ok": False, "error": "no frame yet"})
            return self._send(200, png, "image/png")
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        app = self.server.app
        p = urlparse(self.path).path
        b = self._body()
        if p == "/api/head/start":
            return self._json(200, {"ok": True, "started": app.head.start()})
        if p == "/api/head/stop":
            if app.job.running():
                app.job.stop()
            return self._json(200, {"ok": True, "stopped": app.head.stop()})
        if p == "/api/sweep":
            frames = int(b.get("frames") or app.cfg.get("sweep_frames", 12))
            ok, msg = app.job.start_sweep(max(1, min(frames, 600)), str(b.get("label", ""))[:40])
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "error": "" if ok else msg})
        if p == "/api/record/start":
            ok, msg = app.job.start_record(str(b.get("label", ""))[:40], float(b.get("seconds") or 0))
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "error": "" if ok else msg})
        if p == "/api/record/stop":
            return self._json(200, {"ok": True, "stopped": app.job.stop()})
        if p == "/api/send":
            name = safe_name(b.get("name"))
            if not name:
                return self._json(400, {"ok": False, "error": "bad name"})
            ok, msg = app.sender.send(name)
            return self._json(200 if ok else 409, {"ok": ok, "message": msg, "error": "" if ok else msg})
        if p == "/api/delete":
            ok, msg = app.delete(b.get("name"))
            return self._json(200 if ok else 404, {"ok": ok, "message": msg, "error": "" if ok else msg})
        if p == "/api/pc/ping":
            r = app.sender.pc_ping()
            app.pc = {"ok": bool(r.get("ok")), "checked": time.time(),
                      "error": "" if r.get("ok") else r.get("error", ""), "host": r.get("host", "")}
            return self._json(200, {"ok": True, "pc": r})
        self._json(404, {"ok": False, "error": "not found"})


def serve(cfg):
    host, port = rec.split_host(cfg["listen"], 8080)
    app = App(cfg)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app
    return srv, app


def main(argv=None):
    ap = argparse.ArgumentParser(description="touch capture page for the tablet")
    ap.add_argument("--config", default=os.environ.get("XTION_TABLET_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--listen", default=None, help="override listen address:port")
    ap.add_argument("--recordings", default=None, help="override the recordings folder")
    ap.add_argument("--no-autostart", action="store_true", help="do not start the cameras at launch")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.listen:
        cfg["listen"] = a.listen
    if a.recordings:
        cfg["recordings"] = a.recordings
    if a.no_autostart:
        cfg["head_autostart"] = False
    srv, app = serve(cfg)
    host, port = srv.server_address[:2]
    log(f"tablet UI on http://{host}:{port}/  recordings in {cfg['recordings']}  "
        f"PC {cfg.get('pc_url') or '(not configured)'}")

    def shutdown(*_):
        log("shutting down")
        app.job.stop()
        app.head.stop()
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
