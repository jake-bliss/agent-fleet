import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402
import terminals  # noqa: E402

T = 1000.0


def rec(addr, state="idle", msg=None, kind=None, sid="s1"):
    status = {"app": "claude-code", "state": state, **({"kind": kind} if kind else {}), **({"msg": msg} if msg else {})}
    return {"address": addr, "state": "live", "status": status, "cwd": "/x", "name": "n", "labels": {}, "turn": 0,
            "title": "T", "argv": ["claude", "--resume", sid], "status_ms": 1}


def snap(*recs):
    return json.dumps({"seq": 1, "channel": None, "terminals": list(recs)})


def event(kind, addr, data):
    return json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": kind, "terminal": addr, "seq": 2, "data": data}})


def run(mp, lines, now=T):
    terminals.stream(mp, lines, clock=lambda: now, machines=lambda: {"local": True, "node-a": True})


class TestMap(unittest.TestCase):
    def test_fake_stream_updates_map(self):
        mp = terminals.TerminalMap()
        run(mp, [snap(rec("t1"), rec("node-a/t2")), event("status", "t1", {"app": "claude-code", "state": "blocked",
             "kind": "question", "msg": "Pick one?"}), event("turn", "t1", {"turn": 4}), event("finished", "node-a/t2", {})])
        t = {e["address"]: e for e in mp.view()["terminals"]}
        self.assertEqual((t["t1"]["state"], t["t1"]["kind"], t["t1"]["msg"], t["t1"]["turn"]),
                         ("blocked", "question", "Pick one?", 4))
        self.assertEqual((t["t1"]["machine"], t["node-a/t2"]["machine"], t["node-a/t2"]["state"]), ("local", "node-a", "finished"))
        self.assertEqual(t["t1"]["session"], "s1")
        self.assertTrue(mp.view()["machines"]["node-a"]["connected"])

    def test_missed_ends_stream_and_marks_disconnected(self):
        mp = terminals.TerminalMap()
        lines = [snap(rec("t1")), json.dumps({"jsonrpc": "2.0", "method": "missed", "params": {}}), event("turn", "t1", {"turn": 9})]
        run(mp, iter(lines))
        self.assertFalse(mp.view()["machines"]["local"]["connected"])
        self.assertEqual(mp.view()["terminals"][0]["turn"], 0)

    def test_run_resnapshots_after_drop(self):
        mp = terminals.TerminalMap()
        feeds = iter([[snap(rec("t1"))], [snap(rec("t1"), rec("t2"))]])
        calls = []
        real = terminals.stream

        def fake_stream(m, lines):
            calls.append(1)
            real(m, next(feeds), clock=lambda: T, machines=lambda: {"local": True})
            if len(calls) == 2:
                raise KeyboardInterrupt

        with mock.patch.object(terminals, "stream", fake_stream), mock.patch.object(terminals.subprocess, "Popen"):
            with self.assertRaises(KeyboardInterrupt):
                terminals.run(mp, sleep=lambda s: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(mp.view()["terminals"]), 2)

    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sub/terminals.json"
            mp = terminals.TerminalMap(out)
            run(mp, [snap(rec("t1", "blocked", "Q?", "permission"))])
            got = terminals.load(out)
            self.assertEqual(got["terminals"][0]["msg"], "Q?")
            self.assertEqual(got["machines"]["local"]["last_seen_at"], T)
            self.assertEqual([p.name for p in out.parent.iterdir()], ["terminals.json"])


class TestReview(unittest.TestCase):
    def test_heartbeat_keeps_local_fresh_with_remote_only_listing(self):
        mp = terminals.TerminalMap()
        terminals.stream(mp, [snap(rec("t1")), json.dumps({"method": "heartbeat", "params": {"seq": 3}})],
                         clock=iter([T, T + 200]).__next__, machines=lambda: {"node-a": True})
        self.assertEqual(mp.view()["machines"]["local"]["last_seen_at"], T + 200)

    def test_status_re_resolves_missing_and_replaced_session(self):
        mp = terminals.TerminalMap()
        bare = {**rec("t1"), "status": None, "argv": ["zsh"]}
        run(mp, [snap(bare)])
        self.assertIsNone(mp.view()["terminals"][0]["session"])
        with mock.patch.object(terminals.mux, "hc_json", return_value=(rec("t1"), None)), \
             mock.patch.object(terminals.mux, "session_of", return_value="s2"):
            mp.apply(json.loads(event("status", "t1", {"app": "claude-code", "state": "working"})), T)
            self.assertEqual(mp.view()["terminals"][0]["session"], "s2")
            mp.apply(json.loads(event("status", "t1", {"app": "claude-code", "state": "idle"})), T)
        with mock.patch.object(terminals.mux, "hc_json", return_value=(rec("t1"), None)), \
             mock.patch.object(terminals.mux, "session_of", return_value="s3"):
            mp.apply(json.loads(event("status", "t1", {"app": "claude-code", "state": "working"})), T)
            self.assertEqual(mp.view()["terminals"][0]["session"], "s3")

    def test_snapshot_replaces_but_keeps_disconnected_machine(self):
        mp = terminals.TerminalMap()
        run(mp, [snap(rec("t1"), rec("node-a/t3"), rec("node-b/t4"))])
        terminals.stream(mp, [snap(rec("t1"))], clock=lambda: T, machines=lambda: {"node-a": True, "node-b": False})
        self.assertEqual(sorted(e["address"] for e in mp.view()["terminals"]), ["node-b/t4", "t1"])

    def test_empty_or_stale_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ADW_STATE_DIR": d}):
            out = Path(d) / "terminals.json"
            terminals.write(out, {"written_at": T, "machines": {}, "terminals": []})
            self.assertIsNone(terminals.agents(None, now=T))
            terminals.write(out, {"written_at": T, "machines": {"local": {"connected": True, "last_seen_at": T}},
                                  "terminals": [terminals.entry(rec("t1"), "s")]})
            self.assertIsNone(terminals.agents(None, now=T + 500))
            self.assertEqual(len(terminals.agents(None, now=T + 5)), 1)


class TestBoard(unittest.TestCase):
    def test_blocked_msg_is_needs_text(self):
        mp = terminals.TerminalMap()
        run(mp, [snap(rec("t1", "blocked", "Allow rm -rf?", "permission"))], now=terminals.time.time())
        with mock.patch.object(board, "LIVE", mp), mock.patch.object(board.asks.mux, "read", return_value=("screen", None)):
            agents = board.load_agents([])
        self.assertEqual((agents[0]["attention"], agents[0]["summary"]), ("blocked", "Allow rm -rf?"))
        needs = board.needs_you({"agents": agents, "snoozes": {}})
        self.assertEqual(needs[0]["title"], "Allow rm -rf?")

    def test_stale_machine_is_unknown_never_idle(self):
        mp = terminals.TerminalMap()
        run(mp, [snap(rec("t1"))], now=terminals.time.time() - 500)
        with mock.patch.object(board, "LIVE", mp), mock.patch.object(board.asks.mux, "read", return_value=("", None)):
            self.assertEqual(board.load_agents([])[0]["attention"], "unknown")


class TestWatchdogFallback(unittest.TestCase):
    def test_stale_machine_uses_ls_and_seen_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ADW_STATE_DIR": d}):
            out, seen = Path(d) / "terminals.json", Path(d) / "watchdog-seen.json"
            run(terminals.TerminalMap(out), [snap(rec("t1"), rec("node-a/t2"))], now=T)
            view = terminals.load()
            view["machines"]["node-a"]["last_seen_at"] = T - 1000
            terminals.write(out, view)
            ls = {"terminals": [{"address": "node-a/t2", "agent": "claude-code", "status": "working", "cwd": "/y", "name": "n"},
                                {"address": "t1", "agent": "claude-code", "status": "idle", "cwd": "/x", "name": "n"}]}
            with mock.patch.object(terminals.mux, "hc_json", return_value=(ls, None)) as h:
                rows = terminals.agents(None, now=T + 10)
                h.assert_called_once_with("ls")
                by = {r["pane_id"]: r for r in rows}
                self.assertEqual((by["node-a/t2"]["agent_status"], by["t1"]["agent_status"]), ("working", "idle"))
                self.assertEqual(by["node-a/t2"]["changed_at"], T + 10)
                self.assertEqual(json.loads(seen.read_text())["node-a/t2"], {"state": "working", "since": T + 10})
                rows = terminals.agents(None, now=T + 99)
                self.assertEqual({r["pane_id"]: r for r in rows}["node-a/t2"]["changed_at"], T + 10)


if __name__ == "__main__":
    unittest.main()
