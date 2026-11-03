import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import asks  # noqa: E402

OPTS = ["--option", "a=do the small thing", "--option", "b=do the big thing"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent = []
        patches = [
            mock.patch.object(asks, "ASKS", self.tmp),
            mock.patch.object(asks, "agent_info", lambda p: {"status": "idle", "session": "s1"}),
            mock.patch.object(asks, "seen_by_agent", lambda d: True),
            mock.patch.object(asks, "prompt", self.prompt),
            mock.patch("time.sleep", lambda s: None),
            mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t13", "ADW_HC_MACHINE": "dell"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def prompt(self, pane, text):
        self.sent.append(("agent", "prompt", pane, text))
        return "", None

    def new(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["adw-ask", "new", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                asks.main()
            except SystemExit as e:
                return None, str(e) if e.code else "", out.getvalue()
        return out.getvalue().split()[0], err.getvalue(), out.getvalue()

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["adw-ask", *argv]), contextlib.redirect_stdout(out):
            asks.main()
        return out.getvalue()


class TestNew(Base):
    def test_reversible_with_recommend_auto_applies_and_delivers(self):
        i, err, _ = self.new("--title", "Split the segment?", "--epic", "e", "--class", "split", "--recommend", "a", *OPTS)
        d = asks.load(i)
        self.assertEqual((d["status"], d["answer"]["by"], d["answer"]["choice"]), ("answered", "auto", "a"))
        self.assertIn("auto-applied a", err)
        self.assertTrue(d["delivered"])
        self.assertIn("Auto-applied", self.sent[-1][-1])

    def test_aliases_stored_as_given(self):
        for c in ("design", "fix-streak", "size", "sequencing", "redesign", "reversible"):
            i, _, _ = self.new("--title", f"t {c}", "--class", c, "--recommend", "a", *OPTS)
            self.assertEqual(asks.load(i)["class"], c)
            self.assertEqual(asks.load(i)["status"], "answered")

    def test_non_gated_without_recommend_stays_open(self):
        i, _, _ = self.new("--title", "Which way?", "--class", "design", *OPTS)
        self.assertEqual(asks.load(i)["status"], "open")

    def test_gated_classes_stay_open(self):
        for c in asks.GATED:
            i, _, _ = self.new("--title", f"Decide {c}", "--class", c, "--recommend", "a", *OPTS)
            self.assertEqual(asks.load(i)["status"], "open", c)

    def test_unknown_class_errors(self):
        i, err, _ = self.new("--title", "x", "--class", "whatever", *OPTS)
        self.assertIsNone(i)
        self.assertIn("unknown --class", err)

    def test_scan_hit_in_title_forces_gated(self):
        i, _, _ = self.new("--title", "Merge the PR?", "--class", "reversible", "--recommend", "a", *OPTS)
        d = asks.load(i)
        self.assertEqual(d["status"], "open")
        self.assertIn("merge", d["gated_reason"].lower())

    def test_scan_hit_in_body_forces_gated(self):
        i, _, _ = self.new("--title", "Proceed?", "--body", "this runs a Migration", "--class", "reversible", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "open")

    def test_scan_hit_via_option_label_only(self):
        i, _, _ = self.new("--title", "Proceed?", "--class", "reversible", "--recommend", "a",
                           "--option", "a=ship org#123@abc", "--option", "b=wait")
        d = asks.load(i)
        self.assertEqual(d["status"], "open")
        self.assertIn("ship org#123", d["gated_reason"])

    def test_missing_class_refused_and_nothing_saved(self):
        i, err, _ = self.new("--title", "Pick one", "--recommend", "a", *OPTS)
        self.assertIsNone(i)
        self.assertIn("--class is required", err)
        self.assertIn("merge", err)
        self.assertIn("size", err)
        self.assertEqual(asks.all_asks(), [])

    def test_size_ask_with_ship_wording_auto_applies(self):
        i, _, _ = self.new("--title", "991 lines: waive cap as one PR or split?", "--class", "size", "--recommend", "a",
                           "--option", "a=ship as one PR", "--option", "b=split and merge separately")
        d = asks.load(i)
        self.assertEqual((d["status"], d["answer"]["by"]), ("answered", "auto"))

    def test_size_ask_mentioning_prod_still_gated(self):
        i, _, _ = self.new("--title", "Ship as one PR?", "--body", "touches prod data", "--class", "size", "--recommend", "a", *OPTS)
        d = asks.load(i)
        self.assertEqual(d["status"], "open")
        self.assertIn("prod", d["gated_reason"])

    def test_design_class_merge_wording_still_gates(self):
        i, _, _ = self.new("--title", "Merge duplicate customer accounts", "--class", "design", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "open")

    def test_ship_token_gates_every_pr_shape_class(self):
        for c in asks.NON_GATED:
            i, _, _ = self.new("--title", f"Proceed {c}?", "--class", c, "--recommend", "a",
                               "--option", "a=ship org#123@abc1234", "--option", "b=wait")
            self.assertEqual(asks.load(i)["status"], "open", c)

    def test_auto_rescan_for_size_class_blocks_prod(self):
        d = {"id": "d-abcdef", "created": 1, "status": "open", "title": "Deploy to prod", "epic": "e", "body": "",
             "class": "size", "options": [{"key": "a", "label": "x"}], "recommend": "a", "pane": "dell/t13",
             "session": None, "default_at": None}
        asks.save(d)
        with self.assertRaises(ValueError):
            asks.record_answer("d-abcdef", "a", by="auto")

    def test_default_after_refused_for_gated_class(self):
        i, err, _ = self.new("--title", "Pay out", "--class", "money", "--recommend", "a", "--default-after", "4h", *OPTS)
        self.assertIsNone(i)
        self.assertIn("refused", err)

    def test_default_after_refused_when_scan_hits(self):
        i, err, _ = self.new("--title", "Deploy now?", "--class", "design", "--recommend", "a", "--default-after", "4h", *OPTS)
        self.assertIsNone(i)
        self.assertIn("refused", err)

    def test_default_after_allowed_when_clean(self):
        i, _, _ = self.new("--title", "Pick one", "--class", "design", "--recommend", "a", "--default-after", "4h", *OPTS)
        d = asks.load(i)
        self.assertEqual(d["status"], "answered")
        self.assertTrue(d["default_at"])


class TestGateWidening(Base):
    def test_option_key_alone_gates(self):
        i, _, _ = self.new("--title", "Proceed?", "--class", "reversible", "--recommend", "merge",
                           "--option", "merge=Go", "--option", "wait=Hold")
        self.assertEqual(asks.load(i)["status"], "open")

    def test_merge_phrasings_gate(self):
        for p in ("Ship PR #123?", "ship #123", "pr-ship 123", "Land org#123", "squash it", "drop", "publish release",
                  "Merging now", "cut a release"):
            i, _, _ = self.new("--title", p, "--class", "reversible", "--recommend", "a", *OPTS)
            self.assertEqual(asks.load(i)["status"], "open", p)

    def test_already_answered_race(self):
        real = asks.record_answer

        def raced(i, *a, **k):
            asks.save({**asks.load(i), "status": "answered"})
            return real(i, *a, **k)

        with mock.patch.object(asks, "record_answer", raced):
            i, err, _ = self.new("--title", "Pick one", "--class", "design", "--recommend", "a", *OPTS)
        self.assertIn("already answered", err)


class TestDefaults(Base):
    def legacy(self, **kw):
        d = {"id": "d-abcdef", "created": 1, "status": "open", "title": "Pick one", "epic": "e", "body": "",
             "options": [{"key": "a", "label": "x"}, {"key": "b", "label": "y"}], "recommend": "a",
             "pane": "dell/t13", "session": None, "default_at": 5, **kw}
        asks.save(d)
        return d

    def test_legacy_clean_record_is_defaulted(self):
        self.legacy()
        asks.apply_defaults()
        d = asks.load("d-abcdef")
        self.assertEqual((d["status"], d["answer"]["by"]), ("answered", "default"))

    def test_legacy_gated_record_not_defaulted_and_flagged(self):
        self.legacy(title="Merge #5 now?")
        asks.apply_defaults()
        d = asks.load("d-abcdef")
        self.assertEqual(d["status"], "open")
        self.assertIn("merge", d["default_blocked"].lower())

    def test_legacy_gated_by_option_label_only(self):
        self.legacy(options=[{"key": "a", "label": "ship org#123@abc"}])
        asks.apply_defaults()
        self.assertEqual(asks.load("d-abcdef")["status"], "open")

    def test_gated_class_record_not_defaulted(self):
        self.legacy(**{"class": "scope"})
        asks.apply_defaults()
        self.assertEqual(asks.load("d-abcdef")["status"], "open")

    def test_auto_rescan_blocks_gated(self):
        self.legacy(default_at=None, title="Delete the table?")
        with self.assertRaises(ValueError):
            asks.record_answer("d-abcdef", "a", by="auto")
        self.assertEqual(asks.load("d-abcdef")["status"], "open")


class TestBy(Base):
    def make(self):
        i, _, _ = self.new("--title", "Pick one", "--class", "scope", *OPTS)
        return i

    def test_cli_default_is_agent_pane(self):
        i = self.make()
        self.cli("answer", i, "a")
        self.assertEqual(asks.load(i)["answer"]["by"], "agent:dell/t13")

    def test_answer_from_the_asking_tab_is_not_typed_back_into_it(self):
        i = self.make()
        self.sent.clear()
        self.cli("answer", i, "a")
        self.assertTrue(asks.load(i)["delivered"])
        self.assertEqual(self.sent, [])

    def test_cli_without_pane_is_agent_cli(self):
        i = self.make()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HC_TERMINAL_ID")
            self.cli("answer", i, "a")
        self.assertEqual(asks.load(i)["answer"]["by"], "agent:cli")

    def test_relay(self):
        i = self.make()
        self.cli("answer", i, "a", "--relay", "go with a, he said")
        a = asks.load(i)["answer"]
        self.assertEqual((a["by"], a["relay"]), ("relay:human", "go with a, he said"))
        self.assertIn("the human chose (relayed", self.sent[-1][-1])

    def test_mate(self):
        i = self.make()
        self.cli("answer", i, "a", "--by", "mate")
        self.assertEqual(asks.load(i)["answer"]["by"], "mate")

    def test_http_path_is_human(self):
        i = self.make()
        asks.record_answer(i, "a")
        self.assertEqual(asks.load(i)["answer"]["by"], "human")

    def test_old_record_without_by_reads_as_human(self):
        d = {"id": "d-111111", "title": "t", "options": [], "answer": {"choice": "a", "note": "", "at": 1}}
        self.assertIn("the human chose", asks.answer_message(d))


class TestDedupe(Base):
    def test_exact_duplicate_returns_existing(self):
        a, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", "--body", "same", *OPTS)
        b, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", "--body", "same", *OPTS)
        self.assertEqual(a, b)
        self.assertEqual(len(asks.all_asks()), 1)

    def test_different_options_or_body_is_new(self):
        a, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", "--body", "same", *OPTS)
        b, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", "--body", "same", "--option", "a=do the small thing", "--option", "b=other")
        c, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", "--body", "new head", *OPTS)
        self.assertEqual(len({a, b, c}), 3)

    def test_different_class_recommend_keys_or_session_is_new(self):
        base = ["--title", "Pick one", "--class", "scope", "--epic", "e", "--body", "same"]
        a, _, _ = self.new(*base, "--class", "scope", *OPTS)
        b, _, _ = self.new(*base, "--class", "money", *OPTS)
        c, _, _ = self.new(*base, "--class", "scope", "--recommend", "a", *OPTS)
        d, _, _ = self.new(*base, "--class", "scope", "--option", "x=do the small thing", "--option", "y=do the big thing")
        with mock.patch.object(asks, "agent_info", lambda p: {"status": "idle", "session": "other"}):
            e, _, _ = self.new(*base, "--class", "scope", *OPTS)
        self.assertEqual(len({a, b, c, d, e}), 5)

    def test_answered_duplicate_is_not_reused(self):
        a, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", *OPTS)
        asks.record_answer(a, "a")
        b, _, _ = self.new("--title", "Pick one", "--class", "scope", "--epic", "e", *OPTS)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
