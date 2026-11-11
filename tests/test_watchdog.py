import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402

T = 1793016000.0
UTC = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


def epic(name="e1", building=("S1",), lead_pane="t13", mtime=T, paused=None):
    segs = {"building": [{"id": s, "pr": None, "note": None} for s in building]} if building else {}
    return {"epic": name, "lead": {"session": "sess", "pane": "t13"}, "lead_pane": lead_pane, "paused": paused,
            "mtime": mtime, "counts": {"building": len(building)} if building else {}, "segments": segs}


def agent(pane="t13", epic="e1", status="idle", watch=None, last_active=T, role="lead"):
    return {"pane": pane, "session": f"s-{pane}", "title": "t", "status": status, "attention": "idle", "watch": watch,
            "last_active": last_active, "epic": epic, "role": role, "lead_pane": pane, "screen": "", "cwd": "~/x"}


def board_of(epics=(), agents=(), decisions=()):
    return {"epics": list(epics), "agents": list(agents), "decisions": list(decisions), "interrupts": [],
            "client_time": {}, "snoozes": {}, "pins": {}}


DESK = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


class TestPlan(unittest.TestCase):
    ITEMS = {"decision:a": "trouble one", "watch:b:stopped-silent:1": "trouble two"}

    def test_fires_once_then_quiet(self):
        h, lines, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        self.assertEqual((h, lines), ("[watchdog] 2 items:", ["trouble one", "trouble two"]))
        h, lines, st = board.watchdog_plan(self.ITEMS, st, T + 300, UTC)
        self.assertIsNone(h)

    def test_only_new_keys_sent(self):
        _, _, st = board.watchdog_plan({"decision:a": "one"}, {}, T, UTC)
        h, lines, _ = board.watchdog_plan(self.ITEMS, st, T + 300, UTC)
        self.assertEqual((h, lines), ("[watchdog] 1 items:", ["trouble two"]))

    def test_refires_after_two_hours(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, DESK)
        self.assertIsNone(board.watchdog_plan(self.ITEMS, st, T + 7000, DESK)[0])
        h, lines, _ = board.watchdog_plan(self.ITEMS, st, T + 7300, DESK)
        self.assertEqual(len(lines), 2)

    def test_away_from_desk_sent_items_wait_for_the_morning_summary(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        self.assertIsNone(board.watchdog_plan(self.ITEMS, st, T + 7300, UTC)[0])
        h, lines, _ = board.watchdog_plan({**self.ITEMS, "decision:c": "trouble three"}, st, T + 7300, UTC)
        self.assertEqual(lines, ["trouble three"])

    def test_away_from_desk_a_dialog_still_refires(self):
        k = "dialog:same-session"
        _, _, st = board.watchdog_plan({k: "A"}, {}, T, UTC)
        _, _, st = board.watchdog_plan({}, st, T + 900, UTC)
        h, lines, _ = board.watchdog_plan({k: "B"}, st, T + 7300, UTC)
        self.assertEqual(lines, ["B"])

    def test_morning_summary_at_eight_eastern_in_daylight_time(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        eight_edt = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(board.watchdog_plan(self.ITEMS, st, T + 300, eight_edt)[0], "[watchdog] morning summary, 2 open:")

    def test_desk_hours_are_eastern_weekdays(self):
        self.assertTrue(board.at_desk(datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)))
        self.assertFalse(board.at_desk(datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)))
        self.assertFalse(board.at_desk(datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc)))
        self.assertTrue(board.at_desk(datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)))
        self.assertFalse(board.at_desk(datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)))

    def test_gone_keys_kept_until_refire_window_then_dropped(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        b = {"watch:b:stopped-silent:1": "trouble two"}
        _, _, st = board.watchdog_plan(b, st, T + 300, UTC)
        self.assertEqual(set(st["fired"]), set(self.ITEMS))
        self.assertIsNone(board.watchdog_plan(self.ITEMS, st, T + 600, UTC)[0])
        _, _, st = board.watchdog_plan(b, st, T + 7300, UTC)
        self.assertEqual(set(st["fired"]), set(b))

    def test_nothing_open_nothing_sent(self):
        self.assertIsNone(board.watchdog_plan({}, {}, T, UTC)[0])

    def test_morning_summary_lists_already_fired_once_a_day(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        morning = datetime(2026, 10, 28, 13, 5, tzinfo=timezone.utc)
        h, lines, st = board.watchdog_plan(self.ITEMS, st, T + 300, morning)
        self.assertEqual((h, len(lines)), ("[watchdog] morning summary, 2 open:", 2))
        self.assertIsNone(board.watchdog_plan(self.ITEMS, st, T + 600, morning)[0])
        nxt = datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc)
        self.assertIsNotNone(board.watchdog_plan(self.ITEMS, st, T + 86400, nxt)[0])

    def test_before_8am_eastern_no_summary(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        self.assertIsNone(board.watchdog_plan(self.ITEMS, st, T + 300, datetime(2026, 10, 28, 11, 59, tzinfo=timezone.utc))[0])


class TestItems(unittest.TestCase):
    def watch(self, state, at):
        return {"state": state, "kind": "waiting", "text": "ci", "at": at, "until": None}

    def test_stopped_silent_needs_20_minutes(self):
        w = self.watch("stopped-silent", T - 3000)
        fresh = board_of([epic(building=())], [agent(watch=w, last_active=T - 600)])
        self.assertFalse([k for k in board.watchdog_items(fresh, T) if "stopped-silent" in k])
        stale = board_of([epic(building=())], [agent(watch=w, last_active=T - 1500)])
        self.assertTrue([k for k in board.watchdog_items(stale, T) if "stopped-silent" in k])

    def test_overdue_fires(self):
        b = board_of([epic(building=())], [agent(watch=self.watch("overdue", T - 9000))])
        self.assertTrue([k for k in board.watchdog_items(b, T) if ":overdue:" in k])

    def mate_requests(self, *agents, mate_pane=None):
        b = {**board_of([epic(building=())], agents), "mate_pane": mate_pane}
        return {k: v for k, v in board.watchdog_items(b, T).items() if k.startswith("mate-request:")}

    def test_waiting_status_addressed_to_the_mate_is_surfaced(self):
        w = {**self.watch("waiting", T - 60), "text": "S1 CI running. first mate: please file ship asks #31334 @a68"}
        got = self.mate_requests(agent(watch=w))
        self.assertEqual(list(got.values()), ["mate-request t13 t [lead of e1] :: first mate: please file ship asks #31334 @a68"])

    def test_mate_request_forms(self):
        for text in ("MATE: send me the flip time", "first mate (owner at lunch): ship #1 when green", "First-mate, file it"):
            self.assertTrue(self.mate_requests(agent(watch={**self.watch("waiting", T), "text": text})), text)
        for text in ("lines via first mate", "sent to first mate", "teammate: x", "waiting on the mate's answer"):
            self.assertFalse(self.mate_requests(agent(watch={**self.watch("waiting", T), "text": text})), text)

    def test_same_request_in_a_later_status_keeps_its_key(self):
        a = self.mate_requests(agent(watch={**self.watch("waiting", T - 600), "text": "building S1. MATE: flip time"}))
        b = self.mate_requests(agent(watch={**self.watch("waiting", T), "text": "building S1, S2. MATE: flip time"}))
        c = self.mate_requests(agent(watch={**self.watch("waiting", T), "text": "building S2. MATE: flip done?"}))
        self.assertEqual(a.keys(), b.keys())
        self.assertNotEqual(a.keys(), c.keys())

    def test_the_mates_own_status_and_ended_statuses_are_not_requests(self):
        own = agent(pane="t9", watch={**self.watch("waiting", T), "text": "first mate: watching t1t"})
        self.assertFalse(self.mate_requests(own, mate_pane="t9"))
        done = agent(watch={**self.watch("done", T), "kind": "done", "text": "MATE: epic finished"})
        self.assertFalse(self.mate_requests(done))

    def test_building_with_no_activity_over_an_hour(self):
        b = board_of([epic(mtime=T - 4000)], [agent(last_active=T - 4000)])
        self.assertIn("building-idle:e1:S1", board.watchdog_items(b, T))
        b = board_of([epic(mtime=T - 4000)], [agent(last_active=T - 1000)])
        self.assertNotIn("building-idle:e1:S1", board.watchdog_items(b, T))
        b = board_of([epic(mtime=T - 4000)], [agent(last_active=T - 4000, status="working")])
        self.assertNotIn("building-idle:e1:S1", board.watchdog_items(b, T))

    def test_gated_ask_open_over_4h(self):
        d = {"id": "d-aaaaaa", "status": "open", "title": "Merge it?", "epic": "e1", "body": "", "options": [], "created": T - 5 * 3600}
        self.assertIn("ask-aging:d-aaaaaa", board.watchdog_items(board_of(decisions=[d]), T))
        d2 = {**d, "id": "d-bbbbbb", "title": "Pick one", "created": T - 5 * 3600}
        self.assertNotIn("ask-aging:d-bbbbbb", board.watchdog_items(board_of(decisions=[d2]), T))
        d3 = {**d, "id": "d-cccccc", "created": T - 3600}
        self.assertNotIn("ask-aging:d-cccccc", board.watchdog_items(board_of(decisions=[d3]), T))

    def test_aged_gated_ask_listed_once(self):
        d = {"id": "d-aaaaaa", "status": "open", "title": "Merge it?", "epic": "e1", "body": "", "options": [], "created": T - 5 * 3600}
        items = board.watchdog_items(board_of(decisions=[d]), T)
        self.assertEqual([k for k in items if "d-aaaaaa" in k], ["ask-aging:d-aaaaaa"])



class TestSharedLead(unittest.TestCase):
    def test_second_epic_of_shared_lead_gets_stall_detection(self):
        e2 = epic("e2", mtime=T - 4000)
        lead = agent(epic="e1", last_active=T - 4000)
        b = board_of([epic("e1", building=(), mtime=T - 4000), e2], [lead])
        self.assertIn("building-idle:e2:S1", board.watchdog_items(b, T))
        lead["status"] = "working"
        self.assertNotIn("building-idle:e2:S1", board.watchdog_items(b, T))


class TestLeaderless(unittest.TestCase):
    def test_one_pane_leading_two_epics_is_not_leaderless(self):
        epics = [{**epic("e1"), "lead_pane": None, "tokens": []}, {**epic("e2"), "lead_pane": None, "tokens": []}]
        listing = [{"agent": "claude", "agent_status": "idle", "pane_id": "t13", "agent_session": {"value": "sess"},
                    "terminal_title_stripped": "lead", "cwd": "/x"}]
        with mock.patch.object(board.asks.mux, "available", lambda: True), mock.patch.object(board.terminals, "agents", lambda live: listing), \
                mock.patch.object(board.asks, "read_pane", lambda p, n: ("", None)), mock.patch.object(board.shutil, "which", lambda n: None), \
                mock.patch.object(board, "load_links", lambda: {}), mock.patch.object(board, "last_active", lambda a, s: None), \
                mock.patch.object(board, "context_tokens", lambda s: 0), mock.patch.object(board.adw_status, "last_event", lambda s: None):
            agents = board.load_agents(epics)
        self.assertEqual([e["lead_pane"] for e in epics], ["t13", "t13"])
        ev = board.mate_events(board_of(epics, agents))
        self.assertFalse([k for k in ev if k.startswith("leaderless:")])

    def test_really_leaderless_epic_still_alarms(self):
        ev = board.mate_events(board_of([epic(lead_pane=None)], []))
        self.assertTrue([k for k in ev if k.startswith("leaderless:e1")])


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "epics").mkdir()
        (self.tmp / "firstmate").mkdir()
        self.sent, self.calls = [], []
        self.status = "idle"
        patches = [
            mock.patch.object(board, "EPICS", self.tmp / "epics"),
            mock.patch.object(board, "MATE", self.tmp / "firstmate"),
            mock.patch.object(board.asks, "apply_defaults", lambda: self.calls.append("defaults")),
            mock.patch.object(board.asks, "retry_deliveries", lambda: self.calls.append("retry")),
            mock.patch.object(board.asks, "prompt", self.prompt),
            mock.patch.object(board.asks, "list_agents", lambda: [{"name": "first-mate", "pane_id": "t12"}]),
            mock.patch.object(board.asks, "agent_info", lambda p: {"status": self.status, "session": "m"}),
            mock.patch.object(board, "for_mate", lambda b: b),
            mock.patch.object(board, "WATCHDOG_MORNING_HOUR", 24),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.b = board_of([epic(mtime=time_now() - 7200)], [agent(last_active=time_now() - 7200)])
        self.b["mate_pane"] = "t12"
        p = mock.patch.object(board, "build", lambda a: self.b)
        p.start()
        self.addCleanup(p.stop)

    def prompt(self, pane, text):
        self.sent.append(("agent", "prompt", pane, text))
        return "", None

    def run_wd(self, dry=False):
        out = io.StringIO()
        with redirect_stdout(out):
            board.watchdog(Namespace(dry_run=dry))
        return out.getvalue()

    def test_one_prompt_to_mate_by_name_then_silence(self):
        self.run_wd()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:3], ("agent", "prompt", "t12"))
        self.assertTrue(self.sent[0][3].startswith("[watchdog] 1 items:"))
        self.assertIn("segment S1 of e1 is building", self.sent[0][3])
        self.run_wd()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("defaults", self.calls)
        self.assertIn("retry", self.calls)
        self.assertTrue((self.tmp / "firstmate/digest.md").exists())

    def test_mate_pane_resolution_order(self):
        self.assertEqual(board.mate_pane({"mate_pane": "t99"}), "t99")
        self.assertEqual(board.mate_pane({}), "t12")
        with mock.patch.object(board.asks, "list_agents", lambda: [{"pane_id": "t33"}]), \
                mock.patch.dict("os.environ", {"ADW_MATE_PANE": "t44"}):
            self.assertEqual(board.mate_pane({}), "t44")

    def test_hc_failure_skips_run_and_keeps_state(self):
        self.run_wd()
        state = (self.tmp / "firstmate/watchdog-state.json").read_text()
        self.b = {**self.b, "agents": None}
        self.run_wd()
        self.assertEqual((self.tmp / "firstmate/watchdog-state.json").read_text(), state)
        self.assertEqual(len(self.sent), 1)

    def test_garbage_state_is_reset(self):
        for junk in ("[]", '{"fired": []}', "3"):
            (self.tmp / "firstmate/watchdog-state.json").write_text(junk)
            self.sent.clear()
            self.run_wd()
            self.assertEqual(len(self.sent), 1, junk)

    def test_held_when_mate_blocked_then_retried(self):
        self.status = "blocked"
        self.run_wd()
        self.assertEqual(self.sent, [])
        self.status = "idle"
        self.run_wd()
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_sends_and_writes_nothing(self):
        out = self.run_wd(dry=True)
        self.assertIn("[watchdog]", out)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.tmp / "firstmate/watchdog-state.json").exists())
        self.assertNotIn("defaults", self.calls)


def time_now():
    import time
    return time.time()


if __name__ == "__main__":
    unittest.main()
