#!/usr/bin/env python3
"""Recording format + replay + recorder round trip, with no camera.

Run:  ./.venv/bin/python -m unittest tests.test_head_recording -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sensor_head"))

import head_recording as rec  # noqa: E402
import head_replay  # noqa: E402
import head_record  # noqa: E402

W, H = 64, 48


def fake_hello(cams=("camera1", "camera2")):
    return {"proto": 1, "type": "hello", "host": "fake",
            "cameras": [{"ns": ns, "port": f"1-{i + 7}", "uri": f"1d27/0601@1/{i + 4}",
                         "serial": "", "width": W, "height": H, "fps": 15,
                         "fx": 60.0, "fy": 60.0, "cx": (W - 1) / 2, "cy": (H - 1) / 2,
                         "color": True, "state": "streaming"}
                        for i, ns in enumerate(cams)]}


def depth_frame(i):
    d = (np.arange(W * H, dtype=np.uint16).reshape(H, W) % 3000) + 500 + i
    d[0, 0] = 0
    return d


def rgb_frame(i):
    rng = np.random.default_rng(i)
    return rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8)


def write_fake_recording(path, frames=4, cams=("camera1", "camera2"), mode="sweep"):
    hello = fake_hello(cams)
    w = rec.Writer(path, hello, {"fps": 15, "color": True}, mode=mode, label="unit")
    t = w.t0
    for i in range(frames):
        for ns in cams:
            t += 0.01
            d = depth_frame(i)
            w.frame({"type": "frame", "ns": ns, "kind": "depth", "stamp": t, "w": W, "h": H,
                     "enc": "16UC1", "n": d.nbytes}, d.tobytes(), now=t)
            c = rgb_frame(i)
            w.frame({"type": "frame", "ns": ns, "kind": "rgb", "stamp": t, "w": W, "h": H,
                     "enc": "rgb8", "n": c.nbytes}, c.tobytes(), now=t)
        w.status({"type": "status", "stamp": t, "cameras": {}}, now=t)
    return w.close(complete=True)


class FormatTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r")
            meta = write_fake_recording(path, frames=3)
            self.assertTrue(rec.is_recording(path))
            self.assertEqual(meta["frames"]["camera1"], {"depth": 3, "rgb": 3})
            self.assertTrue(meta["complete"])
            r = rec.Reader(path)
            self.assertEqual(r.cameras, ["camera1", "camera2"])
            depth = [(h, p) for h, p in r.records(kinds=("depth",), cameras=("camera2",))]
            self.assertEqual(len(depth), 3)
            for i, (h, p) in enumerate(depth):
                self.assertEqual(h["comp"], "zlib")
                self.assertEqual(h["raw"], W * H * 2)
                np.testing.assert_array_equal(np.frombuffer(p, np.uint16).reshape(H, W),
                                              depth_frame(i))
            rgb = [(h, p) for h, p in r.records(kinds=("rgb",), cameras=("camera1",))]
            self.assertEqual(len(rgb), 3)
            h, p = rgb[0]
            self.assertEqual(len(p), W * H * 3)
            got = np.frombuffer(p, np.uint8).reshape(H, W, 3).astype(int)
            want = rgb_frame(0).astype(int)
            if h["comp"] == "jpeg":
                # random noise is the worst case for JPEG; just require the right shape + range
                self.assertLess(abs(got.mean() - want.mean()), 40)
            else:
                np.testing.assert_array_equal(got, want)
            statuses = [h for h, p in r.records() if p is None]
            self.assertEqual(len(statuses), 3)
            self.assertIn("t", statuses[0])

    def test_truncated_tail_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r")
            write_fake_recording(path, frames=2, cams=("camera1",))
            fp = os.path.join(path, rec.FRAMES)
            with open(fp, "r+b") as fh:
                fh.truncate(os.path.getsize(fp) - 100)
            n = sum(1 for h, p in rec.Reader(path).records() if p is not None)
            self.assertEqual(n, 3)   # 4 frames written, the last one cut short

    def test_depth_to_points(self):
        d = np.zeros((H, W), np.uint16)
        d[10, 20] = 2000                 # 2 m
        d[5, 5] = 100                    # too close: dropped
        xyz, col = rec.depth_to_points(d, None, fx=60.0, fy=60.0, cx=31.5, cy=23.5)
        self.assertEqual(xyz.shape, (1, 3))
        self.assertAlmostEqual(xyz[0, 2], 2.0, places=5)
        self.assertAlmostEqual(xyz[0, 0], (20 - 31.5) * 2.0 / 60.0, places=5)
        self.assertAlmostEqual(xyz[0, 1], (10 - 23.5) * 2.0 / 60.0, places=5)
        self.assertEqual(col.shape, (1, 3))

    def test_write_ply(self):
        with tempfile.TemporaryDirectory() as tmp:
            xyz = np.array([[0, 0, 1], [1, 2, 3]], np.float32)
            rgb = np.array([[1, 2, 3], [4, 5, 6]], np.uint8)
            p = os.path.join(tmp, "a.ply")
            self.assertEqual(rec.write_ply(p, xyz, rgb), 2)
            with open(p, "rb") as fh:
                head = fh.read(200)
            self.assertIn(b"element vertex 2", head)


class ReplayAndRecordTests(unittest.TestCase):
    def test_replay_speaks_the_head_protocol_and_record_captures_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            write_fake_recording(src, frames=3, mode="sweep")
            rp = head_replay.Replay(rec.Reader(src), port=0, bind="127.0.0.1",
                                    speed=20.0, loop=True).start()
            try:
                # 1. a plain client sees a hello and frames of the right shape
                sock, hello, buf = rec.connect("127.0.0.1", rp.port, fps=30, color=True)
                sock.settimeout(5)
                self.assertEqual(hello["type"], "hello")
                self.assertTrue(hello["replay"])
                self.assertEqual([c["ns"] for c in hello["cameras"]], ["camera1", "camera2"])
                seen, status = {}, None
                deadline = time.time() + 5
                while time.time() < deadline and (len(seen) < 4 or status is None):
                    line, buf = rec.read_line(sock, buf)
                    msg = json.loads(line)
                    if msg["type"] == "status":
                        status = msg
                        continue
                    payload, buf = rec.read_exact(sock, msg["n"], buf)
                    self.assertEqual(len(payload), msg["n"])
                    if msg["kind"] == "depth":
                        self.assertEqual(msg["n"], W * H * 2)
                    else:
                        self.assertEqual(msg["n"], W * H * 3)
                    seen[(msg["ns"], msg["kind"])] = seen.get((msg["ns"], msg["kind"]), 0) + 1
                sock.close()
                self.assertEqual(len(seen), 4, seen)
                self.assertIn("camera1", status["cameras"])
                self.assertEqual(status["cameras"]["camera1"]["state"], "streaming")

                # 2. head_record against the replay produces a complete sweep recording
                out = os.path.join(tmp, "out")
                path, complete, meta = head_record.record(
                    "127.0.0.1", rp.port, out, label="re-record", sweep=2, fps=30,
                    color=True, timeout=10, quiet=True, log=lambda *a: None)
                self.assertTrue(complete, meta)
                self.assertTrue(path.endswith("_re-record"))
                self.assertEqual(meta["mode"], "sweep")
                for ns in ("camera1", "camera2"):
                    self.assertGreaterEqual(meta["frames"][ns]["depth"], 2)
                    self.assertGreaterEqual(meta["frames"][ns]["rgb"], 2)
                r2 = rec.Reader(path)
                d = next(p for h, p in r2.records(kinds=("depth",)))
                self.assertEqual(len(d), W * H * 2)
            finally:
                rp.stop()

    def test_once_mode_reports_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            write_fake_recording(src, frames=1, cams=("camera1",), mode="continuous")
            rp = head_replay.Replay(rec.Reader(src), port=0, speed=50.0).start()
            try:
                self.assertFalse(rp.loop)           # continuous -> once by default
                deadline = time.time() + 5
                while rp.state != "ended" and time.time() < deadline:
                    time.sleep(0.05)
                self.assertEqual(rp.state, "ended")
                self.assertEqual(rp.status()["cameras"]["camera1"]["state"], "ended")
            finally:
                rp.stop()


if __name__ == "__main__":
    unittest.main()
