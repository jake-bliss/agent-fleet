import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402
from test_stall import D, H, Hub, T, seg  # noqa: E402


class TestLeadNow(unittest.TestCase):
    def info(self, table):
        return mock.patch.object(board.asks.mux, "get", lambda p: table.get(p))

    def test_finds_lead_by_session_at_new_pane(self):
        with mock.patch.object(board.asks.mux, "terminal_for_session", lambda s: "t99"), \
                self.info({"t99": {"status": "idle", "session": "s1"}, "t12": {"status": "idle", "session": "other"}}):
            self.assertEqual(board.lead_now("s1", "t12"), ("t99", "idle"))

    def test_session_mismatch_is_none(self):
        with mock.patch.object(board.asks.mux, "terminal_for_session", lambda s: None), \
                self.info({"t12": {"status": "idle", "session": "other"}}):
            self.assertIsNone(board.lead_now("s1", "t12"))

    def test_without_session_record_falls_back_to_registered_pane(self):
        with mock.patch.object(board.asks.mux, "terminal_for_session", lambda s: None), \
                self.info({"t12": {"status": "done", "session": "s1"}}):
            self.assertEqual(board.lead_now("s1", "t12"), ("t12", "done"))

    def test_missing_terminal_is_none(self):
        with mock.patch.object(board.asks.mux, "terminal_for_session", lambda s: None), self.info({}):
            self.assertIsNone(board.lead_now("s1", "t12"))


class TestStallRedispatchGlue(unittest.TestCase):
    def setUp(self):
        self.h = Hub()
        self.addCleanup(self.h.close)
        self.h.ask("d-aaaaaa", at=T - 3 * H)
        self.h.epic("e1", [seg("A", "building", note="per d-aaaaaa")], age=1 * D, decisions="")
        self.h.events([("fleet.segment_blocked", "e1", "Z", T - 5 * D)])
        self.prompts = []
        self.mate_status = "idle"

        def get(p):
            if p == "mate":
                return {"status": self.mate_status, "session": "m"}
            return {"status": "idle", "session": "sess-e1"}

        for p in (mock.patch.object(board, "EPICS", self.h.root / "epics"),
                  mock.patch.object(board.asks.mux, "terminal_for_session", lambda s: None),
                  mock.patch.object(board.asks.mux, "get", get),
                  mock.patch.object(board.asks, "agent_info", get),
                  mock.patch.object(board.asks, "prompt", lambda p, t, guard=None: self.prompts.append(p) or (None, None))):
            p.start()
            self.addCleanup(p.stop)

    def journal(self):
        db = sqlite3.connect(self.h.root / "events.db")
        try:
            return [r[0] for r in db.execute("SELECT outcome FROM redispatch_log ORDER BY ts")]
        finally:
            db.close()

    def test_mate_in_dialog_gets_no_escalation_and_no_row(self):
        board.stall_redispatch({}, "mate", T)
        board.stall_redispatch({}, "mate", T + 3 * H)
        self.mate_status = "blocked"
        board.stall_redispatch({}, "mate", T + 6 * H)
        self.assertEqual(self.journal(), ["sent", "sent"])
        self.assertNotIn("mate", self.prompts)
        self.mate_status = "idle"
        board.stall_redispatch({}, "mate", T + 7 * H)
        self.assertEqual(self.journal(), ["sent", "sent", "escalated"])
        self.assertEqual(self.prompts.count("mate"), 1)

    def test_lead_in_dialog_is_not_prompted(self):
        with mock.patch.object(board.asks.mux, "get", lambda p: {"status": "blocked", "session": "sess-e1"}):
            board.stall_redispatch({}, "mate", T)
        self.assertEqual(self.prompts, [])

    def test_lead_with_unsent_draft_or_asks_is_not_prompted_or_counted(self):
        for att in ("unsent", "blocked", "asks"):
            board.stall_redispatch({"agents": [{"pane": "t12", "attention": att}]}, "mate", T)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.journal(), [])

    def test_mate_with_unsent_draft_gets_no_escalation(self):
        board.stall_redispatch({}, "mate", T)
        board.stall_redispatch({}, "mate", T + 3 * H)
        board.stall_redispatch({"agents": [{"pane": "mate", "attention": "unsent"}]}, "mate", T + 6 * H)
        self.assertNotIn("mate", self.prompts)
        self.assertEqual(self.journal(), ["sent", "sent"])

    def test_mate_draft_still_blocks_escalation_after_for_mate_filters_the_mate(self):
        board.stall_redispatch({}, "mate", T)
        board.stall_redispatch({}, "mate", T + 3 * H)
        full = {"agents": [{"pane": "mate", "session": "mate-s", "cwd": "/x", "attention": "unsent"}]}
        busy = board.busy_panes(full)
        with mock.patch.object(board, "is_mate_session", return_value=True):
            b = board.for_mate(full)
        self.assertEqual(b["agents"], [])
        board.stall_redispatch(b, "mate", T + 6 * H, busy)
        self.assertNotIn("mate", self.prompts)
        self.assertEqual(self.journal(), ["sent", "sent"])


if __name__ == "__main__":
    unittest.main()


class TestRedispatchSendGuard(TestStallRedispatchGlue):
    def setUp(self):
        super().setUp()
        self.typed, self.screens = [], {}
        for p in (mock.patch.object(board.asks, "prompt", lambda pane, text, guard=None: board.asks.mux.prompt(pane, text, guard=guard)),
                  mock.patch.object(board.asks.mux, "read", lambda pane, n: (self.screens.get(pane, "⏺ done\n❯"), None)),
                  mock.patch.object(board.asks.mux, "terminal_lock", mock.MagicMock()),
                  mock.patch.object(board.asks.mux, "type_text", lambda pane, text: self.typed.append(pane) or self.prompts.append(pane) or ("", None))):
            p.start()
            self.addCleanup(p.stop)

    def test_lead_draft_typed_after_the_board_read_is_not_overwritten_and_no_try_recorded(self):
        self.screens["t12"] = "⏺ done\n❯ the human is mid-reply"
        board.stall_redispatch({}, "mate", T)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.journal(), [])

    def test_clean_lead_is_typed(self):
        board.stall_redispatch({}, "mate", T)
        self.assertEqual(self.typed, ["t12"])
        self.assertEqual(self.journal(), ["sent"])

    def test_mate_draft_refuses_escalation(self):
        board.stall_redispatch({}, "mate", T)
        board.stall_redispatch({}, "mate", T + 3 * H)
        self.screens["mate"] = "⏺ done\n❯ half a thought"
        board.stall_redispatch({}, "mate", T + 6 * H)
        self.assertNotIn("mate", self.typed)
        self.assertEqual(self.journal(), ["sent", "sent"])
