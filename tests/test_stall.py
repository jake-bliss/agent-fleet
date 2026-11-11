import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402
import stall  # noqa: E402

T = 1793016000.0
H = 3600
D = 86400


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Hub:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ("epics", "asks", "status"):
            (self.root / d).mkdir()

    def close(self):
        self.tmp.cleanup()

    def epic(self, name, segs, max_parallel=3, lead="t12", age=10 * D, decisions=None):
        d = self.root / "epics" / name
        d.mkdir(exist_ok=True)
        (d / "graph.json").write_text(json.dumps({"epic": name, "max_parallel": max_parallel, "segments": segs}))
        os.utime(d / "graph.json", (T - age, T - age))
        if lead:
            (d / "lead.json").write_text(json.dumps({"pane": lead, "session": f"sess-{name}", "at": "2026-10-01T00:00:00Z"}))
        if decisions is not None:
            (d / "decisions.md").write_text(decisions)
        return d

    def ask(self, i, status="answered", at=None, epic="e1", title="t"):
        d = {"id": i, "created": T - 20 * D, "status": status, "epic": epic, "title": title}
        if status == "answered":
            d["answer"] = {"choice": "a", "at": at if at is not None else T - 9 * D}
        (self.root / "asks" / f"{i}.json").write_text(json.dumps(d))

    def events(self, rows):
        db = sqlite3.connect(self.root / "events.db")
        db.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, ext_id TEXT UNIQUE, ts TEXT,"
                   " actor TEXT, actor_is_self INTEGER, kind TEXT, object_ref TEXT, title TEXT, body TEXT, url TEXT, epic TEXT,"
                   " level TEXT, reason TEXT, routed_at REAL, delivered_to TEXT, delivered_at REAL, meta TEXT)")
        for n, (kind, epic, seg, ts) in enumerate(rows):
            db.execute("INSERT INTO events(source, ext_id, ts, kind, object_ref, epic) VALUES ('fleet', ?, ?, ?, ?, ?)",
                       (f"x{n}", iso(ts), kind, f"{epic}|{seg}", epic))
        db.commit()
        db.close()

    def status(self, session, lines):
        (self.root / "status" / f"{session}.log").write_text("\n".join(lines) + "\n")

    def run(self, **kw):
        return stall.items(None, T, self.root, **kw)


def seg(i, status="pending", deps=(), note="", **kw):
    return {"id": i, "status": status, "deps": list(deps), "note": note, **kw}


class StallCase(unittest.TestCase):
    def setUp(self):
        self.h = Hub()
        self.addCleanup(self.h.close)

    def keys(self, rule=None, **kw):
        return sorted(k for k in self.h.run(**kw) if rule is None or k.startswith(f"stall:{rule}:"))


class TestReadyIdle(StallCase):
    def test_ready_segment_idle_past_threshold_flags(self):
        self.h.epic("e1", [seg("A", "merged"), seg("B", "pending", ["A"])])
        got = self.h.run()
        self.assertIn("stall:ready-idle:e1", got)
        self.assertIn("B (pending) ready 10d", got["stall:ready-idle:e1"])

    def test_unmet_dep_not_ready(self):
        self.h.epic("e1", [seg("A", "building"), seg("B", "pending", ["A"])])
        self.assertEqual(self.keys("ready-idle"), [])

    def test_recent_change_not_idle_yet(self):
        self.h.epic("e1", [seg("A", "merged"), seg("B", "pending", ["A"])], age=1 * H)
        self.assertEqual(self.keys("ready-idle"), [])

    def test_open_ask_in_note_blocks_readiness(self):
        self.h.ask("d-aaaaaa", "open")
        self.h.epic("e1", [seg("B", "pending", note="waiting on d-aaaaaa")])
        self.assertEqual(self.keys("ready-idle"), [])

    def test_answered_ask_makes_pending_segment_ready(self):
        self.h.ask("d-aaaaaa", "answered")
        self.h.epic("e1", [seg("B", "pending", note="the human answered d-aaaaaa=b")])
        self.assertIn("d-aaaaaa answered", self.h.run()["stall:ready-idle:e1"])

    def test_ask_missing_from_store_is_not_resolved(self):
        self.h.epic("e1", [seg("B", "pending", note="see d-bbbbbb")])
        self.assertEqual(self.keys("ready-idle"), [])

    def test_segment_with_pr_is_not_ready_idle(self):
        self.h.epic("e1", [seg("B", "pending", pr=123)])
        self.assertEqual(self.keys("ready-idle"), [])

    def test_blocked_never_ready_idle_but_frozen_with_resolved_blocker(self):
        self.h.ask("d-aaaaaa", "answered")
        self.h.epic("e1", [seg("B", "blocked", note="the human answered d-aaaaaa=b")])
        got = self.h.run()
        self.assertNotIn("stall:ready-idle:e1", got)
        self.assertIn("B blocked 9d; blocker resolved: yes (d-aaaaaa answered)", got["stall:frozen:e1"])

    def test_latest_note_entry_with_held_is_skipped(self):
        self.h.epic("e1", [seg("B", "pending", note="cut | HELD on TICKET-1"), seg("C", "pending", note="parked (d-63dec1=c)")], age=9 * D)
        got = self.h.run()
        self.assertNotIn("stall:ready-idle:e1", got)
        self.assertNotIn("stall:frozen:e1", got)

    def test_old_held_entry_followed_by_clean_entry_is_not_skipped(self):
        self.h.epic("e1", [seg("B", "pending", note="HELD on TICKET-1 | released, ready to build")])
        self.assertIn("stall:ready-idle:e1", self.h.run())

    def test_held_match_is_whole_word_and_case_insensitive(self):
        self.h.epic("e1", [seg("B", "pending", note="shelled and unparked-ish"), seg("C", "pending", note="Deferred post-MVP")])
        self.assertIn("B (pending", self.h.run()["stall:ready-idle:e1"])
        self.assertNotIn("C (pending", self.h.run()["stall:ready-idle:e1"])

    def test_at_cap_text_does_not_say_dispatch(self):
        self.h.epic("e1", [seg("R", "pending"), seg("W", "building"), seg("X", "pr-ready")], max_parallel=2)
        got = self.h.run()["stall:ready-idle:e1"]
        self.assertIn("2/2 running, at cap", got)
        self.assertNotIn("dispatch", got)

    def test_withdrawn_ask_counts_as_ready(self):
        self.h.ask("d-aaaaaa", "withdrawn")
        self.h.epic("e1", [seg("B", "pending", note="was waiting on d-aaaaaa")])
        got = self.h.run()
        self.assertIn("d-aaaaaa withdrawn", got["stall:ready-idle:e1"])

    def test_held_and_in_flight_excluded(self):
        self.h.epic("e1", [seg("H", "held"), seg("P", "parked"), seg("W", "building")])
        self.assertEqual(self.keys("ready-idle"), [])

    def test_busy_crew_agent_in_worktree_counts_as_active(self):
        self.h.epic("e1", [seg("B", "pending", worktree="/w/B")])
        board_ = {"agents": [{"epic": "e1", "role": "crew", "status": "working", "cwd": "/w/B"}]}
        self.assertNotIn("stall:ready-idle:e1", stall.items(board_, T, self.h.root))

    def test_paused_epic_skipped(self):
        d = self.h.epic("e1", [seg("B", "pending")])
        (d / "PAUSED.json").write_text("{}")
        self.assertEqual(self.keys(), [])


class TestAnswered(StallCase):
    def test_answered_not_logged_flags(self):
        self.h.ask("d-aaaaaa", at=T - 3 * H, title="ship it?")
        self.h.epic("e1", [seg("A", "building")], decisions="# decisions\n")
        got = self.h.run()
        self.assertIn("d-aaaaaa answered 3h ago (not in decisions.md)", got["stall:answered:e1"])

    def test_logged_and_unnamed_does_not_flag(self):
        self.h.ask("d-aaaaaa", at=T - 3 * H)
        self.h.epic("e1", [seg("A", "building")], decisions="- d-aaaaaa=a applied\n")
        self.assertEqual(self.keys("answered"), [])

    def test_fresh_answer_within_threshold_does_not_flag(self):
        self.h.ask("d-aaaaaa", at=T - 30 * 60)
        self.h.epic("e1", [seg("A", "building")], decisions="")
        self.assertEqual(self.keys("answered"), [])

    def test_logged_but_named_segment_unchanged_flags(self):
        self.h.ask("d-aaaaaa", at=T - 5 * H)
        self.h.epic("e1", [seg("A", "building", note="per d-aaaaaa=a")], age=1 * D, decisions="d-aaaaaa logged\n")
        self.assertIn("segment A unchanged since", self.h.run()["stall:answered:e1"])


class TestAnsweredDepBlocked(StallCase):
    def test_answer_for_segment_with_unmet_deps_is_not_flagged(self):
        self.h.ask("d-aaaaaa", at=T - 5 * H)
        self.h.epic("e1", [seg("A", "building"), seg("B", "pending", deps=["A"], note="d-aaaaaa")], age=1 * D, decisions="")
        self.assertEqual(self.keys("answered"), [])

    def test_answer_with_deps_met_and_nothing_acted_still_flags(self):
        self.h.ask("d-aaaaaa", at=T - 5 * H)
        self.h.epic("e1", [seg("A", "merged"), seg("B", "building", deps=["A"], note="d-aaaaaa")], age=1 * D, decisions="")
        self.assertIn("d-aaaaaa answered", self.h.run()["stall:answered:e1"])


class TestAnsweredHeld(StallCase):
    def test_asks_on_held_or_parked_segments_are_not_nagged(self):
        self.h.ask("d-aaaaaa", at=T - 5 * H)
        self.h.ask("d-bbbbbb", at=T - 5 * H)
        self.h.epic("e1", [seg("A", "parked", note="d-aaaaaa"), seg("B", "building", note="old | deferred d-bbbbbb")],
                    age=1 * D, decisions="")
        self.assertEqual(self.keys("answered"), [])

    def test_unlogged_ask_with_no_held_segment_still_nags(self):
        self.h.ask("d-aaaaaa", at=T - 5 * H)
        self.h.epic("e1", [seg("A", "building", note="d-aaaaaa")], age=1 * D, decisions="")
        self.assertEqual(self.keys("answered"), ["stall:answered:e1"])


class TestFrozen(StallCase):
    def test_in_progress_unchanged_over_48h_flags(self):
        self.h.epic("e1", [seg("A", "in_progress")], age=3 * D)
        self.assertIn("A in_progress 3d", self.h.run()["stall:frozen:e1"])

    def test_under_48h_does_not_flag(self):
        self.h.epic("e1", [seg("A", "in_progress")], age=40 * H)
        self.assertEqual(self.keys("frozen"), [])

    def test_held_excluded(self):
        self.h.epic("e1", [seg("A", "held"), seg("B", "parked")], age=9 * D)
        self.assertEqual(self.keys("frozen"), [])

    def test_blocked_reports_unresolved_blocker(self):
        self.h.ask("d-aaaaaa", "open")
        self.h.epic("e1", [seg("A", "blocked", note="needs d-aaaaaa")], age=9 * D)
        self.assertIn("blocker resolved: no (d-aaaaaa open)", self.h.run()["stall:frozen:e1"])

    def test_blocked_on_unmet_dep_is_unresolved(self):
        self.h.epic("e1", [seg("A", "blocked", ["Z"]), seg("Z", "building")], age=9 * D)
        self.assertIn("dep Z building", self.h.run()["stall:frozen:e1"])

    def test_pending_behind_unmet_deps_not_frozen(self):
        self.h.epic("e1", [seg("A", "building"), seg("B", "pending", ["A"])], age=9 * D)
        got = self.h.run()
        self.assertNotIn("B pending", got.get("stall:frozen:e1", ""))


class TestUnderParallel(StallCase):
    def test_fewer_running_than_max_flags(self):
        self.h.epic("e1", [seg("R", "pending"), seg("W", "building")], max_parallel=3)
        self.assertIn("1/3 conductors running while 1 segments could start: R (ready)", self.h.run()["stall:under-parallel:e1"])

    def test_resolved_blocked_segments_count_as_startable_work(self):
        self.h.ask("d-aaaaaa", "answered")
        self.h.epic("e1", [seg("B", "blocked", note="answered d-aaaaaa=b"), seg("W", "building")], max_parallel=3, age=9 * D)
        got = self.h.run()
        self.assertNotIn("stall:ready-idle:e1", got)
        self.assertIn("1 segments could start: B (blocked, blocker resolved)", got["stall:under-parallel:e1"])

    def test_unresolved_blocked_does_not_count(self):
        self.h.ask("d-aaaaaa", "open")
        self.h.epic("e1", [seg("B", "blocked", note="needs d-aaaaaa"), seg("W", "building")], max_parallel=3, age=9 * D)
        self.assertNotIn("stall:under-parallel:e1", self.h.run())

    def test_held_blocked_segment_does_not_count(self):
        self.h.ask("d-aaaaaa", "answered")
        self.h.epic("e1", [seg("B", "blocked", note="d-aaaaaa | parked for now"), seg("W", "building")], max_parallel=3, age=9 * D)
        self.assertNotIn("stall:under-parallel:e1", self.h.run())

    def test_at_capacity_does_not_flag(self):
        self.h.epic("e1", [seg("R", "pending"), seg("W", "building"), seg("X", "pr-open")], max_parallel=2)
        self.assertEqual(self.keys("under-parallel"), [])

    def test_no_ready_segments_does_not_flag(self):
        self.h.epic("e1", [seg("W", "building")], max_parallel=3)
        self.assertEqual(self.keys("under-parallel"), [])


class TestFinish(StallCase):
    def test_at_threshold_with_old_merge_flags(self):
        segs = [seg(f"M{n}", "merged") for n in range(7)] + [seg("D", "building"), seg("E", "pending", ["D"]), seg("F", "pending", ["E"])]
        self.h.epic("e1", segs, age=1 * H)
        self.h.events([("fleet.segment_merged", "e1", "M6", T - 4 * D)])
        got = self.h.run()["stall:finish:e1"]
        self.assertIn("7/10 (70%)", got)
        self.assertIn("close-out path: D(building), E(pending), F(pending)", got)

    def test_recent_merge_does_not_flag(self):
        segs = [seg(f"M{n}", "merged") for n in range(7)] + [seg("D", "building")]
        self.h.epic("e1", segs)
        self.h.events([("fleet.segment_merged", "e1", "M6", T - 1 * D)])
        self.assertEqual(self.keys("finish"), [])

    def test_below_threshold_does_not_flag(self):
        self.h.epic("e1", [seg("A", "merged"), seg("B", "pending"), seg("C", "pending")], age=9 * D)
        self.h.events([("fleet.segment_merged", "e1", "A", T - 9 * D)])
        self.assertEqual(self.keys("finish"), [])

    def test_no_merge_history_falls_back_to_coverage_start_and_graph_mtime(self):
        segs = [seg(f"M{n}", "merged") for n in range(7)] + [seg("D", "building")]
        self.h.epic("e1", segs, age=1 * H)
        self.h.events([("fleet.segment_pr_open", "other", "Z", T - 5 * D)])
        self.assertIn("stall:finish:e1", self.h.run())


class TestPromise(StallCase):
    def line(self, ts, kind, text, until=None):
        return f"{iso(ts)} {kind}: {text}" + (f" until {iso(until)}" if until else "")

    def test_until_passed_without_newer_line_flags(self):
        self.h.status("sessAAAA11", [self.line(T - 3 * H, "waiting", "checking CI", T - 1 * H)])
        got = self.h.run()
        self.assertIn("stall:promise:-:sessAAAA", got)
        self.assertIn("1h past its deadline", got["stall:promise:-:sessAAAA"])

    def test_within_grace_does_not_flag(self):
        self.h.status("sessAAAA11", [self.line(T - 3 * H, "waiting", "x", T - 10 * 60)])
        self.assertEqual(self.keys("promise"), [])

    def test_newer_done_line_clears(self):
        self.h.status("sessAAAA11", [self.line(T - 3 * H, "waiting", "x", T - 2 * H), self.line(T - 1 * H, "done", "ok")])
        self.assertEqual(self.keys("promise"), [])

    def test_report_at_clock_time_in_text(self):
        at = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc).timestamp()
        self.h.status("sessBBBB22", [f"{iso(at - 1800)} progress: will report at 11:00Z"])
        t = at + 2 * H
        got = stall.items(None, t, self.h.root)
        self.assertIn("stall:promise:-:sessBBBB", got)
        self.assertEqual(sorted(stall.items(None, at + 30 * 60, self.h.root)), [])

    def test_ancient_promises_ignored(self):
        self.h.status("sessAAAA11", [self.line(T - 9 * D, "waiting", "x", T - 8 * D)])
        self.assertEqual(self.keys("promise"), [])

    def test_lead_session_maps_to_epic(self):
        self.h.epic("e1", [seg("A", "building")], age=0)
        self.h.status("sess-e1", [self.line(T - 3 * H, "waiting", "x", T - 1 * H)])
        self.assertIn("e1 lead", self.h.run()["stall:promise:e1:sess-e1"])

    def test_session_with_a_live_agent_is_left_to_the_board(self):
        self.h.status("sessAAAA11", [self.line(T - 3 * H, "waiting", "x", T - 1 * H)])
        board_ = {"agents": [{"session": "sessAAAA11", "status": "idle", "epic": None}]}
        self.assertEqual(sorted(stall.items(board_, T, self.h.root)), [])

    def test_refreshable_wait_on_open_ask_is_not_a_missed_promise(self):
        self.h.ask("d-aaaaaa", "open")
        self.h.status("sessAAAA11", [self.line(T - 3 * H, "waiting", "d-aaaaaa with the human", T - 1 * H)])
        self.assertEqual(self.keys("promise"), [])


class TestLeadFallback(StallCase):
    def test_unreachable_lead_says_mate_only(self):
        self.h.epic("e1", [seg("B", "pending")], lead="node-a/t12")
        got = self.h.run(probe=lambda pane, session: False)
        self.assertTrue(got["stall:ready-idle:e1"].endswith("(lead pane node-a/t12 unreachable — mate only)"))

    def test_reachable_lead_has_no_suffix(self):
        self.h.epic("e1", [seg("B", "pending")])
        self.assertNotIn("mate only", self.h.run(probe=lambda pane, session: True)["stall:ready-idle:e1"])

    def test_missing_lead_json_says_mate_only(self):
        self.h.epic("e1", [seg("B", "pending")], lead=None)
        self.assertIn("(no lead pane — mate only)", self.h.run(probe=lambda pane, session: True)["stall:ready-idle:e1"])

    def test_probe_cached_per_pane(self):
        self.h.epic("e1", [seg("B", "pending"), seg("C", "in_progress")])
        calls = []
        self.h.run(probe=lambda p, s: calls.append(p) or True)
        self.assertEqual(calls, ["t12"])


class TestSnapshots(StallCase):
    def test_seeded_from_events_history(self):
        self.h.epic("e1", [seg("A", "blocked")], age=1 * H)
        self.h.events([("fleet.segment_blocked", "e1", "A", T - 5 * D)])
        self.assertIn("A blocked 5d", self.h.run()["stall:frozen:e1"])

    def test_history_in_write_mode_reads_rows_by_name(self):
        self.h.epic("e1", [seg("A", "blocked")], age=1 * H)
        self.h.events([("fleet.segment_blocked", "e1", "A", T - 5 * D), ("fleet.segment_merged", "e1", "Z", T - 6 * D)])
        got = self.h.run(write=True)
        self.assertIn("A blocked 5d", got["stall:frozen:e1"])

    def test_seeded_from_graph_mtime_without_history(self):
        self.h.epic("e1", [seg("A", "in_progress")], age=4 * D)
        self.assertIn("A in_progress 4d", self.h.run()["stall:frozen:e1"])

    def test_write_persists_and_status_change_resets_since(self):
        self.h.epic("e1", [seg("A", "in_progress")], age=4 * D)
        self.assertIn("stall:frozen:e1", self.h.run(write=True))
        db = sqlite3.connect(self.h.root / "events.db")
        self.assertEqual(db.execute("SELECT status, since FROM stall_snapshots").fetchall(), [("in_progress", T - 4 * D)])
        db.close()
        self.h.epic("e1", [seg("A", "pr-open")], age=4 * D)
        self.assertEqual(self.keys("frozen", write=True), [])

    def test_stored_snapshot_wins_over_mtime(self):
        self.h.epic("e1", [seg("A", "in_progress")], age=0)
        os.utime(self.h.root / "epics/e1/graph.json", (T, T))
        self.h.run(write=True)
        db = sqlite3.connect(self.h.root / "events.db")
        db.execute("UPDATE stall_snapshots SET since=?", (T - 3 * D,))
        db.commit()
        db.close()
        self.assertIn("stall:frozen:e1", self.h.run())

    def test_dry_run_writes_nothing(self):
        self.h.epic("e1", [seg("A", "in_progress"), seg("B", "pending")], age=4 * D)
        self.h.ask("d-aaaaaa", at=T - 3 * H)
        self.h.events([("fleet.segment_merged", "e1", "A", T - 5 * D)])
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.h.root.rglob("*") if p.is_file()}
        self.assertTrue(self.h.run())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(sys, "argv", ["stall.py", "check", "--now", "--root", str(self.h.root)]):
            stall.main()
        self.assertIn("stall:ready-idle:e1", buf.getvalue())
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.h.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        db = sqlite3.connect(self.h.root / "events.db")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        db.close()
        self.assertNotIn("stall_snapshots", tables)

    def test_dry_run_without_events_db(self):
        self.h.epic("e1", [seg("B", "pending")])
        self.assertIn("stall:ready-idle:e1", self.h.run())
        self.assertFalse((self.h.root / "events.db").exists())


class TestConfig(StallCase):
    def test_snooze_hides_key_until_time_in_dry_run_too(self):
        self.h.epic("e1", [seg("B", "pending")])
        cfg = self.h.root / "stall.json"
        cfg.write_text(json.dumps({"snooze": {"stall:ready-idle:e1": iso(T + H)}}))
        self.assertEqual(self.keys("ready-idle"), [])
        self.assertEqual(self.keys("ready-idle", write=True), [])
        cfg.write_text(json.dumps({"snooze": {"stall:ready-idle:e1": iso(T - H)}}))
        self.assertEqual(self.keys("ready-idle"), ["stall:ready-idle:e1"])

    def test_snooze_only_hides_the_named_key(self):
        self.h.epic("e1", [seg("B", "pending"), seg("W", "building")], max_parallel=3)
        (self.h.root / "stall.json").write_text(json.dumps({"snooze": {"stall:ready-idle:e1": iso(T + H)}}))
        got = self.keys()
        self.assertIn("stall:under-parallel:e1", got)
        self.assertNotIn("stall:ready-idle:e1", got)


class TestSnoozeFingerprint(StallCase):
    KEY = "stall:ready-idle:e1"

    def snooze(self, days=7):
        with mock.patch.object(stall.time, "time", return_value=T):
            return stall.snooze_keys(self.h.root, [self.KEY], days, T)

    def test_holds_while_nothing_changes(self):
        self.h.epic("e1", [seg("B", "pending", note="a | b")])
        self.snooze()
        self.assertEqual(self.keys("ready-idle"), [])

    def test_lifts_on_status_change(self):
        self.h.epic("e1", [seg("B", "pending"), seg("C", "pending")])
        self.snooze()
        self.h.epic("e1", [seg("B", "ready"), seg("C", "pending")])
        self.assertEqual(self.keys("ready-idle"), [self.KEY])

    def test_lifts_on_latest_note_change_but_not_on_older_entries(self):
        self.h.epic("e1", [seg("B", "pending", note="old | new")])
        self.snooze()
        self.h.epic("e1", [seg("B", "pending", note="rewritten old | new")])
        self.assertEqual(self.keys("ready-idle"), [])
        self.h.epic("e1", [seg("B", "pending", note="old | newer")])
        self.assertEqual(self.keys("ready-idle"), [self.KEY])

    def test_expires_after_until(self):
        self.h.epic("e1", [seg("B", "pending")])
        self.snooze(days=1)
        self.assertEqual(stall.items(None, T + 2 * D, self.h.root).keys() & {self.KEY}, {self.KEY})

    def test_plain_string_form_has_no_fingerprint_check(self):
        self.h.epic("e1", [seg("B", "pending")])
        (self.h.root / "stall.json").write_text(json.dumps({"snooze": {self.KEY: iso(T + H)}}))
        self.h.epic("e1", [seg("B", "ready")])
        self.assertEqual(self.keys("ready-idle"), [])

    def test_cli_writes_until_and_fp_and_keeps_other_keys(self):
        self.h.epic("e1", [seg("B", "pending", note="x | y")])
        (self.h.root / "stall.json").write_text(json.dumps({"frozen": 99, "snooze": {"stall:other": "2099-01-01T00:00:00Z"}}))
        self.snooze(days=7)
        cfg = json.loads((self.h.root / "stall.json").read_text())
        self.assertEqual(cfg["frozen"], 99)
        self.assertEqual(cfg["snooze"]["stall:other"], "2099-01-01T00:00:00Z")
        self.assertEqual(cfg["snooze"][self.KEY], {"until": iso(T + 7 * D), "fp": stall.fingerprint([seg("B", "pending", note="x | y")])})

    def test_snooze_holds_when_live_board_differs_from_snooze_time(self):
        self.h.epic("e1", [seg("B", "pending", worktree="/w/B"), seg("C", "pending"), seg("D", "merged")])
        self.snooze()
        crew = {"agents": [{"epic": "e1", "role": "crew", "status": "working", "cwd": "/w/B"}]}
        got = stall.items(crew, T, self.h.root)
        self.assertNotIn(self.KEY, got)
        self.assertEqual(stall.items(None, T, self.h.root).get(self.KEY), None)

    def test_promise_snooze_fingerprint_is_the_last_status_line(self):
        self.h.status("sessAAAA11", [f"{iso(T - 3 * H)} waiting: x until {iso(T - 1 * H)}"])
        key = "stall:promise:-:sessAAAA"
        stall.snooze_keys(self.h.root, [key], 1, T)
        self.assertNotIn(key, self.h.run())
        self.h.status("sessAAAA11", [f"{iso(T - 3 * H)} waiting: y until {iso(T - 1 * H)}"])
        self.assertIn(key, self.h.run())

    def test_promise_snooze_lifts_on_text_before_a_pipe(self):
        self.h.status("sessAAAA11", [f"{iso(T - 3 * H)} waiting: a | tail until {iso(T - 1 * H)}"])
        key = "stall:promise:-:sessAAAA"
        stall.snooze_keys(self.h.root, [key], 1, T)
        self.h.status("sessAAAA11", [f"{iso(T - 3 * H)} waiting: b | tail until {iso(T - 1 * H)}"])
        self.assertIn(key, self.h.run())

    def test_cli_main_and_unknown_key(self):
        self.h.epic("e1", [seg("B", "pending")])
        with mock.patch.object(sys, "argv", ["stall.py", "snooze", "stall:nope", "--days", "1", "--root", str(self.h.root)]), \
                self.assertRaises(SystemExit):
            stall.main()
        with mock.patch.object(sys, "argv", ["stall.py", "snooze", self.KEY, "--days", "1", "--root", str(self.h.root)]), \
                contextlib.redirect_stdout(io.StringIO()):
            stall.main()
        self.assertIn(self.KEY, json.loads((self.h.root / "stall.json").read_text())["snooze"])

    def test_stall_json_overrides_thresholds(self):
        self.h.epic("e1", [seg("A", "in_progress")], age=3 * D)
        (self.h.root / "stall.json").write_text(json.dumps({"frozen": 5 * D}))
        self.assertEqual(self.keys("frozen"), [])


class TestDeliver(StallCase):
    def fires(self):
        db = sqlite3.connect(self.h.root / "events.db")
        rows = db.execute("SELECT key, rule, epic, seg, delivered_to FROM stall_fires ORDER BY rowid").fetchall()
        db.close()
        return rows

    def setUp(self):
        super().setUp()
        self.h.epic("e1", [seg("B", "pending")])
        self.texts = {"stall:ready-idle:e1": "stall ready-idle e1: x"}

    def test_lead_and_mate_logged_per_target(self):
        sent = []
        stall.deliver(list(self.texts), self.texts, True, lambda p, x: sent.append((p, x)) or True, self.h.root, T)
        self.assertEqual(sent[0][0], "t12")
        self.assertIn("stall ready-idle e1: x", sent[0][1])
        self.assertEqual([r[4] for r in self.fires()], ["mate", "t12"])

    def test_dead_lead_still_logged_for_mate(self):
        stall.deliver(list(self.texts), self.texts, True, lambda p, x: False, self.h.root, T)
        self.assertEqual([r[4] for r in self.fires()], ["mate"])

    def test_nothing_delivered_nothing_recorded(self):
        stall.deliver(list(self.texts), self.texts, False, lambda p, x: False, self.h.root, T)
        self.assertEqual(self.fires(), [])

    def test_lead_only_when_mate_failed(self):
        stall.deliver(list(self.texts), self.texts, False, lambda p, x: True, self.h.root, T)
        self.assertEqual([r[4] for r in self.fires()], ["t12"])

    def test_fire_logged_before_a_later_send_raises(self):
        self.h.epic("e2", [seg("B", "pending")], lead="t19")
        texts = {"stall:ready-idle:e1": "a", "stall:ready-idle:e2": "b"}

        def send(pane, text):
            if pane == "t19":
                raise RuntimeError("boom")
            return True

        with self.assertRaises(RuntimeError):
            stall.deliver(list(texts), texts, False, send, self.h.root, T)
        self.assertEqual([r[4] for r in self.fires()], ["t12"])

    def test_lead_not_reprompted_within_refire(self):
        sent = []
        send = lambda p, x: sent.append(p) or True  # noqa: E731
        stall.deliver(list(self.texts), self.texts, True, send, self.h.root, T)
        stall.deliver(list(self.texts), self.texts, True, send, self.h.root, T + 3 * H)
        self.assertEqual(len(sent), 1)
        stall.deliver(list(self.texts), self.texts, True, send, self.h.root, T + 5 * H)
        self.assertEqual(len(sent), 2)

    def test_annotate_probes_only_given_keys_and_marks_dead_leads(self):
        self.h.epic("e2", [seg("B", "pending")], lead="node-a/t12")
        probed = []
        texts = {"stall:ready-idle:e1": "a", "stall:ready-idle:e2": "b"}
        out, targets = stall.annotate(texts, ["stall:ready-idle:e2"], self.h.root, lambda p, s: probed.append((p, s)) or False)
        self.assertEqual(probed, [("node-a/t12", "sess-e2")])
        self.assertEqual(targets, {"stall:ready-idle:e2": None})
        self.assertEqual(out["stall:ready-idle:e1"], "a")
        self.assertEqual(out["stall:ready-idle:e2"], "b (lead pane node-a/t12 unreachable — mate only)")


UTC = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


class TestBoardWiring(unittest.TestCase):
    ITEMS = {"stall:frozen:e1": "stall frozen", "decision:other": "trouble other"}

    def test_stall_items_refire_after_four_hours_others_after_two(self):
        desk = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, desk)
        h, lines, _ = board.watchdog_plan(self.ITEMS, st, T + 2 * H + 300, desk)
        self.assertEqual(lines, ["trouble other"])
        h, lines, _ = board.watchdog_plan(self.ITEMS, st, T + 4 * H + 300, desk)
        self.assertEqual(sorted(lines), ["stall frozen", "trouble other"])

    def test_away_from_desk_only_stall_items_refire(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        h, lines, _ = board.watchdog_plan(self.ITEMS, st, T + 4 * H + 300, UTC)
        self.assertEqual(lines, ["stall frozen"])

    def test_stall_fired_state_kept_for_four_hours(self):
        _, _, st = board.watchdog_plan(self.ITEMS, {}, T, UTC)
        _, _, st = board.watchdog_plan({}, st, T + 3 * H, UTC)
        self.assertEqual(set(st["fired"]), {"stall:frozen:e1"})

    def test_stall_failure_is_logged_and_skipped(self):
        b = {"epics": [], "agents": [], "decisions": [], "interrupts": [], "client_time": {}, "snoozes": {}, "pins": {}}
        err = io.StringIO()
        with mock.patch.object(board, "stall_module", side_effect=RuntimeError("boom")), contextlib.redirect_stderr(err):
            self.assertIsInstance(board.watchdog_items(b, T), dict)
        self.assertIn("stall skipped", err.getvalue())

    def test_stall_items_join_the_feed(self):
        b = {"epics": [], "agents": [], "decisions": [], "interrupts": [], "client_time": {}, "snoozes": {}, "pins": {}}
        fake = mock.Mock()
        fake.items.return_value = {"stall:frozen:e1": "stall frozen e1"}
        with mock.patch.object(board, "stall_module", return_value=fake):
            self.assertEqual(board.watchdog_items(b, T)["stall:frozen:e1"], "stall frozen e1")


class TestBoardDeliver(StallCase):
    KEY = "stall:ready-idle:e1"

    def setUp(self):
        super().setUp()
        self.h.epic("e1", [seg("B", "pending")])
        (self.h.root / "firstmate").mkdir()
        self.items = {self.KEY: "stall ready-idle e1: x"}
        self.prompts = []
        self.lead = {"status": "idle", "session": "sess-e1"}

    def run_locked(self, mate_prompt_err=None, st=None, items=None):
        import argparse
        if st is not None:
            (self.h.root / "firstmate" / "watchdog-state.json").write_text(json.dumps(st))

        def asks_prompt(pane, text):
            self.prompts.append(pane)
            return (None, mate_prompt_err) if mate_prompt_err and pane == "mate:p1" else ("", None)

        b = {"agents": [], "epics": [], "decisions": [], "interrupts": []}
        with mock.patch.object(board, "EPICS", self.h.root / "epics"), mock.patch.object(board, "MATE", self.h.root / "firstmate"), \
                mock.patch.object(board, "build", return_value=b), mock.patch.object(board, "for_mate", side_effect=lambda x: x), \
                mock.patch.object(board, "watchdog_items", return_value=items or self.items), mock.patch.object(board, "digest", return_value=""), \
                mock.patch.object(board, "mate_pane", return_value="mate:p1"), mock.patch.object(board.asks.mux, "get", lambda p: self.lead), \
                mock.patch.object(board.asks, "apply_defaults"), mock.patch.object(board.asks, "retry_deliveries"), \
                mock.patch.object(board.asks, "agent_info", return_value={"status": "idle"}), \
                mock.patch.object(board.asks, "prompt", asks_prompt), mock.patch.object(board.time, "time", return_value=T), \
                mock.patch.object(board, "stall_module", return_value=stall):
            board.watchdog_locked(argparse.Namespace(dry_run=False))

    def fires(self):
        db = sqlite3.connect(self.h.root / "events.db")
        try:
            return [r[0] for r in db.execute("SELECT delivered_to FROM stall_fires ORDER BY rowid")]
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()

    def test_fresh_keys_come_from_prior_state(self):
        self.assertEqual(board.stall_fresh(self.items, {"fired": {self.KEY: T - 3 * H}}, T), [])
        self.assertEqual(board.stall_fresh(self.items, {"fired": {self.KEY: T - 5 * H}}, T), [self.KEY])
        self.assertEqual(board.stall_fresh({"other": "x"}, {}, T), [])

    def test_mate_and_lead_both_prompted_and_logged(self):
        self.run_locked()
        self.assertEqual(self.prompts, ["mate:p1", "t12"])
        self.assertEqual(self.fires(), ["mate", "t12"])

    def test_nothing_fresh_means_no_header_and_no_lead_prompt(self):
        self.run_locked(st={"fired": {self.KEY: T - 3 * H}, "morning": "2026-10-26"})
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.fires(), [])

    def test_mate_failure_prompts_lead_once_and_not_again(self):
        self.run_locked(mate_prompt_err="boom")
        self.assertEqual(self.prompts, ["mate:p1", "t12"])
        self.run_locked(mate_prompt_err="boom")
        self.assertEqual(self.prompts, ["mate:p1", "t12", "mate:p1"])
        self.assertEqual(self.fires(), ["t12"])

    def test_dead_lead_falls_back_to_mate_with_note(self):
        self.lead = None
        self.run_locked()
        self.assertEqual(self.prompts, ["mate:p1"])
        self.assertEqual(self.fires(), ["mate"])
        db = sqlite3.connect(self.h.root / "events.db")
        text = db.execute("SELECT text FROM stall_fires").fetchone()[0]
        db.close()
        self.assertTrue(text.endswith("(lead pane t12 unreachable — mate only)"))

    def test_lead_in_dialog_or_reused_pane_is_unreachable(self):
        self.assertFalse(self.reach({"status": "blocked", "session": "sess-e1"}))
        self.assertFalse(self.reach({"status": "idle", "session": "other"}))
        self.assertTrue(self.reach({"status": "idle", "session": "sess-e1"}))

    def reach(self, lead):
        self.lead = lead
        with mock.patch.object(board.asks.mux, "get", lambda p: self.lead):
            return board.lead_reachable("t12", "sess-e1")



class TestRedispatch(StallCase):
    def setUp(self):
        super().setUp()
        self.sent, self.mate = [], []
        self.where = ("t12", "idle")
        self.h.ask("d-aaaaaa", at=T - 3 * H, title="pick one")
        d = self.h.root / "asks" / "d-aaaaaa.json"
        a = json.loads(d.read_text())
        a["options"] = [{"key": "a", "label": "ship it"}]
        d.write_text(json.dumps(a))
        self.h.epic("e1", [seg("A", "building", note="per d-aaaaaa")], age=1 * D, decisions="")
        self.h.events([("fleet.segment_blocked", "e1", "Z", T - 5 * D), ("fleet.segment_pr_open", "e1", "A", T - 5 * D)])

    def go(self, t=T, **cfg):
        if cfg:
            (self.h.root / "stall.json").write_text(json.dumps(cfg))
        return stall.redispatch(self.h.root, t, lambda p, x: self.sent.append((p, x)) or True,
                                lambda s, p: self.where, lambda x: self.mate.append(x) or True)

    def log(self):
        db = sqlite3.connect(self.h.root / "events.db")
        return db.execute("SELECT ask, outcome, target FROM redispatch_log ORDER BY ts").fetchall()

    def test_sends_concrete_instruction_to_idle_lead(self):
        self.assertEqual(self.go(), [("d-aaaaaa", "sent")])
        pane, text = self.sent[0]
        self.assertEqual(pane, "t12")
        self.assertIn("segment A of e1", text)
        self.assertIn("(a) ship it", text)
        self.assertEqual(self.log(), [("d-aaaaaa", "sent", "t12")])

    def test_rate_limited_then_resent_after_gap(self):
        self.go()
        self.assertEqual(self.go(t=T + 10 * 60), [])
        self.assertEqual(self.go(t=T + 3 * H), [("d-aaaaaa", "sent")])
        self.assertEqual(len(self.sent), 2)

    def test_escalates_to_mate_after_two_failed_sends(self):
        self.go()
        self.go(t=T + 3 * H)
        self.assertEqual(self.go(t=T + 6 * H), [("d-aaaaaa", "escalated")])
        self.assertEqual(len(self.mate), 1)
        self.assertEqual(self.go(t=T + 12 * H), [])

    def test_working_or_dialog_lead_is_not_sent_or_counted(self):
        self.where = ("t12", "working")
        self.assertEqual(self.go(), [])
        self.assertEqual(self.log() if (self.h.root / "events.db").exists() else [], [])

    def test_gone_lead_is_journaled_leaderless_and_not_sent(self):
        self.where = None
        self.assertEqual(self.go(), [("d-aaaaaa", "leaderless")])
        self.assertEqual(self.sent, [])

    def test_moved_lead_is_found_by_session_and_repointed(self):
        self.where = ("t99", "idle")
        self.go()
        self.assertEqual(self.sent[0][0], "t99")
        lead = json.loads((self.h.root / "epics" / "e1" / "lead.json").read_text())
        self.assertEqual((lead["pane"], lead["session"]), ("t99", "sess-e1"))

    def test_kill_switch(self):
        self.assertEqual(self.go(redispatch=False), [])
        self.assertEqual(self.sent, [])

    def test_progressed_segment_is_not_redispatched(self):
        self.h.epic("e1", [seg("A", "pr-open", note="per d-aaaaaa")], age=1 * D, decisions="d-aaaaaa ok")
        db = sqlite3.connect(self.h.root / "events.db")
        db.execute("UPDATE events SET ts=? WHERE object_ref='e1|A'", (iso(T - 1 * H),))
        db.commit()
        db.close()
        self.assertEqual(self.go(), [])

    def test_resolve_runs_once_per_epic(self):
        self.h.ask("d-bbbbbb", at=T - 3 * H)
        self.h.epic("e1", [seg("A", "building", note="d-aaaaaa"), seg("B", "building", note="d-bbbbbb")], age=1 * D, decisions="")
        calls = []
        stall.redispatch(self.h.root, T, lambda p, x: True, lambda s, p: calls.append(s) or self.where, lambda x: True)
        self.assertEqual(len(calls), 1)

    def test_segment_also_waiting_on_an_open_ask_is_not_redispatched(self):
        self.h.ask("d-bbbbbb", status="open", at=None)
        self.h.epic("e1", [seg("A", "blocked", note="d-aaaaaa d-bbbbbb")], age=1 * D, decisions="d-aaaaaa d-bbbbbb")
        self.assertEqual(self.go(), [])
        self.assertEqual(self.sent, [])

    def test_segment_with_a_pr_in_flight_is_not_redispatched(self):
        self.h.epic("e1", [seg("A", "pr-open", note="merge ask d-aaaaaa", pr=12)], age=1 * D, decisions="")
        self.assertEqual(self.go(), [])
        self.assertEqual(self.sent, [])

    def test_per_run_cap(self):
        for n in ("d-bbbbbb", "d-cccccc"):
            self.h.ask(n, at=T - 3 * H)
        self.h.epic("e1", [seg("A", "building", note="d-aaaaaa d-bbbbbb d-cccccc")], age=1 * D, decisions="")
        self.assertEqual(len(self.go(redispatch_per_run=2)), 2)

    def test_ask_pointer_field_links_the_segment(self):
        self.h.epic("e1", [seg("A", "building", ask="d-aaaaaa")], age=1 * D, decisions="d-aaaaaa")
        self.assertEqual(self.go(), [("d-aaaaaa", "sent")])

    def test_pending_segment_covered_by_ready_idle_still_retries_and_escalates(self):
        self.h.epic("e1", [seg("A", "pending", note="per d-aaaaaa")], age=1 * D, decisions="d-aaaaaa")
        self.assertIn("stall:ready-idle:e1", self.h.run())
        self.assertEqual(self.go(), [("d-aaaaaa", "sent")])
        self.assertEqual(self.go(t=T + 3 * H), [("d-aaaaaa", "sent")])
        self.assertEqual(self.go(t=T + 6 * H), [("d-aaaaaa", "escalated")])

    def test_held_segment_is_not_redispatched(self):
        self.h.epic("e1", [seg("A", "building", note="d-aaaaaa | held for the human")], age=1 * D, decisions="d-aaaaaa")
        self.assertEqual(self.go(), [])

    def test_instruction_asks_for_status_not_just_note(self):
        self.go()
        self.assertIn("set its status (blocked or held)", self.sent[0][1])

    def test_notify_attempts_are_capped_per_run(self):
        self.h.ask("d-bbbbbb", at=T - 3 * H)
        self.h.epic("e1", [seg("A", "building", note="d-aaaaaa d-bbbbbb")], age=1 * D, decisions="")
        self.go()
        self.go(t=T + 3 * H)
        self.assertEqual(len(self.go(t=T + 6 * H)), 1)
        self.assertEqual(len(self.mate), 1)

    def test_dep_blocked_sibling_is_not_redispatched(self):
        self.h.epic("e1", [seg("A", "pr-open", note="per d-aaaaaa"), seg("B", "pending", note="per d-aaaaaa", deps=["C"]),
                           seg("C", "pr-open")], age=1 * D, decisions="d-aaaaaa")
        db = sqlite3.connect(self.h.root / "events.db")
        db.execute("UPDATE events SET ts=? WHERE object_ref='e1|A'", (iso(T - 1 * H),))
        db.commit()
        db.close()
        self.assertEqual(self.go(), [])

    def test_snooze_suppresses_even_when_ready_idle_covers_the_segment(self):
        self.h.epic("e1", [seg("A", "pending", note="per d-aaaaaa")], age=1 * D, decisions="d-aaaaaa")
        self.go(snooze={"stall:answered:e1": {"until": iso(T + 5 * D), "fp": "stale"}})
        self.assertEqual(self.sent, [])

    def test_crewed_segment_is_not_redispatched(self):
        self.h.epic("e1", [seg("A", "building", note="per d-aaaaaa", worktree="/w/seg-a")], age=1 * D, decisions="d-aaaaaa")
        board = {"agents": [{"epic": "e1", "role": "crew", "status": "working", "cwd": "/w/seg-a"}]}
        self.assertEqual(stall.redispatch(self.h.root, T, lambda p, x: self.sent.append(p) or True, lambda s, p: self.where,
                                          lambda x: True, board), [])
        self.assertEqual(self.sent, [])

    def test_repoint_refuses_when_session_changed(self):
        self.assertFalse(stall.repoint_lead(self.h.root, "e1", "t99", "other-session"))
        lead = json.loads((self.h.root / "epics" / "e1" / "lead.json").read_text())
        self.assertEqual(lead["pane"], "t12")

    def test_repoint_rewrites_pane_for_matching_session(self):
        self.assertTrue(stall.repoint_lead(self.h.root, "e1", "t99", "sess-e1"))
        lead = json.loads((self.h.root / "epics" / "e1" / "lead.json").read_text())
        self.assertEqual((lead["pane"], lead["session"]), ("t99", "sess-e1"))
        self.assertFalse(stall.repoint_lead(self.h.root, "e1", "t99", "sess-e1"))


if __name__ == "__main__":
    unittest.main()
