import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402


class LeadPlanningTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "manifest.json").write_text('{"repos": {}}')
        self.epic = self.dir / "epics/ep"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def lead(self, *args):
        r = subprocess.run(["bash", str(ROOT / "bin/adw-lead-state"), *args], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "ADW_BY": "owner", "ADW_HOME": str(self.dir), "HOME": str(self.dir)})
        return r.returncode, r.stdout + r.stderr

    def fill_plan(self):
        p = self.epic / "plan.md"
        p.write_text(p.read_text().replace("(fill at the frame gate)", "query returns 0 rows"))

    def test_planning_blocks_dispatch_until_handoff(self):
        self.assertEqual(self.lead("init", "ep", "repo-a", "goal", "--planning")[0], 0)
        self.assertEqual(json.loads((self.epic / "PLANNING.json").read_text())["by"], "owner")
        code, out = self.lead("status", "ep")
        self.assertIn("** PLANNING", out)
        code, out = self.lead("ready", "ep")
        self.assertEqual((code, out.strip()), (0, "# epic is still PLANNING: dispatch nothing until adw-lead-state handoff"))
        self.assertIn("planning 1\n  ep", self.lead("fleet", "-")[1])

        code, out = self.lead("handoff", "ep", "3", "1500", "first cut")
        self.assertNotEqual(code, 0)
        self.assertIn("placeholder", out)
        self.fill_plan()
        self.assertNotEqual(self.lead("handoff", "ep", "x", "1500", "s")[0], 0)
        self.assertNotEqual(self.lead("handoff", "ep", "0", "1500", "s")[0], 0)
        self.assertEqual(self.lead("handoff", "ep", "3", "1500", "first cut")[0], 0)
        ready = json.loads((self.epic / "READY.json").read_text())
        self.assertEqual((ready["forecast_segments"], ready["forecast_lines"], ready["by"]), (3, 1500, "owner"))
        self.assertIn("forecast 3 segments", (self.epic / "decisions.md").read_text())
        self.assertNotEqual(self.lead("handoff", "ep", "3", "1500", "again")[0], 0)
        self.assertNotIn("PLANNING", self.lead("status", "ep")[1])
        self.assertIn("ready for a lead", self.lead("fleet", "-")[1])

    def test_over_forecast_tripwire(self):
        self.lead("init", "ep", "repo-a", "goal", "--planning")
        self.fill_plan()
        self.lead("handoff", "ep", "2", "900", "s")
        g = json.loads((self.epic / "graph.json").read_text())
        g["segments"] = [{"id": f"s{i}", "deps": [], "status": "pending"} for i in range(5)]
        (self.epic / "graph.json").write_text(json.dumps(g))
        out = self.lead("status", "ep")[1]
        self.assertIn("OVER FORECAST: 5 segments vs 2 planned", out)
        self.assertNotIn("redesign", out)
        g["segments"] = g["segments"][:4]
        (self.epic / "graph.json").write_text(json.dumps(g))
        self.assertNotIn("OVER FORECAST", self.lead("status", "ep")[1])

    def test_fleet_lists_by_stage(self):
        self.lead("init", "ep", "repo-a", "goal", "--planning")
        self.lead("pause", "ep", "later")
        out = self.lead("fleet", "-")[1]
        self.assertIn("paused 1\n  ep", out)
        self.assertIn("planning 0", out)
        self.lead("resume", "ep")
        self.fill_plan()
        self.lead("handoff", "ep", "2", "900", "s")
        self.assertIn("ready for a lead", self.lead("fleet", "-")[1])
        (self.epic / "lead.json").write_text("{}")
        self.assertIn("planning 0", self.lead("fleet", "-")[1])

    def test_handoff_needs_planning_and_plain_init_unchanged(self):
        self.lead("init", "ep", "repo-a", "goal")
        self.assertFalse((self.epic / "PLANNING.json").exists())
        self.assertNotEqual(self.lead("handoff", "ep", "2", "900", "s")[0], 0)
        self.assertNotIn("PLANNING", self.lead("status", "ep")[1])


class BoardStageTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_stage_and_mate_event(self):
        d = self.dir / "ep"
        d.mkdir()
        (d / "graph.json").write_text('{"epic": "ep", "repo": "repo-a", "segments": []}')
        self.assertEqual(board.load_epic(d)["stage"], "live")
        (d / "PLANNING.json").write_text("{}")
        self.assertEqual(board.load_epic(d)["stage"], "planning")
        (d / "READY.json").write_text("{}")
        e = board.load_epic(d)
        self.assertEqual(e["stage"], "ready")
        b = {"epics": [e], "agents": [], "decisions": [], "interrupts": [], "requests": []}
        with mock.patch.object(board, "needs_you", return_value=[]), \
                mock.patch.object(board, "EPICS", self.dir / "none"):
            self.assertEqual(board.mate_events(b)["epic-ready:ep"],
                             "plan-ready epic ep was approved in planning; start its lead")
        e["paused"] = {"paused_at": 1}
        with mock.patch.object(board, "needs_you", return_value=[]), \
                mock.patch.object(board, "EPICS", self.dir / "none"):
            self.assertNotIn("epic-ready:ep", board.mate_events(b))
        (d / "lead.json").write_text('{"session": "s", "pane": "t1"}')
        self.assertEqual(board.load_epic(d)["stage"], "live")


class LeadRegistrationTest(unittest.TestCase):
    def test_lead_refused_while_planning(self):
        import status
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        d = root / "ep"
        d.mkdir()
        (d / "graph.json").write_text("{}")
        (d / "PLANNING.json").write_text("{}")
        with mock.patch.object(status, "EPICS", root), mock.patch.object(status, "session_for", return_value="s"), \
                mock.patch.object(sys, "argv", ["adw-status", "lead", "ep", "--pane", "t1"]):
            with self.assertRaises(SystemExit) as cm:
                status.main()
            self.assertIn("still being planned", str(cm.exception))
            self.assertFalse((d / "lead.json").exists())
            (d / "READY.json").write_text("{}")
            with mock.patch.object(status, "rebind_asks", return_value=0):
                status.main()
        self.assertEqual(json.loads((d / "lead.json").read_text())["session"], "s")


if __name__ == "__main__":
    unittest.main()
