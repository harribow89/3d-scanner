#!/usr/bin/env python3
"""Unit tests for scanner_core (no Qt, no Docker, no camera needed).

Run:  ./.venv/bin/python -m unittest discover -s tests -v
  or  python3 -m unittest tests.test_core
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scanner_core as core  # noqa: E402


class CommandBuilderTests(unittest.TestCase):
    """Every GUI action must be an argv list whose elements survive spaces."""

    def all_commands(self):
        return [
            core.cmd_doctor(), core.cmd_preview(), core.cmd_camera_test(5),
            core.cmd_snap(), core.cmd_map(True), core.cmd_map(False), core.cmd_gui(),
            core.cmd_viz(), core.cmd_export("scan", 3.0, 0.01),
            core.cmd_view(core.OUTPUT_DIR / "a b.ply"),
            core.cmd_isolate(core.OUTPUT_DIR / "a b_cloud.ply", "tabletop_object", 0.006),
            core.cmd_isolate(core.OUTPUT_DIR / "x.ply", "raw_clean", 0.01,
                             core.OUTPUT_DIR / "x.stl"),
            core.cmd_multi(), core.cmd_multi_slam(), core.cmd_overlap(), core.cmd_calibrate(),
            core.cmd_station("capture", "--frames", "4"), core.cmd_station("build"),
            core.cmd_build_image(), core.cmd_stop(),
        ]

    def test_argv_are_lists_of_str(self):
        for c in self.all_commands():
            self.assertIsInstance(c.argv, list, c.title)
            self.assertTrue(all(isinstance(a, str) for a in c.argv), c.title)
            self.assertTrue(c.title)

    def test_launcher_path_is_one_element_even_with_spaces(self):
        # The project folder on this machine contains a space ("3d scanning").
        # The old GUI died on exactly that; make sure the path is ONE argv item.
        c = core.cmd_map()
        self.assertEqual(c.argv[0], str(core.LAUNCHER))
        self.assertEqual(Path(c.argv[0]).name, "run_scanner_docker.sh")
        if " " in str(core.HERE):
            self.assertIn(" ", c.argv[0])

    def test_view_and_isolate_keep_full_paths(self):
        p = core.OUTPUT_DIR / "a b.ply"
        self.assertIn(str(p), core.cmd_view(p).argv)
        iso = core.cmd_isolate(p, "center_focus", 0.0123)
        self.assertIn(str(p), iso.argv)
        self.assertIn("--strategy", iso.argv)
        self.assertEqual(iso.argv[iso.argv.index("--voxel") + 1], "0.0123")

    def test_export_uses_project_relative_paths(self):
        c = core.cmd_export("room", 4.5, 0.02)
        self.assertEqual(c.argv[1:], ["export", "rtabmap.db", "output/room.ply", "4.50", "0.0200"])
        self.assertEqual(core.export_cloud_path("room"), core.OUTPUT_DIR / "room_cloud.ply")

    def test_map_fresh_flag_goes_through_env(self):
        self.assertEqual(core.cmd_map(True).env["MAP_FRESH"], "1")
        self.assertEqual(core.cmd_map(False).env["MAP_FRESH"], "0")
        self.assertTrue(core.cmd_map().long_running)
        self.assertEqual(core.cmd_map().needs_camera, 1)
        self.assertEqual(core.cmd_multi_slam().needs_camera, 2)

    def test_shell_preview_quotes(self):
        c = core.cmd_view(core.OUTPUT_DIR / "a b.ply")
        self.assertIn("'", c.shell_preview())


class XtionDetectionTests(unittest.TestCase):
    def make_dev(self, root, name, vid, pid, bus, dev):
        d = root / name
        d.mkdir()
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{dev}\n")

    def test_detects_only_primesense_devices_sorted_by_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_dev(root, "1-5", "1d27", "0609", 1, 7)
            self.make_dev(root, "1-3", "1d27", "0601", 1, 5)
            self.make_dev(root, "2-1", "046d", "c52b", 2, 3)      # a mouse
            (root / "1-3:1.0").mkdir()                             # interface dir
            (root / "usb1").mkdir()                                # root hub
            (root / "1-9").mkdir()                                 # no idVendor file
            cams = core.detect_xtions(root)
        self.assertEqual([c.port for c in cams], ["1-3", "1-5"])
        self.assertEqual(cams[0].uri, "1d27/0601@1/5")
        self.assertEqual(cams[0].name, "Xtion PRO LIVE")
        self.assertEqual(cams[1].name, "Carmine 1.09")
        self.assertIn("2 cameras", core.camera_summary(cams))

    def test_missing_sysfs_is_empty(self):
        self.assertEqual(core.detect_xtions(Path("/nonexistent/sysfs")), [])
        self.assertEqual(core.camera_summary([]), "No Xtion detected")

    def test_fake_camera_env(self):
        self.assertTrue(core.fake_camera_enabled({core.FAKE_CAMERA_ENV: "1"}))
        self.assertFalse(core.fake_camera_enabled({core.FAKE_CAMERA_ENV: "0"}))
        self.assertFalse(core.fake_camera_enabled({}))


class TelemetryTests(unittest.TestCase):
    def test_missing_db(self):
        t = core.read_telemetry(Path("/nonexistent/rtabmap.db"))
        self.assertFalse(t["db_exists"])
        self.assertEqual(t["nodes"], 0)

    def test_counts_from_rtabmap_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "rtabmap.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE Node (id INTEGER PRIMARY KEY, weight INTEGER)")
            conn.execute("CREATE TABLE Link (from_id INTEGER, to_id INTEGER, type INTEGER)")
            conn.executemany("INSERT INTO Node VALUES (?, ?)", [(1, 0), (2, 0), (3, -1)])
            conn.executemany("INSERT INTO Link VALUES (?, ?, ?)",
                             [(1, 2, 0), (2, 3, 0), (1, 3, 1)])
            conn.commit()
            conn.close()
            t = core.read_telemetry(db)
        self.assertTrue(t["db_exists"])
        self.assertEqual(t["nodes"], 3)
        self.assertEqual(t["keyframes"], 2)
        self.assertEqual(t["links"], 2)
        self.assertEqual(t["loop_closures"], 1)
        self.assertEqual(t["last_loop_closure"], "1 ↔ 3")
        self.assertIsNone(t["error"])

    def test_half_created_db_reports_error_not_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "rtabmap.db"
            db.write_bytes(b"")  # RTAB-Map has created the file but no schema yet
            t = core.read_telemetry(db)
        self.assertTrue(t["db_exists"])
        self.assertIsNotNone(t["error"])

    def test_scan_hint_is_plain_text(self):
        self.assertIn("No map", core.scan_hint({"db_exists": False}, False))
        t = {"db_exists": True, "nodes": 0, "loop_closures": 0}
        self.assertIn("Waiting", core.scan_hint(t, True))
        t = {"db_exists": True, "nodes": 30, "loop_closures": 0}
        self.assertIn("loop", core.scan_hint(t, True))
        t = {"db_exists": True, "nodes": 30, "loop_closures": 2}
        self.assertIn("2 loop", core.scan_hint(t, True))


class LiveStatusTests(unittest.TestCase):
    def test_fresh_and_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp)
            self.assertIsNone(core.read_live_status(live))
            (live / "status.json").write_text(json.dumps({"ts": time.time(), "cameras": {}}))
            s = core.read_live_status(live)
            self.assertTrue(s["fresh"])
            (live / "status.json").write_text(json.dumps({"ts": time.time() - 30}))
            self.assertFalse(core.read_live_status(live)["fresh"])
            (live / "status.json").write_text("{not json")
            self.assertIsNone(core.read_live_status(live))
            rgb, depth = core.live_image_paths(live, "camera")
            self.assertEqual(rgb.name, "camera_rgb.jpg")
            self.assertEqual(depth.name, "camera_depth.jpg")


class DoctorTests(unittest.TestCase):
    def test_quick_doctor_structure(self):
        checks = core.run_doctor(full=False)
        names = [c.name for c in checks]
        self.assertIn("Docker daemon", names)
        self.assertIn("Depth camera (Xtion)", names)
        self.assertNotIn("Python venv (Open3D, SciPy)", names)  # skipped in quick mode
        for c in checks:
            self.assertIsInstance(c.ok, bool)
            self.assertTrue(c.detail)
        blocking = core.doctor_blocking_problems(checks)
        self.assertTrue(all(c.required and not c.ok for c in blocking))

    def test_human_size(self):
        self.assertEqual(core.human_size(10), "10 B")
        self.assertEqual(core.human_size(2048), "2.0 KB")
        self.assertEqual(core.human_size(5 * 1048576), "5.0 MB")


class StrategyListTests(unittest.TestCase):
    def test_matches_point_cloud_tools(self):
        try:
            import point_cloud_tools  # needs open3d
        except Exception as e:  # pragma: no cover
            self.skipTest(f"open3d not importable here: {e}")
        self.assertEqual(core.ISOLATION_STRATEGIES, list(point_cloud_tools.ISOLATION_PROFILES))
        for preset in core.EXPORT_PRESETS.values():
            self.assertIn(preset["strategy"], core.ISOLATION_STRATEGIES)


if __name__ == "__main__":
    unittest.main()
