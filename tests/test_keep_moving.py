import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402
import status  # noqa: E402

T = 1793016000.0


def epic(name="e1", counts=None, lead=True, lead_pane=None, **kw):
    return {"epic": name, "lead": {"session": "sess", "pane": "node-b/t1"} if lead else None, "lead_pane": lead_pane,
            "paused": None, "mtime": T, "counts": counts or {}, "segments": {}, **kw}


def board_of(epics):
    return {"epics": epics, "agents": [], "decisions": [], "interrupts": [], "client_time": {}, "snoozes": {}, "pins": {}}


class Leaderless(unittest.TestCase):
    def keys(self, *epics):
        return {k: v for k, v in board.mate_events(board_of(list(epics))).items() if k.startswith("leaderless:")}

    def test_dead_lead_with_only_waiting_work_is_respawned(self):
        ev = self.keys(epic(counts={"pending": 2, "blocked": 1}))
        self.assertEqual(list(ev), ["leaderless:e1:sess"])
        self.assertIn("adw-respawn node-b/t1", ev["leaderless:e1:sess"])

    def test_live_lead_complete_paused_or_planning_epics_stay_quiet(self):
        c = {"building": 1}
        self.assertFalse(self.keys(epic(counts=c, lead_pane="node-b/t1"), epic("e2", counts=c, completed_at=T),
                                   epic("e3", counts=c, paused={"by": "human"}), epic("e4", counts=c, stage="planning"),
                                   epic("e5", counts={"merged": 3})))

    def test_epic_with_open_work_and_no_lead_goes_to_the_human(self):
        ev = self.keys(epic(counts={"pending": 3}, lead=False))
        self.assertEqual(list(ev), ["leaderless:e1:none"])
        self.assertIn("the human starts a lead", ev["leaderless:e1:none"])


class MarkEnded(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def log(self, session, line):
        (self.dir / f"{session}.log").write_text(line + "\n")

    def test_closes_only_dead_sessions_with_an_old_open_wait(self):
        old = "2026-10-29T08:00:00Z"
        self.log("dead", f"{old} waiting: review bot on #1 until 2026-10-29T08:30:00Z")
        self.log("live", f"{old} waiting: CI")
        self.log("finished", f"{old} done: shipped")
        self.log("fresh", "2026-10-29T08:50:00Z progress: building")
        t = 1793260800.0 + 3600
        for p in self.dir.glob("*.log"):
            os.utime(p, (t, t))
        n = board.mark_ended({"agents": [{"session": "live"}]}, t, self.dir)
        self.assertEqual(n, 1)
        self.assertEqual(status.last_event("dead", self.dir)["kind"], "ended")
        self.assertEqual(status.last_event("live", self.dir)["kind"], "waiting")
        self.assertEqual(board.mark_ended({"agents": []}, t, self.dir), 1)
        self.assertIsNone(board.watch_state("idle", None, status.last_event("dead", self.dir)))


class WatchdogMarksEndedOnFullBoard(unittest.TestCase):
    def test_sessions_hidden_from_the_mate_still_count_as_live(self):
        full = {"agents": [{"session": "personal"}, {"session": "mate"}]}
        seen = []

        class Stop(Exception):
            pass

        def for_mate(b):
            seen.append(("for_mate", [a["session"] for a in b["agents"]]))
            raise Stop

        with mock.patch.object(board, "build", lambda args: full), \
                mock.patch.object(board, "for_mate", for_mate), \
                mock.patch.object(board, "mark_ended", lambda b: seen.append(("ended", [a["session"] for a in b["agents"]]))), \
                mock.patch.object(board.asks, "apply_defaults", lambda: None), \
                mock.patch.object(board.asks, "retry_deliveries", lambda: None), self.assertRaises(Stop):
            board.watchdog_locked(mock.Mock(dry_run=False))
        self.assertEqual(seen, [("ended", ["personal", "mate"]), ("for_mate", ["personal", "mate"])])


class HumanWaitNeedsAsk(unittest.TestCase):
    def run_status(self, *argv):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        with mock.patch.object(status, "STATUS", d), mock.patch.object(status, "session_for", lambda p: "s1"), \
                mock.patch.object(status, "is_mate", lambda p: False), mock.patch.object(_sys, "argv", ["adw-status", *argv, "--pane", "t5"]):
            status.main()
        return (d / "s1.log").read_text()

    def test_wait_on_human_without_ask_is_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.run_status("waiting", "the human to merge #12")
        self.assertIn("adw-ask id", str(e.exception))

    def test_wait_naming_its_ask_or_only_mentioning_the_human_passes(self):
        self.assertIn("d-abc123", self.run_status("waiting", "the human: d-abc123 merge #12"))
        self.assertIn("per the human", self.run_status("waiting", "CI on #12, per the human", "--mention"))


class LendSlots(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "bin" / "adw-lead-state", self.dir / "lead.sh")
        (self.dir / "manifest.json").write_text('{"repos": {}}')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def epic(self, name, mp, statuses, pane="node-a/t1"):
        d = self.dir / "epics" / name
        d.mkdir(parents=True)
        segs = [{"id": f"{name}-{i}", "status": s, "deps": [], "brief": "b.md"} for i, s in enumerate(statuses)]
        (d / "graph.json").write_text(json.dumps({"epic": name, "repo": "r", "max_parallel": mp, "segments": segs}))
        (d / "lead.json").write_text(json.dumps({"pane": pane, "session": name}))

    def ready(self, name, free="20"):
        r = subprocess.run(["bash", str(self.dir / "lead.sh"), "ready", name], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "ADW_HOST": "node-a", "ADW_HOME": str(self.dir),
                                "ADW_MEM_FREE_GB": free})
        return r.stdout.split(), r.stderr

    def test_epic_at_cap_borrows_idle_host_slots(self):
        self.epic("a", 2, ["building", "building", "ready", "ready", "ready"])
        out, err = self.ready("a")
        self.assertEqual(out, ["a-2", "a-3"])
        self.assertIn("2 beyond max_parallel", err)

    def test_no_lending_when_memory_is_short(self):
        self.epic("a", 2, ["building", "building", "ready"])
        self.assertEqual(self.ready("a", free="3")[0], [])

    def test_other_epics_unused_budget_and_host_cap_are_kept(self):
        self.epic("a", 2, ["building", "building", "ready", "ready"])
        self.epic("b", 11, ["building"] * 9 + ["ready"])
        self.epic("c", 5, ["building"] * 5, pane="node-b/t2")
        (self.dir / "slots.json").write_text('{"node-a": {"max_in_flight": 12}}')
        self.assertEqual(self.ready("a")[0], [])

    def test_own_ordinary_budget_counts_against_the_host_cap(self):
        self.epic("a", 2, ["ready"] * 4)
        self.epic("b", 9, ["building"] * 9)
        self.assertEqual(self.ready("a")[0], ["a-0", "a-1", "a-2"])


if __name__ == "__main__":
    unittest.main()
