"""Unit tests for pr-state --failures extraction, pr-reply ref parsing and pr-ship decisions."""
import importlib.machinery
import importlib.util
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin")


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(HERE, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ps = load("pr-state")
ship = load("pr-ship")
reply = load("pr-reply")

TEST_CONFIG = {
    "our_authors": ["me-dev"],
    "repos": {
        "acme/app": {"bot": "review-bot", "merge": "bot"},
        "acme/api": {"bot": "review-bot", "merge": "bot"},
        "acme/site": {"merge": "green"},
    },
}
ps.CONFIG = ship.ps.CONFIG = TEST_CONFIG

LOG = "\n".join([
    "2026-10-13T10:00:00.1234567Z \x1b[36mRunning tests\x1b[0m",
    "2026-10-13T10:00:01.0000000Z   1) Failure:",
    "2026-10-13T10:00:01.0000000ZFooTest#test_bar [test/foo_test.rb:12]:",
    "2026-10-13T10:00:01.0000000ZExpected: 1",
    "2026-10-13T10:00:01.0000000Z  Actual: 2",
    "2026-10-13T10:00:01.0000000Z",
    "2026-10-13T10:00:02.0000000Z   2) Error:",
    "2026-10-13T10:00:02.0000000ZBazTest#test_q:",
    "2026-10-13T10:00:02.0000000ZNoMethodError: undefined method 'x'",
    "2026-10-13T10:00:02.0000000Z    app/models/baz.rb:9:in 'm'",
    "",
    "app/models/user.rb:10:5: C: Style/StringLiterals: Prefer single quotes",
    "app/models/user.rb:10:5: C: Style/StringLiterals: Prefer single quotes",
    "src/a.ts(3,4): error TS2322: Type 'x' is not assignable",
    "FAIL src/a.test.ts",
    "  ● thing > works",
    "##[error]Process completed with exit code 1.",
    "irrelevant noise",
])


def info(**over):
    base = {
        "state": "open", "draft": False, "head": "abc",
        "ci": {"total": 5, "ok": 5, "pending": 0, "failed": []},
        "bot": {"approved": True, "verdict_head": "abc", "high_risk": False},
        "merge_state": "CLEAN", "author": "me-dev", "base": "main", "default_branch": "main",
        "changes_requested": [],
    }
    base.update(over)
    return base


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.lines, self.trunc = ps.extract_failures(LOG)

    def test_strips_prefix_and_ansi(self):
        self.assertFalse(any("\x1b" in l or l.startswith("2026-") for l in self.lines))

    def test_minitest_blocks(self):
        self.assertIn("FooTest#test_bar [test/foo_test.rb:12]:", self.lines)
        self.assertIn(" Actual: 2", self.lines)
        self.assertIn("NoMethodError: undefined method 'x'", self.lines)

    def test_other_formats(self):
        joined = "\n".join(self.lines)
        for needle in ("Style/StringLiterals", "error TS2322", "FAIL src/a.test.ts", "● thing", "##[error]Process"):
            self.assertIn(needle, joined)
        self.assertNotIn("irrelevant noise", joined)

    def test_dedupe(self):
        self.assertEqual(sum("Style/StringLiterals" in l for l in self.lines), 1)

    def test_cap(self):
        lines, trunc = ps.extract_failures(LOG, 3)
        self.assertEqual(len(lines), 3)
        self.assertEqual(trunc, len(self.lines) - 3)

    def test_rspec_block_gets_title(self):
        lines, _ = ps.extract_failures("  1) Foo does x\n     Failure/Error: expect(1).to eq(2)\n       expected: 2\n")
        self.assertEqual(lines[0], "  1) Foo does x")

    def test_job_url(self):
        m = ps.JOB_URL.search("https://github.com/o/r/actions/runs/11/job/22")
        self.assertEqual(m.groups(), ("11", "22"))


def check(name, status="COMPLETED", conclusion="SUCCESS", started="2026-01-01T00:00:00Z", wf="W", id=1):
    return {"__typename": "CheckRun", "databaseId": id, "name": name, "status": status, "conclusion": conclusion,
            "detailsUrl": "u", "startedAt": started, "checkSuite": {"workflowRun": {"workflow": {"name": wf}}}}


class ChangesRequestedTest(unittest.TestCase):
    def lr(self, nodes, more=False):
        return {"pageInfo": {"hasNextPage": more}, "nodes": nodes}

    def test_latest_changes_requested(self):
        nodes = [{"author": {"login": "a"}, "state": "CHANGES_REQUESTED"}, {"author": {"login": "b"}, "state": "APPROVED"}]
        self.assertEqual(ps.changes_requested(self.lr(nodes)), ["a"])

    def test_none(self):
        self.assertEqual(ps.changes_requested(self.lr([{"author": {"login": "b"}, "state": "APPROVED"}])), [])

    def test_commented_after_changes_requested_still_blocks(self):
        query = ps.PR_FIELDS
        self.assertIn("latestOpinionatedReviews", query)
        self.assertNotIn("latestReviews(", query)

    def test_build_reads_opinionated_reviews(self):
        pr = {"isDraft": False, "state": "OPEN", "headRefOid": "h", "merged": False, "mergeStateStatus": "CLEAN",
              "title": "t", "url": "u", "author": {"login": "me-dev"}, "baseRefName": "main",
              "_default_branch": "main", "reviewDecision": None,
              "latestReviews": self.lr([{"author": {"login": "a"}, "state": "COMMENTED"}]),
              "latestOpinionatedReviews": self.lr([{"author": {"login": "a"}, "state": "CHANGES_REQUESTED"}])}
        conns = {"contexts": {"nodes": []}, "reviews": {"nodes": []}, "comments": {"nodes": []},
                 "reviewThreads": {"nodes": []}}
        with mock.patch.object(ps, "fetch", lambda r, n: (pr, conns)):
            self.assertEqual(ps.build("acme/site", 1)["changes_requested"], ["a"])

    def test_truncated_or_missing_fails_closed(self):
        self.assertTrue(ps.changes_requested(self.lr([], more=True)))
        self.assertTrue(ps.changes_requested(None))


class CiTest(unittest.TestCase):
    def test_queued_rerun_is_newest(self):
        old, rerun = check("a", id=1), check("a", status="QUEUED", conclusion=None, started=None, id=2)
        self.assertEqual(ps.analyze_ci([old, rerun])["pending"], 1)
        self.assertEqual(ps.analyze_ci([rerun, old])["pending"], 1)

    def test_undated_older_check_does_not_hide_newer_failure(self):
        old = check("a", conclusion="SKIPPED", started=None, id=1)
        new = check("a", conclusion="FAILURE", started="2026-01-02T00:00:00Z", id=2)
        for order in ([old, new], [new, old]):
            self.assertEqual(len(ps.analyze_ci(order)["failed"]), 1)

    def test_undated_cancelled_does_not_block_successful_rerun(self):
        old = check("a", conclusion="CANCELLED", started=None, id=1)
        new = check("a", id=2)
        for order in ([old, new], [new, old]):
            ci = ps.analyze_ci(order)
            self.assertEqual((ci["ok"], ci["failed"]), (1, []))

    def test_status_context_latest_by_created_at(self):
        a = {"__typename": "StatusContext", "context": "s", "state": "FAILURE", "targetUrl": "u", "createdAt": "2026-01-01T00:00:00Z"}
        b = dict(a, state="SUCCESS", createdAt="2026-01-02T00:00:00Z")
        for order in ([a, b], [b, a]):
            self.assertEqual(ps.analyze_ci(order)["ok"], 1)

    def test_nonpass(self):
        ci = ps.analyze_ci([check("a"), check("b", conclusion="SKIPPED"), check("c", conclusion="STALE"),
                            check("d", conclusion="NEUTRAL")])
        self.assertEqual(ci["nonpass"], ["c"])
        self.assertEqual(ci["ok"], 3)


class FailClosedConfigTest(unittest.TestCase):
    def test_missing_config_refuses_everything(self):
        with mock.patch.dict(os.environ, {"PR_TOOLS_CONFIG": "/nonexistent/pr-tools.json"}):
            self.assertEqual(load("pr-state").CONFIG, {})
        saved = ship.ps.CONFIG
        ship.ps.CONFIG = {}
        try:
            for repo in ("acme/app", "acme/api", "acme/site", "other/thing"):
                self.assertEqual(ship.decide(info(), repo)[0], "refuse")
        finally:
            ship.ps.CONFIG = saved

    def test_unlisted_author_refuses(self):
        self.assertEqual(ship.decide(info(author="stranger"), "acme/site")[0], "refuse")


class BotDecideTest(unittest.TestCase):
    F = "acme/app"

    def rv(self, login="review-bot", state="APPROVED", body="Review bot: Auto-approved 5/5", oid="h", typename="Bot"):
        return {"author": {"login": login, "__typename": typename}, "state": state, "body": body, "commit": {"oid": oid}}

    def act(self, reviews, comments=()):
        pr = {"isDraft": False, "state": "OPEN", "headRefOid": "h"}
        fp = ps.analyze_bot(pr, {"reviews": {"nodes": list(reviews)}, "comments": {"nodes": list(comments)}}, self.F)
        return ship.decide(info(head="h", bot=fp), self.F)[0]

    def test_approved_on_head_merges(self):
        self.assertEqual(self.act([self.rv()]), "merge")
        self.assertEqual(self.act([self.rv(), self.rv(state="COMMENTED", body="")]), "merge")

    def test_other_login_does_not_count(self):
        self.assertEqual(self.act([self.rv(login="other-bot")]), "wait")
        self.assertEqual(self.act([self.rv(state="COMMENTED"), self.rv(login="other-bot")]), "wait")

    def test_old_commit_does_not_count(self):
        self.assertEqual(self.act([self.rv(oid="old")]), "wait")

    def test_unprefixed_body_does_not_count(self):
        self.assertEqual(self.act([self.rv(body="LGTM")]), "wait")

    def test_triage_human_review_refuses(self):
        blob = base64.b64encode(json.dumps({"human_review_required": True}).encode()).decode()
        report = {"author": {"login": "review-bot", "__typename": "Bot"},
                  "body": f"{ps.report_marker()}\n<!-- review-bot:head=h -->\n<!-- review-bot:triage={blob} -->"}
        fp = ps.analyze_bot({"isDraft": False, "state": "OPEN", "headRefOid": "h"},
                                   {"reviews": {"nodes": [self.rv()]}, "comments": {"nodes": [report]}}, self.F)
        self.assertTrue(fp["high_risk"], fp)
        self.assertEqual(ship.decide(info(head="h", bot=fp), self.F)[0], "refuse")

    def test_high_risk_phrase_refuses(self):
        report = {"author": {"login": "review-bot", "__typename": "Bot"}, "body": f"{ps.report_marker()}\nhuman review is required"}
        self.assertEqual(self.act([self.rv()], [report]), "refuse")


class BotTest(unittest.TestCase):
    def verdict(self, states):
        reviews = [{"author": {"login": "review-bot", "__typename": "Bot"}, "state": s, "body": "Review bot: x", "commit": {"oid": "h"}}
                   for s in states]
        pr = {"isDraft": False, "state": "OPEN", "headRefOid": "h"}
        conns = {"reviews": {"nodes": reviews}, "comments": {"nodes": []}}
        return ps.analyze_bot(pr, conns, "acme/app")

    def test_latest_wins(self):
        self.assertTrue(self.verdict(["CHANGES_REQUESTED", "APPROVED"])["approved"])
        self.assertFalse(self.verdict(["APPROVED", "CHANGES_REQUESTED"])["approved"])


class FailuresTest(unittest.TestCase):
    def test_fetch_error_exit_1(self):
        info_ = {"head": "abcdef0", "ci": {"failed": [{"name": "j", "url": "https://x/actions/runs/1/job/2"}]}}
        def boom(repo, job):
            raise ps.PrError("HTTP 404")
        import io, contextlib
        buf = io.StringIO()
        with mock.patch.object(ps, "build", lambda repo, number, runs=False: info_), \
                mock.patch.object(ps, "job_log", boom), contextlib.redirect_stdout(buf):
            code = ps.print_failures("o/r", 1, 60)
        self.assertEqual(code, 1)
        self.assertIn("could not fetch log (HTTP 404)", buf.getvalue())
        self.assertNotIn("no recognizable", buf.getvalue())
        self.assertIn("full log:", buf.getvalue())


class ShipMainTest(unittest.TestCase):
    F = "acme/app"

    def go(self, infos, argv=None):
        calls = []
        seq = iter(infos)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_build(repo, number, runs=False):
            calls.append("fetch")
            return next(seq)

        with mock.patch.object(ship.ps, "build", fake_build), mock.patch.object(ship.subprocess, "run", fake_run), \
                mock.patch.object(ship.time, "sleep"), mock.patch("builtins.print"):
            code = ship.main(["7", "-R", self.F] + (argv or []))
        return code, calls

    def test_merge_argv_and_recheck_order(self):
        code, calls = self.go([info(), info(head="abc")])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["fetch", "fetch",
                                 ["gh", "pr", "merge", "7", "-R", self.F, "--squash", "--match-head-commit", "abc"]])

    def test_ready_argv_and_recheck_order(self):
        code, calls = self.go([info(draft=True), info(draft=True)])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["fetch", "fetch", ["gh", "pr", "ready", "7", "-R", self.F]])

    def test_state_changed_aborts_without_mutation(self):
        code, calls = self.go([info(), info(head="zzz")])
        self.assertEqual(code, 3)
        self.assertEqual(calls, ["fetch", "fetch"])

    def test_refusals_make_no_mutation(self):
        for over, expect in ((dict(author="someone-else"), 4), (dict(base="x"), 4), (dict(merge_state="BLOCKED"), 3)):
            code, calls = self.go([info(**over)])
            self.assertEqual((code, calls), (expect, ["fetch"]))

    def test_green_repo_draft_only_ready(self):
        calls = []
        with mock.patch.object(ship.ps, "build", lambda r, n, runs=False: info(draft=True, merge_state="DRAFT")), \
                mock.patch.object(ship.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or mock.Mock(returncode=0)), \
                mock.patch("builtins.print"):
            code = ship.main(["7", "-R", "acme/site"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["gh", "pr", "ready", "7", "-R", "acme/site"]])

    def test_dry_run_no_mutation(self):
        code, calls = self.go([info()], ["--dry-run"])
        self.assertEqual((code, calls), (0, ["fetch"]))

    def test_merge_failure_exit_5(self):
        with mock.patch.object(ship.ps, "build", lambda r, n, runs=False: info()), \
                mock.patch.object(ship.subprocess, "run", lambda cmd, **kw: mock.Mock(returncode=1, stderr="boom")), \
                mock.patch("builtins.print"):
            self.assertEqual(ship.main(["7", "-R", self.F]), 5)

    def test_unexpected_exception_exit_5(self):
        with mock.patch.object(ship.ps, "build", side_effect=KeyError("x")), mock.patch("builtins.print"):
            self.assertEqual(ship.main(["7", "-R", self.F]), 5)


class ShipExitCodeTest(unittest.TestCase):
    def test_bad_args_exit_error(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(ship.main(["1", "--bogus"]), ship.EXIT_ERROR)

    def test_help_exits_zero(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(ship.main(["--help"]), 0)

    def test_interrupt_exit_error(self):
        def boom(r, n):
            raise KeyboardInterrupt
        with mock.patch.object(ship, "fetch_info", boom):
            self.assertEqual(ship.main(["1", "-R", "acme/app"]), ship.EXIT_ERROR)


class ReplyMainTest(unittest.TestCase):
    def thread(self, resolved=False):
        return {"id": "PRRT_1", "isResolved": resolved, "comments": {"pageInfo": {"hasNextPage": False},
                "nodes": [{"databaseId": 10, "url": "u10"}, {"databaseId": 11, "url": "u11"}]}}

    def test_files_tab_ref(self):
        self.assertEqual(reply.parse_ref("https://github.com/o/r/pull/1/files#r99"), ("comment", 99))

    def test_find_thread_by_reply_id_posts_to_first(self):
        posted = []
        with mock.patch.object(reply, "all_threads", lambda r, n: iter([self.thread()])), \
                mock.patch.object(reply, "post_reply", lambda r, n, cid, body: posted.append(cid) or "url"), \
                mock.patch("builtins.print"):
            code = reply.main(["1", "11", "hi", "-R", "o/r"])
        self.assertEqual((code, posted), (0, [10]))

    def test_paginated_comment_found(self):
        thread = self.thread()
        thread["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c"}
        more = {"node": {"comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{"databaseId": 500, "url": "u"}]}}}
        with mock.patch.object(reply.ps, "gh_graphql", lambda q, v: more):
            with mock.patch.object(reply, "all_threads", lambda r, n: iter([thread])):
                self.assertEqual(reply.find_thread("o/r", 1, "comment", 500)["id"], "PRRT_1")

    def test_resolve_failure_exit_6(self):
        def boom(q, v):
            raise reply.ps.PrError("nope")
        with mock.patch.object(reply, "all_threads", lambda r, n: iter([self.thread()])), \
                mock.patch.object(reply, "post_reply", lambda *a: "url"), \
                mock.patch.object(reply.ps, "gh_graphql", boom), mock.patch("builtins.print") as out:
            code = reply.main(["1", "10", "hi", "-R", "o/r", "--resolve"])
        self.assertEqual(code, 6)
        self.assertIn("resolve failed", out.call_args[0][0])

    def test_reply_failure_exit_5(self):
        def boom(*a):
            raise reply.ps.PrError("x")
        with mock.patch.object(reply, "all_threads", lambda r, n: iter([self.thread()])), \
                mock.patch.object(reply, "post_reply", boom), mock.patch("builtins.print"):
            self.assertEqual(reply.main(["1", "10", "hi", "-R", "o/r"]), 5)


class RenderTest(unittest.TestCase):
    def test_nonpass_render_and_wait(self):
        i = {"state": "open", "draft": False, "repo": "o/r", "number": 1, "head": "abcdef0", "title": "t", "open_threads": 0,
             "ci": {"total": 3, "ok": 2, "pending": 0, "failed": [], "nonpass": ["x"]},
             "bot": {"status": "n/a", "approved": False}, "review_decision": None, "merge_state": "CLEAN"}
        self.assertIn("CI ⚠ nonpass: x", ps.render(i))
        self.assertEqual(ps.condition_status("ci-green", i, ""), "impossible")


class ReplyRefTest(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(reply.parse_ref("https://github.com/o/r/pull/1#discussion_r123"), ("comment", 123))
        self.assertEqual(reply.parse_ref("456"), ("comment", 456))
        self.assertEqual(reply.parse_ref("PRRT_abc"), ("thread", "PRRT_abc"))
        with self.assertRaises(reply.ps.PrError):
            reply.parse_ref("nonsense")


class DecideTest(unittest.TestCase):
    F = "acme/app"

    def act(self, i, repo=None):
        return ship.decide(i, repo or self.F)[0]

    def test_merged(self):
        self.assertEqual(self.act(info(state="merged")), "done")

    def test_closed_unmerged_refuses(self):
        self.assertEqual(self.act(info(state="closed")), "refuse")

    def test_merge_ready(self):
        self.assertEqual(self.act(info()), "merge")
        self.assertEqual(self.act(info(), "acme/api"), "merge")

    def test_not_clean(self):
        self.assertEqual(self.act(info(merge_state="BLOCKED")), "wait")

    def test_unapproved_or_stale(self):
        self.assertEqual(self.act(info(bot={"approved": False, "verdict_head": "abc", "high_risk": False})), "wait")
        self.assertEqual(self.act(info(bot={"approved": True, "verdict_head": "old", "high_risk": False})), "wait")

    def test_ci_not_green(self):
        self.assertEqual(self.act(info(ci={"total": 5, "ok": 4, "pending": 1, "failed": []})), "wait")
        self.assertEqual(self.act(info(ci={"total": 5, "ok": 4, "pending": 0, "failed": [{"name": "x"}]})), "wait")
        self.assertEqual(self.act(info(ci={"total": 0, "ok": 0, "pending": 0, "failed": []})), "wait")

    def test_high_risk(self):
        self.assertEqual(self.act(info(bot={"approved": True, "verdict_head": "abc", "high_risk": True})), "refuse")

    def test_draft(self):
        self.assertEqual(self.act(info(draft=True)), "ready")
        self.assertEqual(self.act(info(draft=True, ci={"total": 5, "ok": 4, "pending": 1, "failed": []})), "wait")

    def test_green_repo(self):
        r = "acme/site"
        self.assertEqual(self.act(info(bot={"approved": False}), r), "merge")
        self.assertEqual(self.act(info(draft=True, merge_state="DRAFT"), r), "ready")
        self.assertEqual(self.act(info(draft=True, changes_requested=["x"]), r), "wait")
        self.assertEqual(self.act(info(changes_requested=["x"]), r), "wait")
        self.assertEqual(self.act(info(merge_state="BEHIND"), r), "wait")

    def test_exact_repo_match(self):
        self.assertEqual(self.act(info(), "acme/site-extra"), "refuse")
        self.assertEqual(self.act(info(), "evil/site"), "refuse")
        self.assertEqual(self.act(info(), "evil/app"), "refuse")

    def test_nonpass_blocks(self):
        ci = {"total": 5, "ok": 4, "pending": 0, "failed": [], "nonpass": ["x"]}
        self.assertEqual(self.act(info(ci=ci)), "wait")

    def test_recheck(self):
        base = info()
        for fresh in (info(head="new"), info(merge_state="BLOCKED")):
            with mock.patch.object(ship, "fetch_info", lambda r, n, f=fresh: f):
                with self.assertRaises(ship.StateChanged):
                    ship.recheck(self.F, 1, base, "merge")
        with mock.patch.object(ship, "fetch_info", lambda r, n: info()):
            ship.recheck(self.F, 1, base, "merge")

    def test_author_allowlist(self):
        self.assertEqual(ship.decide(info(author="someone-else"), self.F)[0], "refuse")
        self.assertIn("not ours: someone-else", ship.decide(info(author="someone-else"), self.F)[1])
        self.assertEqual(self.act(info(author="app/other-bot")), "refuse")
        self.assertEqual(self.act(info(author="other-bot")), "refuse")
        self.assertEqual(self.act(info(author="app/evil")), "refuse")
        self.assertEqual(self.act(info(author="me-dev-fake")), "refuse")

    def test_base_branch(self):
        self.assertEqual(self.act(info(base="feature/x")), "refuse")
        self.assertEqual(self.act(info(base="master", default_branch="main")), "refuse")
        self.assertEqual(self.act(info(default_branch=None)), "refuse")

    def test_other_repos_refused(self):
        self.assertEqual(self.act(info(), "acme/other"), "refuse")
        self.assertEqual(ship.EXIT["refuse"], 4)
        self.assertEqual(ship.EXIT["wait"], 3)


TOKEN = "ship app#7@abcdef0"
HEAD = "abcdef0123"


def ask(label=f"{TOKEN} (merge as is)", title="Merge app#7?", body="", status="answered", by="human", choice="a", other="Hold",
        note=""):
    return {"id": "d-1", "status": status, "title": title, "body": body,
            "options": [{"key": "a", "label": label}, {"key": "b", "label": other}],
            "answer": {"choice": choice, "note": note, "at": 1, "by": by} if status == "answered" else None}


def ainfo(**over):
    return info(**{"head": HEAD, **over})


NO_FP = {"approved": False, "verdict_head": None, "high_risk": False}


class ApprovedByTest(unittest.TestCase):
    F = "acme/app"

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "log.jsonl")

    def check(self, a, repo=None, **over):
        return ship.validate_approval(a, repo or self.F, 7, ainfo(**over))

    def go(self, a, infos=None, argv=None, repo=None, log=None):
        events = []
        base = ainfo(bot=NO_FP)
        seq = iter(infos or [base, base])

        def fake_run(cmd, **kw):
            events.append("merge")
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_log(phase, *rest):
            events.append(phase)
            if log:
                log(phase)

        with mock.patch.object(ship.ps, "build", lambda r, n, runs=False: next(seq)), \
                mock.patch.object(ship.subprocess, "run", fake_run), mock.patch.object(ship, "load_ask", lambda i: a), \
                mock.patch.object(ship, "log_override", fake_log), \
                mock.patch.object(ship.time, "sleep"), mock.patch("builtins.print") as out, mock.patch("sys.stderr"):
            code = ship.main(["7", "-R", repo or self.F, "--approved-by", "d-1"] + (argv or []))
        return code, events, out

    def test_token_accepted(self):
        approval, why = self.check(ask())
        self.assertIsNone(why)
        self.assertEqual(approval["ask"], "d-1")
        self.assertIsNone(self.check(ask(label="ship app#7@abcdef0123"))[1])

    def test_hold_labels_without_token_refused(self):
        for label in ("Hold", "Merge", "A teammate reviews all three current PRs before further merge authorization",
                      "push one PR (merge stays the human's)", "Merge RETIRE after A6c-2 deploys",
                      "merge as is (bot approve 6bc885f) + reconcile-sweep follow-up"):
            self.assertIsNotNone(self.check(ask(label=label))[1], label)

    def test_token_mismatches_refused(self):
        for label in ("ship app#8@abcdef0", "ship api#7@abcdef0", "ship App#7@abcdef0", "ship app#7@abcdef1",
                      "ship app#7@abcde", "ship app#7@ABCDEF0", "ship app#7@abcdef0 and ship app#7@abcdef0def0",
                      "reship app#7@abcdef0", "ship app#70@abcdef0"):
            self.assertIsNotNone(self.check(ask(label=label))[1], label)
        self.assertIsNotNone(ship.validate_approval(ask(), "acme/api", 7, ainfo())[1])

    def test_token_must_lead_and_be_tight(self):
        for label in ("Hold, do not ship app#7@abcdef0", "Hold: ship app#7@abcdef0 later", "do not ship app#7@abcdef0",
                      "ship app#7@abcdef0 then ship app#7@abcdef0", "ship app#7@abcdef0DEAD", "ship app#7@abcdef0g",
                      "ship app#7@abcdef0_x", "ship app#005@abcdef0", "ship app#0@abcdef0"):
            self.assertIsNotNone(self.check(ask(label=label))[1], label)
        self.assertIsNone(self.check(ask(label="  ship app#7@abcdef0 (merge as is)"))[1])

    def test_chosen_option_must_be_unique_and_not_note(self):
        dup = ask()
        dup["options"].append({"key": "a", "label": TOKEN})
        self.assertIsNotNone(self.check(dup)[1])
        note = ask(choice="note")
        note["options"].append({"key": "note", "label": TOKEN})
        self.assertIsNotNone(self.check(note)[1])

    def test_recheck_reloads_ask(self):
        i = ainfo(bot=NO_FP)
        gone = ask(status="withdrawn")
        with mock.patch.object(ship, "fetch_info", lambda r, n: i):
            with mock.patch.object(ship, "load_ask", lambda x: ask()) as _:
                ship.recheck(self.F, 7, i, "merge", None, "d-1")
            with mock.patch.object(ship, "load_ask", lambda x: gone):
                with self.assertRaises(ship.StateChanged):
                    ship.recheck(self.F, 7, i, "merge", None, "d-1")

    def test_withdrawn_between_loads_no_merge(self):
        asks = iter([ask(), ask(status="withdrawn")])
        events = []
        base = ainfo(bot=NO_FP)
        with mock.patch.object(ship.ps, "build", lambda r, n, runs=False: base), \
                mock.patch.object(ship.subprocess, "run", lambda cmd, **kw: events.append(cmd)), \
                mock.patch.object(ship, "load_ask", lambda i: next(asks)), mock.patch.object(ship, "log_override"), \
                mock.patch("builtins.print"), mock.patch("sys.stderr"):
            code = ship.main(["7", "-R", self.F, "--approved-by", "d-1"])
        self.assertEqual((code, events), (3, []))

    def test_relative_log_path(self):
        cwd = os.getcwd()
        os.chdir(self.dir.name)
        try:
            with mock.patch.dict(os.environ, {"PR_SHIP_OVERRIDE_LOG": "rel.jsonl"}):
                ship.log_override("pending", {"ask": "d-1"}, self.F, 7, "abc")
        finally:
            os.chdir(cwd)
        self.assertTrue(os.path.exists(os.path.join(os.path.realpath(self.dir.name), "rel.jsonl")))

    def test_token_outside_chosen_label_ignored(self):
        self.assertIsNotNone(self.check(ask(label="Merge", title=TOKEN, body=TOKEN, note=TOKEN))[1])
        self.assertIsNotNone(self.check(ask(label="Hold", other=TOKEN))[1])
        self.assertIsNone(self.check(ask(label="Hold", other=TOKEN, choice="b"))[1])

    def test_answer_gates(self):
        self.assertIsNotNone(self.check(ask(by="default"))[1])
        self.assertIsNotNone(self.check(ask(by="Human"))[1])
        self.assertIsNotNone(self.check(ask(by=None))[1])
        self.assertIsNotNone(self.check(ask(status="open"))[1])
        self.assertIsNotNone(self.check(ask(choice="zzz"))[1])

    def test_merge_logs_pending_before_merge(self):
        code, events, out = self.go(ask())
        self.assertEqual((code, events), (0, ["pending", "merge", "merged"]))
        self.assertIn("app#7 merged (human-approved via d-1: ship app#7@abcdef0 (merge as is))",
                      [c.args[0] for c in out.call_args_list])

    def test_pending_log_failure_blocks_merge(self):
        def boom(phase):
            raise OSError("disk full")

        code, events, _ = self.go(ask(), log=boom)
        self.assertEqual((code, events), (5, ["pending"]))

    def test_merged_log_failure_warns_and_succeeds(self):
        def boom(phase):
            if phase == "merged":
                raise OSError("disk full")

        code, events, _ = self.go(ask(), log=boom)
        self.assertEqual((code, events), (0, ["pending", "merge", "merged"]))

    def test_real_log_lines(self):
        with mock.patch.dict(os.environ, {"PR_SHIP_OVERRIDE_LOG": self.log}):
            ship.log_override("pending", {"ask": "d-1", "choice": "a", "label": "l"}, self.F, 7, "abc")
        entry = json.loads(open(self.log).read())
        self.assertEqual((entry["phase"], entry["repo"], entry["number"], entry["head"], entry["ask"]),
                         ("pending", self.F, 7, "abc", "d-1"))

    def test_dry_run_writes_nothing(self):
        code, events, _ = self.go(ask(), [ainfo(bot=NO_FP)], ["--dry-run"])
        self.assertEqual((code, events), (0, []))

    def test_refused_ask_no_mutation(self):
        code, events, _ = self.go(ask(label="Merge"))
        self.assertEqual((code, events), (4, []))

    def test_not_applicable_on_green_repo_and_draft(self):
        code, events, out = self.go(ask(), [ainfo()], repo="acme/site")
        self.assertEqual((code, events), (4, []))
        self.assertIn("approval token not applicable here", out.call_args_list[-1].args[0])
        self.assertEqual(self.go(ask(), [ainfo(draft=True)])[:2], (4, []))

    def test_missing_ask_exit_5(self):
        def boom(i):
            raise ship.ps.PrError("no such ask")

        with mock.patch.object(ship.ps, "build", lambda r, n, runs=False: ainfo()), mock.patch.object(ship, "load_ask", boom), \
                mock.patch("builtins.print"):
            self.assertEqual(ship.main(["7", "-R", self.F, "--approved-by", "x"]), 5)

    def test_other_gates_remain(self):
        for over, expect in ((dict(ci={"total": 5, "ok": 4, "pending": 0, "failed": [{"name": "t", "url": None}]}), 3),
                             (dict(merge_state="BLOCKED"), 3), (dict(author="someone-else"), 4), (dict(base="x"), 4)):
            code, events, _ = self.go(ask(), [ainfo(bot=NO_FP, **over)])
            self.assertEqual((code, events), (expect, []), over)

    def test_without_flag_bot_applies(self):
        i = ainfo(bot=NO_FP)
        self.assertEqual(ship.decide(i, self.F)[0], "wait")
        self.assertEqual(ship.decide(i, self.F, {"ask": "d-1", "label": "Merge"})[0], "merge")

    def test_high_risk_waived_only_with_approval(self):
        i = ainfo(bot={"approved": True, "verdict_head": HEAD, "high_risk": True})
        self.assertEqual(ship.decide(i, self.F)[0], "refuse")
        self.assertEqual(ship.decide(i, self.F, {"ask": "d-1", "label": "Merge"})[0], "merge")

    def test_recheck_passes_approval(self):
        i = ainfo(bot=NO_FP)
        with mock.patch.object(ship, "fetch_info", lambda r, n: i):
            ship.recheck(self.F, 1, i, "merge", {"ask": "d-1", "label": "Merge"})
            with self.assertRaises(ship.StateChanged):
                ship.recheck(self.F, 1, i, "merge")


def run_json(**over):
    base = {"id": 9, "name": "CI", "event": "issue_comment", "status": "completed", "conclusion": "failure",
            "head_sha": "0123456789abcdef", "head_branch": "main", "created_at": "2026-10-14T01:02:03Z",
            "html_url": "https://github.com/o/r/actions/runs/9"}
    base.update(over)
    return base


class RunsTest(unittest.TestCase):
    R = "o/r"

    def capture(self, fn, *a):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fn(*a)
        return code, buf.getvalue()

    def test_line_format(self):
        self.assertEqual(ps.run_line(ps.run_summary(run_json())),
                         "CI · issue_comment · failure · 0123456 · main · 2026-10-14T01:02:03Z · https://github.com/o/r/actions/runs/9")
        self.assertEqual(ps.run_summary(run_json(status="in_progress", conclusion=None))["status"], "in_progress")

    def test_run_lists_failed_jobs(self):
        jobs = {"jobs": [{"id": 1, "name": "lint", "conclusion": "failure"}, {"id": 2, "name": "test", "conclusion": "success"}]}
        with mock.patch.object(ps, "gh_rest", lambda p: jobs if p.endswith("&page=1") else run_json()), \
                mock.patch.object(ps, "job_log") as log:
            code, out = self.capture(ps.print_run, self.R, 9, False, 60, False)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1:], ["failed job: lint"])
        log.assert_not_called()

    def test_run_failures_extracts_per_failed_job(self):
        jobs = {"jobs": [{"id": 1, "name": "lint", "conclusion": "failure"}, {"id": 3, "name": "t", "conclusion": "timed_out"},
                         {"id": 2, "name": "ok", "conclusion": "success"}]}
        with mock.patch.object(ps, "gh_rest", lambda p: jobs if "jobs" in p else run_json()), \
                mock.patch.object(ps, "job_log", return_value=LOG) as log:
            code, out = self.capture(ps.print_run, self.R, 9, True, 60, False)
        self.assertEqual([c.args[1] for c in log.call_args_list], [1, 3])
        self.assertIn("Style/StringLiterals", out)

    def test_run_json(self):
        with mock.patch.object(ps, "gh_rest", lambda p: {"jobs": []} if "jobs" in p else run_json()):
            _, out = self.capture(ps.print_run, self.R, 9, False, 60, True)
        self.assertEqual(json.loads(out)["failed_jobs"], [])

    def test_jobs_paginate(self):
        pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}]}
        with mock.patch.object(ps, "gh_rest", lambda p: {"jobs": pages[int(p.rsplit("=", 1)[1])]}):
            self.assertEqual(len(ps.run_jobs(self.R, 9)), 101)

    def test_runs_query_only_given_filters(self):
        seen = []
        with mock.patch.object(ps, "gh_rest", lambda p: seen.append(p) or {"workflow_runs": [run_json(), run_json(id=10)]}):
            _, out = self.capture(ps.list_runs, self.R, "main", None, None, 5, False)
            self.capture(ps.list_runs, self.R, None, "push", "abc", 3, False)
            self.capture(ps.list_runs, self.R, None, None, None, 10, False)
        self.assertEqual(seen, ["repos/o/r/actions/runs?branch=main&per_page=5",
                                "repos/o/r/actions/runs?event=push&head_sha=abc&per_page=3",
                                "repos/o/r/actions/runs?per_page=10"])
        self.assertEqual(len(out.splitlines()), 2)

    def test_arg_conflicts(self):
        import sys
        for argv in (["--run", "1", "7"], ["--runs", "7"], ["--runs", "--wait", "ci"], ["--run", "1", "--findings"],
                     ["--run", "1", "--runs"], [],
                     ["--runs", "--limit", "0"], ["--runs", "--limit", "101"], ["--run", "1", "--branch", "x"],
                     ["--run", "0", "--limit", "5"], ["--branch", "x", "7"], ["--runs", "--failures"],
                     ["--runs", "--sha", "abc1234"], ["--runs", "--sha", "g" * 40]):
            with mock.patch.object(sys, "argv", ["pr-state"] + argv), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as cm:
                    ps.main()
            self.assertEqual(cm.exception.code, 2, argv)

    def test_sha_full_lowercased(self):
        import sys
        seen = []
        with mock.patch.object(sys, "argv", ["pr-state", "--runs", "-R", self.R, "--sha", "A" * 40]), \
                mock.patch.object(ps, "list_runs", lambda *a: seen.append(a) or 0):
            self.assertEqual(ps.main(), 0)
        self.assertEqual(seen[0][3], "a" * 40)

    def test_run_zero_is_a_run(self):
        import sys
        with mock.patch.object(sys, "argv", ["pr-state", "--run", "0", "-R", self.R]), \
                mock.patch.object(ps, "print_run", return_value=0) as pr:
            self.assertEqual(ps.main(), 0)
        pr.assert_called_once()

    def test_main_gh_error_exit_3(self):
        import sys

        def boom(p):
            raise ps.PrError("nope")

        with mock.patch.object(sys, "argv", ["pr-state", "--runs", "-R", self.R]), mock.patch.object(ps, "gh_rest", boom), \
                mock.patch("sys.stderr"):
            self.assertEqual(ps.main(), 3)


class FetchPaginationTest(unittest.TestCase):
    F = "acme/app"

    def pr(self, comments, more):
        empty = {"pageInfo": {}, "nodes": []}
        return {"id": "P", "commits": {"nodes": []}, "reviews": empty, "reviewThreads": empty,
                "comments": {"pageInfo": {"hasPreviousPage": more, "startCursor": "c1"}, "nodes": comments}}

    def test_outsider_marker_does_not_stop_paging(self):
        fake = {"author": {"login": "mallory"}, "body": ps.report_marker() + " all clear"}
        real = {"author": {"login": "review-bot", "__typename": "Bot"},
                "body": ps.report_marker() + "\nrequesting a human review\n<!-- review-bot:head=abc -->"}
        calls = []

        def gql(query, variables):
            calls.append(variables)
            if "pullRequest(number" in query:
                return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": self.pr([fake], True)}}
            return {"node": {"comments": {"pageInfo": {"hasPreviousPage": False}, "nodes": [real]}}}

        with mock.patch.object(ps, "gh_graphql", gql):
            pr, conns = ps.fetch(self.F, 1)
        self.assertEqual(len(calls), 2)
        self.assertIs(ps.find_report(conns["comments"]["nodes"], "review-bot"), real)

    def test_genuine_marker_stops_paging(self):
        real = {"author": {"login": "review-bot", "__typename": "Bot"}, "body": ps.report_marker()}
        calls = []

        def gql(query, variables):
            calls.append(variables)
            return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": self.pr([real], True)}}

        with mock.patch.object(ps, "gh_graphql", gql):
            ps.fetch(self.F, 1)
        self.assertEqual(len(calls), 1)

    def test_repo_without_bot_skips_comment_paging(self):
        calls = []

        def gql(query, variables):
            calls.append(variables)
            return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": self.pr([], True)}}

        with mock.patch.object(ps, "gh_graphql", gql):
            ps.fetch("acme/site", 1)
        self.assertEqual(len(calls), 1)


class FindingsPaginationTest(unittest.TestCase):
    def thread(self, n):
        return {"isResolved": False, "isOutdated": False, "path": "f", "line": n, "originalLine": n,
                "comments": {"totalCount": 1, "nodes": [{"author": {"login": "bot"}, "body": f"t{n}", "url": f"u{n}"}]}}

    def test_threads_beyond_first_page_are_listed(self):
        def gql(query, variables):
            if "pullRequest(number" in query:
                return {"repository": {"pullRequest": {"id": "P", "author": {"login": "me"}, "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "e1"}, "nodes": [self.thread(1)]}}}}
            self.assertEqual(variables["c"], "e1")
            return {"node": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [self.thread(101)]}}}

        out = io.StringIO()
        with mock.patch.object(ps, "gh_graphql", gql), contextlib.redirect_stdout(out):
            ps.print_findings("o/r", 1, "all", 200)
        self.assertIn("2 shown", out.getvalue())
        self.assertIn("u101", out.getvalue())

    def test_page_cap_raises(self):
        def gql(query, variables):
            conn = {"pageInfo": {"hasNextPage": True, "endCursor": "e"}, "nodes": []}
            if "pullRequest(number" in query:
                return {"repository": {"pullRequest": {"id": "P", "author": {"login": "me"}, "reviewThreads": conn}}}
            return {"node": {"reviewThreads": conn}}

        with mock.patch.object(ps, "gh_graphql", gql), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ps.PrError):
                ps.print_findings("o/r", 1, "all", 200)


class BotIdentityTest(unittest.TestCase):
    F = "acme/app"

    def test_user_typed_bot_login_ignored(self):
        user = {"login": "review-bot", "__typename": "User"}
        review = {"author": user, "state": "APPROVED", "body": "Review bot: ok 5/5", "commit": {"oid": "h"}}
        report = {"author": user, "body": ps.report_marker() + "\n<!-- review-bot:head=h -->"}
        fp = ps.analyze_bot({"isDraft": False, "state": "OPEN", "headRefOid": "h"},
                            {"reviews": {"nodes": [review]}, "comments": {"nodes": [report]}}, self.F)
        self.assertFalse(fp["approved"])
        self.assertEqual(fp["status"], "none")

    def test_paging_cap_without_report_fails_closed(self):
        calls = []

        def gql(query, variables):
            calls.append(1)
            conn = {"pageInfo": {"hasPreviousPage": True, "startCursor": "c"}, "nodes": []}
            if "pullRequest(number" in query:
                empty = {"pageInfo": {}, "nodes": []}
                pr = {"id": "P", "commits": {"nodes": []}, "reviews": empty, "reviewThreads": empty, "comments": conn}
                return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": pr}}
            return {"node": {"comments": conn}}

        with mock.patch.object(ps, "gh_graphql", gql):
            pr, conns = ps.fetch(self.F, 1)
        fp = ps.analyze_bot({"isDraft": False, "state": "OPEN", "headRefOid": "h"}, conns, self.F)
        self.assertEqual(len(calls), 11)
        self.assertTrue(fp["high_risk"])
        self.assertEqual(fp["high_risk_reason"], "report not found within paging cap")


class DeployedWaitTest(unittest.TestCase):
    def run_wait(self, pm, as_json=False, buf=None):
        i = {"state": "merged", "draft": False, "repo": "o/r", "number": 1, "head": "abcdef0", "title": "t", "open_threads": 0,
             "ci": {"total": 1, "ok": 1, "pending": 0, "failed": []}, "bot": {"status": "n/a", "approved": False},
             "review_decision": None, "merge_state": None, "merge_commit": "m" * 40, "post_merge": pm}
        args = mock.Mock(timeout=5, interval=1, json=as_json)
        with mock.patch.object(ps, "snapshot", lambda t, r: [i]), mock.patch.object(ps, "SETTLE_SECONDS", 0), \
                mock.patch.object(ps.time, "sleep"), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(buf if buf is not None else io.StringIO()):
            return ps.wait([("o/r", 1)], "deployed", args)

    def pm(self, **over):
        base = {"total": 1, "pending": 0, "deploys": 1, "deploys_pending": 0, "superseded": 0, "deploys_ok": 1, "failed": []}
        base.update(over)
        return base

    def test_successful_deploy_exits_0(self):
        self.assertEqual(self.run_wait(self.pm()), 0)

    def test_cancelled_deploy_is_unverified_exit_4(self):
        self.assertEqual(self.run_wait(self.pm(superseded=1, deploys_ok=0)), 4)

    def test_publish_success_with_deploy_cancelled_is_unverified(self):
        self.assertEqual(self.run_wait(self.pm(deploys=2, superseded=1, deploys_ok=1)), 4)

    def test_no_deploy_run_is_unverified(self):
        self.assertEqual(self.run_wait(self.pm(deploys=0, deploys_ok=0)), 4)

    def test_failed_deploy_exits_2(self):
        pm = self.pm(deploys_ok=0, failed=[{"name": "Deploy", "url": "u", "deploy": True}])
        self.assertEqual(self.run_wait(pm), 2)

    def test_zero_runs_keeps_waiting_until_timeout(self):
        self.assertEqual(ps.condition_status("deployed", {"state": "merged", "ci": {}, "bot": {},
                                                          "post_merge": self.pm(total=0)}, ""), "waiting")

    def test_reason_in_json(self):
        out = io.StringIO()
        self.run_wait(self.pm(deploys=0, deploys_ok=0), as_json=True, buf=out)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["deploy_reason"], "no deploy run on the merge commit")

    def test_failed_ci_without_deploy_is_not_deployed(self):
        pm = self.pm(deploys=0, deploys_ok=0, failed=[{"name": "CI", "url": "u", "deploy": False}])
        self.assertEqual(self.run_wait(pm), 4)

    def test_post_merge_runs_counts_successful_deploys(self):
        runs = [{"name": "Deploy", "event": "push", "status": "completed", "conclusion": "cancelled"}]
        with mock.patch.object(ps, "gh_rest", lambda p: {"workflow_runs": runs}):
            pm = ps.post_merge_runs("o/r", "abc")
        self.assertEqual((pm["superseded"], pm["deploys_ok"]), (1, 0))
        runs[0]["conclusion"] = "success"
        with mock.patch.object(ps, "gh_rest", lambda p: {"workflow_runs": runs}):
            self.assertEqual(ps.post_merge_runs("o/r", "abc")["deploys_ok"], 1)


class MergeFreezeTest(unittest.TestCase):
    def freeze(self, content):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "freeze.json")
        if content is not None:
            with open(path, "w") as f:
                f.write(content)
        return mock.patch.dict(os.environ, {"PR_SHIP_FREEZE_FILE": path})

    def test_no_file_means_no_freeze(self):
        with self.freeze(None):
            self.assertIsNone(ship.merge_freeze("acme/app"))

    def test_listed_repo_is_frozen_with_reason(self):
        with self.freeze('{"repos": ["acme/app"], "reason": "release"}'):
            self.assertEqual(ship.merge_freeze("acme/app"), "release")
            self.assertIsNone(ship.merge_freeze("acme/api"))

    def test_unreadable_file_freezes(self):
        with self.freeze("{nope"):
            self.assertEqual(ship.merge_freeze("acme/app"), "freeze file unreadable")

    def test_frozen_repo_makes_no_mutation(self):
        calls = []
        with self.freeze('{"repos": ["acme/app"], "reason": "release"}'), \
                mock.patch.object(ship.ps, "build", lambda r, n, runs=False: info()), \
                mock.patch.object(ship.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or mock.Mock(returncode=0)), \
                mock.patch("builtins.print"):
            code = ship.main(["7", "-R", "acme/app"])
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])


class WaitAnyTest(unittest.TestCase):
    def run_wait(self, any_flag, merged_numbers):
        def i(n):
            return {"state": "merged" if n in merged_numbers else "open", "draft": False, "repo": "o/r", "number": n,
                    "head": "abcdef0", "title": "t", "open_threads": 0,
                    "ci": {"total": 1, "ok": 1, "pending": 0, "failed": []}, "bot": {"status": "n/a", "approved": False},
                    "review_decision": None, "merge_state": None, "merge_commit": None,
                    "post_merge": {"total": 0, "pending": 0, "deploys": 0, "deploys_pending": 0, "superseded": 0,
                                   "deploys_ok": 0, "failed": []}}
        args = mock.Mock(timeout=0, interval=1, json=False)
        args.any = any_flag
        with mock.patch.object(ps, "snapshot", lambda t, r: [i(1), i(2)]), mock.patch.object(ps.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return ps.wait([("o/r", 1), ("o/r", 2)], "merged", args)

    def test_any_returns_when_one_met(self):
        self.assertEqual(self.run_wait(True, {2}), 0)

    def test_without_any_waits_for_all(self):
        self.assertEqual(self.run_wait(False, {2}), 1)


class ModelPinTest(unittest.TestCase):
    HOOK = os.path.join(HERE, "..", "hooks", "model-pin.py")

    def run_hook(self, **inp):
        out = subprocess.run([sys.executable, self.HOOK], input=json.dumps({"tool_input": inp}),
                             capture_output=True, text=True).stdout
        return json.loads(out) if out.strip() else None

    def test_opus_conductor_denied(self):
        r = self.run_hook(subagent_type="adw-conductor", model="opus", description="build segment", prompt="x")
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_opus_conductor_allowed_for_redesign(self):
        self.assertIsNone(self.run_hook(subagent_type="adw-conductor", model="opus", description="redesign segment", prompt="x"))

    def test_opus_worker_denied_even_for_redesign(self):
        r = self.run_hook(subagent_type="worker", model="opus", description="redesign", prompt="x")
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
