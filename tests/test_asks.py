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
            mock.patch.object(asks, "ships_without_ask", lambda options: None),
            mock.patch("time.sleep", lambda s: None),
            mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t13", "ADW_HC_MACHINE": "node-b"}),
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

    def test_scan_hit_in_body_only_does_not_gate_a_non_gated_class(self):
        i, _, _ = self.new("--title", "Proceed?", "--body", "this runs a Migration", "--class", "reversible", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "answered")

    def test_scan_hit_in_body_of_unclassified_ask_still_gates(self):
        self.assertIsNotNone(asks.gate_reason({"title": "Proceed?", "body": "this runs a Migration", "options": []}))

    def test_scan_hit_via_option_label_only(self):
        i, _, _ = self.new("--title", "Proceed?", "--class", "reversible", "--recommend", "a",
                           "--option", "a=squash the branch", "--option", "b=wait")
        d = asks.load(i)
        self.assertEqual(d["status"], "open")
        self.assertIn("squash", d["gated_reason"])

    def test_ship_token_in_non_shape_class_refused(self):
        i, err, _ = self.new("--title", "Proceed?", "--class", "reversible", "--recommend", "a",
                             "--option", "a=ship acme#123@abc", "--option", "b=wait")
        self.assertIsNone(i)
        self.assertIn("can't carry a PR merge", err)

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
        i, _, _ = self.new("--title", "Ship as one PR? touches prod data", "--class", "size", "--recommend", "a", *OPTS)
        d = asks.load(i)
        self.assertEqual(d["status"], "open")
        self.assertIn("prod", d["gated_reason"])

    def test_design_class_merge_wording_still_gates(self):
        i, _, _ = self.new("--title", "Merge duplicate customer accounts", "--class", "design", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "open")

    def test_ship_token_gates_every_pr_shape_class(self):
        for c in asks.PR_SHAPE_CLASSES:
            i, _, _ = self.new("--title", f"Proceed {c}?", "--class", c, "--recommend", "a",
                               "--option", "a=ship org#123@abc1234", "--option", "b=wait")
            self.assertEqual(asks.load(i)["status"], "open", c)

    def test_auto_rescan_for_size_class_blocks_prod(self):
        d = {"id": "d-abcdef", "created": 1, "status": "open", "title": "Deploy to prod", "epic": "e", "body": "",
             "class": "size", "options": [{"key": "a", "label": "x"}], "recommend": "a", "pane": "node-b/t13",
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
            i, err, _ = self.new("--title", p, "--class", "reversible", "--recommend", "a", *OPTS)
            if i is None:
                self.assertIn("can't carry a PR merge", err, p)
            else:
                self.assertEqual(asks.load(i)["status"], "open", p)

    def test_danger_phrasings_gate_every_class(self):
        for p in ("Run payouts", "Rotate the API key", "api-key reset", "Reset password", "Purge old rows", "Wipe the table",
                  "Truncate events", "Teardown the stack", "tear-down EU", "Void invoice 12", "Issue credit to buyer",
                  "Grant admin access", "Disable MFA for user"):
            for cls in ("reversible", "size"):
                i, _, _ = self.new("--title", p, "--class", cls, "--recommend", "a", *OPTS)
                self.assertEqual(asks.load(i)["status"], "open", (p, cls))

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
             "pane": "node-b/t13", "session": None, "default_at": 5, **kw}
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
        self.assertEqual(asks.load(i)["answer"]["by"], "agent:node-b/t13")

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




class TestShipWithoutAsk(Base):
    ARGS = ("--title", "Ship it?", "--class", "merge", "--option", "a=ship acme#123@abc1234", "--option", "b=hold")

    def test_merge_ask_refused_when_plain_pr_ship_would_act(self):
        line = "acme#123 dry-run: would squash-merge (review bot approved current head, CI green, CLEAN)"
        with mock.patch.object(asks, "ships_without_ask", lambda options: line):
            i, err, _ = self.new(*self.ARGS)
        self.assertIsNone(i)
        self.assertIn("no ask needed", err)
        self.assertEqual(asks.all_asks(), [])

    def test_merge_ask_filed_when_pr_ship_refuses(self):
        i, _, _ = self.new(*self.ARGS)
        self.assertEqual(asks.load(i)["status"], "open")

    def test_other_gated_classes_skip_the_check(self):
        with mock.patch.object(asks, "ships_without_ask", side_effect=AssertionError("called")):
            i, _, _ = self.new("--title", "Flip it?", "--class", "auth", "--option", "a=ship acme#123@abc1234", "--option", "b=hold")
        self.assertEqual(asks.load(i)["status"], "open")


class TestShipsWithoutAskProbe(unittest.TestCase):
    OPTS = [{"key": "a", "label": "ship acme#123@abc1234 now"}]

    def run_probe(self, rc, out, exe="/bin/pr-ship"):
        res = mock.Mock(returncode=rc, stdout=out)
        with mock.patch.object(asks, "ship_repos", lambda: {"acme": "org/acme"}), mock.patch("shutil.which", lambda n: exe), mock.patch("subprocess.run", return_value=res) as run:
            return asks.ships_without_ask(self.OPTS), run

    def test_would_merge_is_reported_with_full_repo(self):
        got, run = self.run_probe(0, "acme#123 dry-run: would squash-merge (x)\n")
        self.assertIn("would squash-merge", got)
        self.assertEqual(run.call_args[0][0], ["/bin/pr-ship", "123", "-R", "org/acme", "--dry-run"])

    def test_refuse_wait_and_done_are_not_reported(self):
        for rc, out in ((4, "acme#123 refuse: needs the human"), (3, "acme#123 wait: CI"), (0, "acme#123 done: PR is merged; nothing to do")):
            self.assertIsNone(self.run_probe(rc, out)[0])

    def test_errors_and_missing_tool_fail_open(self):
        with mock.patch("shutil.which", lambda n: "/bin/pr-ship"), mock.patch("subprocess.run", side_effect=OSError):
            self.assertIsNone(asks.ships_without_ask(self.OPTS))
        self.assertIsNone(self.run_probe(0, "acme#123 dry-run: would squash-merge", exe=None)[0])

    def test_unknown_repo_is_not_probed(self):
        with mock.patch("shutil.which", lambda n: "/bin/pr-ship"), mock.patch("subprocess.run") as run:
            self.assertIsNone(asks.ships_without_ask([{"key": "a", "label": "ship mystery#5@abc1234"}]))
        run.assert_not_called()


class TestNonGatedHint(Base):
    def test_gate_word_in_non_gated_ask_explains_why_it_stays_open(self):
        i, err, _ = self.new("--title", "Drop the old column?", "--class", "design", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "open")
        self.assertIn("stays open for the human", err)
        self.assertIn("'Drop'", err)


    def test_merge_wording_in_fix_streak_ask_is_refused(self):
        i, err, _ = self.new("--title", "Round 2 findings", "--class", "fix-streak", "--recommend", "a",
                             "--option", "a=one more patch round, then ship acme#12@abc1234", "--option", "b=park")
        self.assertIsNone(i)
        self.assertIn("can't carry a PR merge", err)
        self.assertEqual(list(asks.ASKS.glob("d-*.json")), [])

    def test_merge_wording_in_size_ask_still_allowed(self):
        i, _, _ = self.new("--title", "Over size, ship as one PR?", "--class", "size", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "answered")

    def test_domain_merge_wording_still_files(self):
        i, _, _ = self.new("--title", "Merge duplicate customer accounts", "--class", "design", "--recommend", "a", *OPTS)
        self.assertEqual(asks.load(i)["status"], "open")


class TestRelocate(unittest.TestCase):
    def test_answer_follows_session_to_new_pane(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(asks, "ASKS", Path(tmp)), \
                mock.patch.object(asks, "agent_info", lambda p: {"status": "idle", "session": "s1"} if p == "t9" else {"status": "idle", "session": "other"}), \
                mock.patch.object(asks.mux, "terminal_for_session", lambda s: "t9"), \
                mock.patch.object(asks, "seen_by_agent", lambda d: False), \
                mock.patch.object(asks, "prompt", lambda p, t: sent.append(p) or (None, None)), \
                mock.patch("time.sleep", lambda s: None):
            sent = []
            d = {"id": "d-abcdef", "created": 1, "status": "answered", "title": "t", "epic": "e", "options": [{"key": "a", "label": "x"}],
                 "pane": "t1", "session": "s1", "answer": {"choice": "a", "at": 2, "by": "human"}}
            asks.save(d)
            got = asks.deliver(d)
            self.assertEqual(sent, ["t9"])
            self.assertEqual(got["pane"], "t9")
            self.assertFalse(got.get("undeliverable"))


if __name__ == "__main__":
    unittest.main()
