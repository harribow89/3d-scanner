#!/usr/bin/env python3
"""pc_receiver: upload extraction is safe and round-trips a recording.

Run:  ./.venv/bin/python -m unittest tests.test_pc_receiver -v
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sensor_head"))
sys.path.insert(0, str(ROOT / "tests"))

import head_recording as rec  # noqa: E402
import pc_receiver  # noqa: E402
from test_head_recording import write_fake_recording  # noqa: E402


def tar_of(path, name):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as tar:
        for fn in (rec.META, rec.FRAMES):
            tar.add(os.path.join(path, fn), arcname=f"{name}/{fn}")
    return bio.getvalue()


class ExtractTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            write_fake_recording(src, frames=2)
            data = tar_of(src, "upload-1")
            dest = os.path.join(tmp, "dest")
            n = pc_receiver.extract_recording(io.BytesIO(data), len(data), dest)
            self.assertEqual(n, 2)
            self.assertTrue(rec.is_recording(dest))
            self.assertEqual(rec.Reader(dest).summary()["frames"]["camera1"], {"depth": 2, "rgb": 2})

    def test_rejects_path_traversal_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode="w") as tar:
                info = tarfile.TarInfo("x/../../evil.txt")
                info.size = 3
                tar.addfile(info, io.BytesIO(b"abc"))
            data = bio.getvalue()
            dest = os.path.join(tmp, "dest")
            with self.assertRaises(ValueError):
                pc_receiver.extract_recording(io.BytesIO(data), len(data), dest)
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(os.path.join(tmp, "evil.txt")))

    def test_rejects_non_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode="w") as tar:
                info = tarfile.TarInfo("r/readme.txt")
                info.size = 2
                tar.addfile(info, io.BytesIO(b"hi"))
            data = bio.getvalue()
            dest = os.path.join(tmp, "dest")
            with self.assertRaises(ValueError):
                pc_receiver.extract_recording(io.BytesIO(data), len(data), dest)
            self.assertFalse(os.path.exists(dest))

    def test_safe_name(self):
        self.assertEqual(pc_receiver.safe_name("../x"), "x")      # only the last component survives
        self.assertIsNone(pc_receiver.safe_name(".."))
        self.assertIsNone(pc_receiver.safe_name(".hidden"))
        self.assertEqual(pc_receiver.safe_name("a/b/20261108-1200_room one"), "20261108-1200_room-one")


class HttpTests(unittest.TestCase):
    def test_upload_requires_token_and_lands_in_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            write_fake_recording(src, frames=1, cams=("camera1",))
            out = os.path.join(tmp, "out")
            srv = pc_receiver.serve("127.0.0.1", 0, out, "secret")
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                data = tar_of(src, "s1")
                base = f"http://127.0.0.1:{port}"
                req = urllib.request.Request(f"{base}/api/upload?name=s1", data=data, method="POST",
                                             headers={"X-Token": "wrong", "Content-Length": str(len(data))})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(cm.exception.code, 401)
                req = urllib.request.Request(f"{base}/api/upload?name=s1", data=data, method="POST",
                                             headers={"X-Token": "secret", "Content-Length": str(len(data))})
                with urllib.request.urlopen(req, timeout=5) as r:
                    resp = json.loads(r.read().decode())
                self.assertTrue(resp["ok"], resp)
                self.assertTrue(rec.is_recording(os.path.join(out, "s1")))
                # a second upload with the same name is refused unless replace=1
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(cm.exception.code, 409)
                req = urllib.request.Request(f"{base}/api/recordings", headers={"X-Token": "secret"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    lst = json.loads(r.read().decode())
                self.assertEqual([x["name"] for x in lst["recordings"]], ["s1"])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
