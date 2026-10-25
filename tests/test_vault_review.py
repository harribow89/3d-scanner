import html.parser
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "hec_node_vault"
sys.path.insert(0, str(VAULT))

import fabrication  # noqa: E402
import spec as vault_spec  # noqa: E402


class VaultReviewTests(unittest.TestCase):
    def test_box_massing_parts_are_draft(self):
        spec = vault_spec.build_spec(vault_spec.PRESETS["resolved"])
        printed = [part for part in spec["parts"] if part["printed"]]
        self.assertTrue(printed)
        self.assertTrue(all(part["kind"] == "box" for part in printed))
        self.assertTrue(all(part["print_profile"]["draft"] for part in printed))

    def test_build_sheet_matches_resolved_spec(self):
        class PayloadParser(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.capturing = False
                self.chunks = []

            def handle_starttag(self, tag, attrs):
                self.capturing = tag == "script" and dict(attrs).get("id") == "vault-data"

            def handle_endtag(self, tag):
                if tag == "script" and self.capturing:
                    self.capturing = False

            def handle_data(self, data):
                if self.capturing:
                    self.chunks.append(data)

        parser = PayloadParser()
        parser.feed((VAULT / "build_sheet.html").read_text(encoding="utf-8"))
        payload = json.loads("".join(parser.chunks))
        data = fabrication.build("resolved")
        spec = data["spec"]

        expected_parts = [{
            "key": part["key"], "name": part["name"], "group": part["group"],
            "no": part["no"], "printed": part["printed"], "size": part["size"],
            "pos": part["pos"], "family": part.get("family", ""),
            "material": part["material"], "profile": part.get("print_profile", {}),
        } for part in spec["parts"]]
        expected_steps = [{
            "no": step["no"], "title": step["title"], "time": step["time"],
            "detail": step["detail"], "warning": step.get("warning", ""),
            "tools": step["tools"], "cut": step["cut"],
            "parts": fabrication.resolve_step_parts(spec, step),
            "visible": fabrication.parts_visible_at(spec, step["no"]),
        } for step in data["steps"]]

        self.assertEqual(payload["parts"], expected_parts)
        self.assertEqual(payload["steps"], expected_steps)
        self.assertEqual(payload["params"], {
            key: spec["params"][key] for key in (
                "width", "depth", "height", "node_count", "tier_pitch", "board",
                "psu_form", "fan_size", "exhaust_fans", "intake_fans",
                "glass_thickness", "printer_bed",
            )
        })
        self.assertEqual(payload["levels"], spec["levels"])
        self.assertEqual(payload["tiers"], spec["tiers"])
        self.assertEqual(payload["cut"], data["cut_parts"])
        self.assertEqual(payload["fixings"], data["fixings"])
        self.assertEqual(payload["totals"], data["totals"])
        self.assertEqual(payload["warnings"], spec["warnings"])
        self.assertEqual(payload["printed_totals"], spec["totals"])

    def test_assembly_warning_says_short_circuit(self):
        spec = vault_spec.build_spec(vault_spec.PRESETS["resolved"])
        warning = next(step["warning"] for step in fabrication.assembly_steps(spec)
                       if step["no"] == 2)
        self.assertIn("short circuit waiting to happen", warning)


if __name__ == "__main__":
    unittest.main()
