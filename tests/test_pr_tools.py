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


REVIEWS = json.dumps([
    {"id": 11, "user": {"login": "approver-bot[bot]"}, "state": "APPROVED"},
    {"id": 12, "user": {"login": "someone-else"}, "state": "APPROVED"},
    {"id": 13, "user": {"login": "approver-bot"}, "state": "COMMENTED"},
    {"id": 14, "user": {"login": "approver-bot"}, "state": "APPROVED"}])


def gh_reply(cmd, merge_fails=False, dismiss_fails=False, list_fails=False, reviews=None):
    """Fake subprocess.run for the gh/ssh calls pr-ship makes after a bot approve."""
    bad = (merge_fails and cmd[:3] == ["gh", "pr", "merge"]) or (dismiss_fails and cmd[:4] == ["gh", "api", "-X", "PUT"]) \
        or (list_fails and "--paginate" in cmd)
    out = (REVIEWS if reviews is None else reviews) if cmd[:2] == ["gh", "api"] and "--paginate" in cmd else ""
    return mock.Mock(returncode=1 if bad else 0, stdout=out, stderr="boom" if bad else "")


def dismissed_ids(calls):
    return [c[4].split("/")[-2] for c in calls if c[:4] == ["gh", "api", "-X", "PUT"]]


class ResidualHarness:
    F = "acme/app"
    HEAD = "abc1234"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = os.path.join(self.dir.name, "ledger.jsonl")
        self.write_ledger([("codex", "pass", self.HEAD), ("claude", "pass", "old1")])
        self.seen = []
        patcher = mock.patch.dict(ship.ps.CONFIG, {"bot_approver": {"login": "approver-bot", "gh": ["ssh", "approver-host", "gh"]}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_ledger(self, rows):
        with open(self.ledger, "w") as f:
            for who, verdict, sha, *via in rows:
                f.write(json.dumps({"who": who, "verdict": verdict, "sha": sha, **({"via": via[0]} if via else {})}) + "\n")

    def finding(self, **over):
        return {"path": "a.rb", "url": "https://x/1", "author": "review-bot", "severity": "minor", "replied": True,
                "replies": ["fixed"], "original_commit": "old1", **over}

    def pr(self, **over):
        fp = {"approved": False, "verdict_head": self.HEAD, "high_risk": False, "score": "4", "label": "Findings"}
        return info(**{"head": self.HEAD, "bot": fp, "review_decision": "REVIEW_REQUIRED", "merge_state": "BLOCKED",
                       "branch": "claude/x", **over})

    def accepted_ask(self, **over):
        return {"id": "d-abc123", "status": "answered", "title": "Accept app#7 risk?", "body": "",
                "options": [{"key": "a", "label": "Accept the risk"}, {"key": "b", "label": "Fix it first"}],
                "answer": {"choice": "a", "by": "human"}, **over}

    def go(self, infos=None, findings=None, asks=None, argv=None, freeze_after=99, dismiss_fails=False, sleep=None, mode=None, **over):
        calls, logs, freezes = [], [], []
        self.printed = printed = []
        after = {"review_decision": "APPROVED", "merge_state": "CLEAN"} if "review_decision" not in over else {}
        seq = iter(infos or [self.pr(**over)] * 2 + [self.pr(**{**over, **after})] * 3)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return gh_reply(cmd, dismiss_fails=dismiss_fails)

        def fake_freeze(r):
            freezes.append(1)
            return "release" if len(freezes) > freeze_after else None

        def fake_ask(i):
            if i not in (asks or {}):
                raise ship.ps.PrError("no such ask")
            return asks[i]

        with mock.patch.dict(os.environ, {"PR_SHIP_REVIEW_LEDGER": self.ledger}), \
                mock.patch.object(ship.ps, "build", lambda r, n, runs=False: next(seq)), \
                mock.patch.object(ship.ps, "gh_rest", lambda path: [
                    {"sha": "old1", "commit": {"author": {"date": "2026-10-01T00:00:00Z"}}},
                    {"sha": self.HEAD, "commit": {"author": {"date": "2026-10-03T00:00:00Z"}}}]), \
                mock.patch.object(ship.ps, "open_findings", lambda r, n, resolved_blocking=False: self.seen.append(resolved_blocking) or (findings if findings is not None else [])), \
                mock.patch.object(ship, "merge_freeze", fake_freeze), \
                mock.patch.object(ship, "load_ask", fake_ask), \
                mock.patch.object(ship, "log_override", lambda phase, entry, *a: logs.append((phase, entry))), \
                mock.patch.object(ship.subprocess, "run", fake_run), mock.patch.object(ship.time, "sleep", sleep or mock.DEFAULT), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
            code = ship.main(["7", "-R", self.F] + (mode or ["--residual", "false positive, verified"]) + (argv or []))
        return code, calls, logs


class ResidualTest(ResidualHarness, unittest.TestCase):
    def test_happy_path_approves_then_merges_and_logs(self):
        accepted = {"d-abc123": self.accepted_ask()}
        findings = [self.finding(), self.finding(severity="blocking", replies=["Approver accepted this, see d-abc123"])]
        code, calls, logs = self.go(findings=findings, asks=accepted)
        self.assertEqual(code, 0)
        self.assertEqual([c[:5] if c[0] == "ssh" else c[:3] for c in calls],
                         [["ssh", "approver-host", "gh", "pr", "review"], ["gh", "pr", "merge"]])
        self.assertIn("--approve", calls[0])
        self.assertEqual(calls[0][-1], "'pr-ship residual: review bot 4/5 unapproved, findings answered, CI green - false positive, verified'")
        self.assertEqual(calls[1], ["gh", "pr", "merge", "7", "-R", self.F, "--squash", "--match-head-commit", self.HEAD])
        self.assertEqual([(p, e["stage"]) for p, e in logs], [("residual", "pending"), ("residual", "merged")])
        self.assertEqual(logs[0][1]["asks"], ["d-abc123"])
        self.assertEqual(logs[0][1]["reason"], "false positive, verified")

    def test_freeze_appearing_before_clean_merge_refuses(self):
        code, calls, _ = self.go(review_decision=None, merge_state="CLEAN", freeze_after=2)
        self.assertEqual(code, 4)
        self.assertEqual(calls, [])

    def test_no_approve_when_review_not_required(self):
        code, calls, _ = self.go(review_decision=None, merge_state="CLEAN")
        self.assertEqual(code, 0)
        self.assertEqual([c[:3] for c in calls], [["gh", "pr", "merge"]])

    def test_dry_run_no_mutation(self):
        code, calls, logs = self.go(argv=["--dry-run"])
        self.assertEqual((code, calls, logs), (0, [], []))

    def refused(self, **kw):
        code, calls, logs = self.go(**kw)
        self.assertEqual(code, 4)
        self.assertEqual((calls, logs), ([], []))

    def test_refuses_high_risk(self):
        pr = self.pr()
        pr["bot"]["high_risk"] = True
        self.refused(infos=[pr])

    def test_refuses_without_codex_receipt_on_head(self):
        self.write_ledger([("codex", "pass", "old1"), ("claude", "pass", "old1")])
        self.refused()

    def test_refuses_without_claude_receipt(self):
        self.write_ledger([("codex", "pass", self.HEAD)])
        self.refused()

    def test_claude_low_tier_skip_counts(self):
        self.write_ledger([("codex", "pass", self.HEAD), ("claude", "skip", "old1")])
        self.assertEqual(self.go()[0], 0)

    def test_refuses_unreplied_finding(self):
        self.refused(findings=[self.finding(replied=False, replies=[])])

    def test_refuses_critical_reply_without_approver_ask(self):
        findings = [self.finding(severity="blocking", replies=["ack, see d-abc123"])]
        self.refused(findings=findings, asks={"d-abc123": self.accepted_ask(answer={"choice": "a", "by": "mate"})})
        self.refused(findings=findings, asks={})
        self.refused(findings=[self.finding(severity=None, replies=["will not fix"])])

    def test_critical_false_positive_with_file_line_merges(self):
        for first in ("\U0001F6A8 **Critical** (correctness)", "\u26a0\ufe0f **Warning** (security)"):
            finding = self.finding(severity=ps.finding_severity(first + "\nbody"),
                                   replies=["False positive: app/models/a.rb:42 already scopes the tenant"])
            self.assertEqual(finding["severity"], "blocking")
            code, calls, _ = self.go(findings=[finding])
            self.assertEqual(code, 0, first)
            self.assertEqual(calls[-1][:3], ["gh", "pr", "merge"])

    def test_false_positive_without_file_line_refused(self):
        finding = self.finding(severity=ps.finding_severity("\U0001F6A8 **Critical** (correctness)"),
                               replies=["False positive, trust me"])
        self.refused(findings=[finding])

    def test_waits_on_ci_pending(self):
        code, calls, logs = self.go(ci={"total": 5, "ok": 4, "pending": 1, "failed": []})
        self.assertEqual((code, calls, logs), (3, [], []))

    def test_waits_when_not_clean(self):
        self.assertEqual(self.go(merge_state="BEHIND")[0], 3)

    def test_blocked_without_review_required_waits(self):
        for decision in ("APPROVED", None):
            code, calls, _ = self.go(review_decision=decision, merge_state="BLOCKED")
            self.assertEqual((code, calls), (3, []))

    def test_reason_sanitized_for_approver_command(self):
        code, calls, _ = self.go(mode=["--residual", "x; rm -rf (y)"])
        self.assertEqual(calls[0][:3], ["ssh", "approver-host", "gh"])
        self.assertEqual(calls[0][-1], "'pr-ship residual: review bot 4/5 unapproved, findings answered, CI green - x rm -rf y'")

    def test_post_approve_not_clean_aborts_before_merge(self):
        pr = self.pr()
        code, calls, logs = self.go(infos=[pr] * 20)
        self.assertEqual(code, 3)
        self.assertEqual(dismissed_ids(calls), ["11", "14"])
        self.assertEqual(logs[-1][1]["stage"], "approved-withdrawn")

    def test_changes_requested_waits_without_ssh(self):
        cr = self.pr(review_decision="CHANGES_REQUESTED")
        code, calls, _ = self.go(infos=[cr] * 3)
        self.assertEqual((code, calls), (3, []))

    def test_approve_raising_still_dismisses(self):
        real = ship.run

        def failing(cmd):
            if cmd[0] == "ssh":
                raise ship.ps.PrError("ssh reset")
            return real(cmd)

        with mock.patch.object(ship, "run", failing):
            code, calls, logs = self.go()
        self.assertEqual((code, sorted(set(dismissed_ids(calls)))), (5, ["11", "14"]))
        self.assertEqual(logs[-1][1]["stage"], "approved-withdrawn")

    def test_merge_error_dismisses_residual(self):
        real = ship.run

        def failing(cmd):
            if cmd[:3] == ["gh", "pr", "merge"]:
                raise ship.ps.PrError("merge failed")
            return real(cmd)

        with mock.patch.object(ship, "run", failing):
            code, calls, logs = self.go()
        self.assertEqual((code, dismissed_ids(calls)), (5, ["11", "14"]))
        self.assertEqual(logs[-1][1]["stage"], "approved-withdrawn")

    def test_freeze_appearing_before_merge_dismisses_and_refuses(self):
        code, calls, logs = self.go(freeze_after=3)
        self.assertEqual(code, 4)
        self.assertEqual(dismissed_ids(calls), ["11", "14"])
        self.assertNotIn(["gh", "pr", "merge"], [c[:3] for c in calls])

    def test_sigterm_during_settle_dismisses(self):
        import signal

        def term(_):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        code, calls, logs = self.go(infos=[self.pr()] * 20, sleep=term)
        self.assertEqual((code, dismissed_ids(calls)), (5, ["11", "14"]))

    def test_dismiss_failure_exits_5_unmerged(self):
        code, calls, logs = self.go(infos=[self.pr()] * 20, dismiss_fails=True)
        self.assertEqual(code, 5)
        self.assertEqual(logs[-1][1]["stage"], "approved-unmerged")

    def test_accept_ask_must_name_this_pr_and_option(self):
        finding = self.finding(severity="blocking", replies=["Approver accepted, d-abc123"])
        self.refused(findings=[finding], asks={"d-abc123": self.accepted_ask(title="Accept app#8 risk?")})
        self.refused(findings=[finding], asks={"d-abc123": self.accepted_ask(answer={"choice": "b", "by": "human"})})
        url = self.accepted_ask(title="risk", body="https://github.com/acme/app/pull/7")
        self.assertEqual(self.go(findings=[finding], asks={"d-abc123": url})[0], 0)

    def test_negated_false_positive_refused(self):
        finding = self.finding(severity="blocking", replies=["Not a false positive: real bug at x.rb:88"])
        self.refused(findings=[finding])

    def test_resolved_blocking_thread_needs_same_proof(self):
        code, calls, logs = self.go(findings=[self.finding(severity="blocking", replies=["won't fix"])])
        self.assertTrue(self.seen and all(self.seen))
        self.assertEqual(code, 4)

    def test_resolved_blocking_thread_citing_a_pr_commit_counts_as_fixed(self):
        f = self.finding(severity="blocking", resolved=True, replies=[f"Fixed in {self.HEAD[:9]}"])
        self.assertEqual(self.go(findings=[f])[0], 0)

    def test_open_or_foreign_commit_citation_is_not_a_fix(self):
        self.assertEqual(self.go(findings=[self.finding(severity="blocking", replies=["Fixed in abcdef12"])])[0], 4)
        self.assertEqual(self.go(findings=[self.finding(severity="blocking", resolved=True, replies=["Fixed in abcdef12"])])[0], 4)
        still = self.finding(severity="blocking", resolved=True, replies=[f"Still broken after {self.HEAD}, not fixed"])
        self.assertEqual(self.go(findings=[still])[0], 4)

    def test_fixed_in_later_pr_commit_counts_on_unresolved_thread(self):
        f = self.finding(severity="blocking", resolved=False, replies=[f"Fixed in {self.HEAD[:9]}"])
        self.assertEqual(self.go(findings=[f])[0], 0)

    def test_fixed_in_the_reviewed_commit_itself_is_refused(self):
        f = self.finding(severity="blocking", resolved=False, replies=["Fixed in old1"])
        self.assertEqual(self.go(findings=[f])[0], 4)

    def test_fixed_in_with_unknown_original_commit_is_refused(self):
        f = self.finding(severity="blocking", replies=[f"Fixed in {self.HEAD[:9]}"], original_commit="gone")
        self.assertEqual(self.go(findings=[f])[0], 4)

    def test_rebased_away_original_counts_a_fix_authored_after_the_finding(self):
        f = self.finding(severity="blocking", replies=[f"Fixed in {self.HEAD[:9]}"], original_commit="gone",
                         created="2026-10-02T00:00:00Z")
        self.assertEqual(self.go(findings=[f])[0], 0)

    def test_rebased_away_original_refuses_a_commit_authored_before_the_finding(self):
        early = self.finding(severity="blocking", replies=["Fixed in old1"], original_commit="gone", created="2026-10-02T00:00:00Z")
        self.assertEqual(self.go(findings=[early])[0], 4)
        late = self.finding(severity="blocking", replies=[f"Fixed in {self.HEAD[:9]}"], original_commit="gone",
                            created="2026-10-04T00:00:00Z")
        self.assertEqual(self.go(findings=[late])[0], 4)

    def test_resolved_outdated_critical_without_reply_is_refused(self):
        f = self.finding(severity="blocking", resolved=True, outdated=True, replied=False, replies=[])
        self.assertEqual(self.go(findings=[f])[0], 4)

    def test_verdict_on_older_head_merges(self):
        fp = {"approved": False, "verdict_head": "old1", "high_risk": False, "score": None, "label": "Findings", "rounds": 1}
        self.assertEqual(self.go(bot=fp, findings=[self.finding()])[0], 0)

    def test_no_bot_review_waits(self):
        fp = {"approved": False, "verdict_head": None, "high_risk": False, "score": None, "label": None, "rounds": 0}
        code, calls, _ = self.go(bot=fp)
        self.assertEqual((code, calls), (3, []))

    def test_unverified_codex_row_refused(self):
        with open(self.ledger, "w") as f:
            for who, sha, unv in (("codex", self.HEAD, True), ("claude", "old1", False)):
                f.write(json.dumps({"who": who, "verdict": "pass", "sha": sha, "unverified": unv}) + "\n")
        self.refused()

    def test_ancestor_claude_skip_still_accepted(self):
        with open(self.ledger, "w") as f:
            for who, v, sha in (("codex", "pass", self.HEAD), ("claude", "skip", "old1")):
                f.write(json.dumps({"who": who, "verdict": v, "sha": sha, "unverified": False}) + "\n")
        self.assertEqual(self.go()[0], 0)

    def test_head_moving_during_approval_is_logged_and_waits(self):
        moved = self.pr(head="f" * 40, review_decision="APPROVED", merge_state="CLEAN")
        code, calls, logs = self.go(infos=[self.pr()] * 2 + [moved] * 3, findings=[self.finding()])
        self.assertEqual(code, 3)
        self.assertIn("approved-moved-head", [e.get("stage") for _, e in logs])
        self.assertEqual(logs[-1][1]["stage"], "approved-withdrawn")
        self.assertEqual(dismissed_ids(calls), ["11", "14"])
        self.assertNotIn(["gh", "pr", "merge"], [c[:3] for c in calls])

    def test_mutually_exclusive_with_approved_by(self):
        self.assertEqual(self.go(argv=["--approved-by", "d-1"])[0], 5)


class OpenFindingsTest(unittest.TestCase):
    def threads(self):
        def t(body, resolved, outdated):
            return {"isResolved": resolved, "isOutdated": outdated, "path": "a.rb", "line": 1, "originalLine": 1,
                    "comments": {"totalCount": 1, "nodes": [{"author": {"login": "review-bot"}, "body": body, "url": "u"}]}}
        return "me-dev", [t("\U0001F6A8 **Critical** (x)", True, False), t("\U0001F6A8 **Critical** (x)", True, True),
                              t("Nit: y", True, False), t("\u26a0\ufe0f **Warning** (z)", False, False)]

    def test_resolved_blocking_only_when_asked(self):
        with mock.patch.object(ps, "fetch_findings", lambda r, n: self.threads()):
            self.assertEqual(len(ps.open_findings("o/r", 1)), 1)
            self.assertEqual(len(ps.open_findings("o/r", 1, resolved_blocking=True)), 3)


class FindingSeverityTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ps.finding_severity("**Critical**: tenant leak"), "blocking")
        self.assertEqual(ps.finding_severity("⚠️ Warning - n+1"), "blocking")
        self.assertEqual(ps.finding_severity("\U0001F6A8 **Critical** (correctness)"), "blocking")
        self.assertEqual(ps.finding_severity("\u26a0\ufe0f **Warning** (security)"), "blocking")
        self.assertEqual(ps.finding_severity("Nit: rename"), "minor")
        self.assertEqual(ps.finding_severity("Suggestion (minor): rename"), "minor")
        self.assertIsNone(ps.finding_severity("This looks wrong"))


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
        return self.run_wait_states(any_flag, ["merged" if n in merged_numbers else "open" for n in (1, 2)])

    def run_wait_states(self, any_flag, states):
        def i(n):
            return {"state": states[n - 1], "draft": False, "repo": "o/r", "number": n,
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


class MachineRoutingTest(unittest.TestCase):
    @staticmethod
    def asks_module():
        loader = importlib.machinery.SourceFileLoader("adw_asks", os.path.join(HERE, "..", "board", "asks.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    @classmethod
    def setUpClass(cls):
        cls.asks = cls.asks_module()

    def patched(self, machine=None, remotes=("node1",)):
        a = self.asks
        return mock.patch.multiple(a, MACHINE=machine,
                                   REMOTE_HERDR={m: a.Path(f"/x/{m}.sock") for m in remotes})

    def test_machines_from_env(self):
        with mock.patch.dict(os.environ, {"ADW_MACHINES": "laptop, node1,bad name"}):
            self.assertEqual(self.asks.configured_machines(), ["laptop", "node1"])

    def test_default_is_single_machine(self):
        with mock.patch.dict(os.environ, {"ADW_HOME": tempfile.mkdtemp()}):
            os.environ.pop("ADW_MACHINES", None)
            self.assertEqual(self.asks.configured_machines(), [])

    def test_remote_pane_routes_to_socket(self):
        with self.patched():
            cmd, env = self.asks.herdr_cmd(["agent", "get", "node1/w1:p2"])
        self.assertEqual(cmd, ["herdr", "agent", "get", "w1:p2"])
        self.assertEqual(env["HERDR_SOCKET_PATH"], "/x/node1.sock")

    def test_own_machine_prefix_is_local(self):
        with self.patched(machine="node1"):
            cmd, env = self.asks.herdr_cmd(["agent", "get", "node1/w1:p2"])
        self.assertEqual((cmd, env), (["herdr", "agent", "get", "w1:p2"], None))

    def test_pane_ref_prefixes_only_with_machine(self):
        with self.patched(machine="node1"):
            self.assertEqual(self.asks.pane_ref("w1:p2"), "node1/w1:p2")
            self.assertEqual(self.asks.pane_ref("laptop/w1:p2"), "laptop/w1:p2")
        with self.patched(machine=None):
            self.assertEqual(self.asks.pane_ref("w1:p2"), "w1:p2")


class ReviewFixesTest(unittest.TestCase):
    def test_freeze_with_empty_reason_still_freezes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "f.json")
        with open(path, "w") as f:
            f.write('{"repos": ["acme/app"], "reason": ""}')
        with mock.patch.dict(os.environ, {"PR_SHIP_FREEZE_FILE": path}):
            self.assertEqual(ship.merge_freeze("acme/app"), "frozen")

    def test_any_wait_met_beats_another_prs_failure(self):
        self.assertEqual(WaitAnyTest().run_wait_states(True, ["merged", "closed"]), 0)

    def test_routing_ignores_message_text(self):
        a = MachineRoutingTest.asks_module()
        with mock.patch.multiple(a, MACHINE=None, REMOTE_HERDR={"node1": a.Path("/x/node1.sock")}):
            cmd, env = a.herdr_cmd(["agent", "prompt", "w1:p2", "node1/w1:p9"])
        self.assertEqual(cmd, ["herdr", "agent", "prompt", "w1:p2", "node1/w1:p9"])
        self.assertIsNone(env)



class EpicReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("EFFICIENCY_REPORT", os.path.join(HERE, "efficiency-report"))
        loader = importlib.machinery.SourceFileLoader("adw_epic_report", os.path.join(HERE, "..", "board", "epic-report.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.er = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.er)

    def test_pr_numbers_ignore_digits_in_owner_and_repo(self):
        f = self.er.pr_numbers
        self.assertEqual(f(42), [42])
        self.assertEqual(f("42"), [42])
        self.assertEqual(f("#42"), [42])
        self.assertEqual(f("https://github.com/org2/repo3/pull/42"), [42])
        self.assertEqual(f("#12 and #345"), [12, 345])
        self.assertEqual(f(["#7", "https://github.com/a1/b2/pull/9"]), [7, 9])
        self.assertEqual(f("owner/repo"), [])
        self.assertEqual(f(None), [])

    def test_born_without_birthtime_uses_oldest_file_not_rewritten_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.er.Path(tmp)
            plan, graph, old = d / "plan.md", d / "graph.json", d / "segments" / "s1.md"
            old.parent.mkdir()
            for x in (plan, graph, old):
                x.write_text("x")
            os.utime(old, (1000, 1000))
            os.utime(plan, (5000, 5000))
            os.utime(graph, (9000, 9000))

            class NoBirth:
                def __init__(self, st):
                    self.st_mtime = st.st_mtime

            real_stat = self.er.Path.stat
            with mock.patch.object(self.er.Path, "stat", lambda self, **kw: NoBirth(real_stat(self, **kw))), \
                    mock.patch.object(self.er.Path, "is_file", lambda self: not self.name == "segments"):
                self.assertEqual(self.er.epic_born(d, (plan, graph)), 1000)

    def test_born_prefers_birthtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.er.Path(tmp)
            plan = d / "plan.md"
            plan.write_text("x")

            class Birth:
                st_birthtime = 1234
                st_mtime = 9999

            with mock.patch.object(self.er.Path, "stat", lambda self, **kw: Birth()):
                self.assertEqual(self.er.epic_born(d, (plan,)), 1234)


class FlaggedTest(ResidualHarness, unittest.TestCase):
    KEY = "new_checkout.v2"

    def setUp(self):
        super().setUp()
        self.write_ledger([("codex", "pass", self.HEAD), ("claude", "pass", "old1")])

    def pr(self, **over):
        return super().pr(**{"body": f"Behind {self.KEY}, off in prod", **over})

    def flagged(self, key=None, **kw):
        return self.go(mode=["--flagged", key or self.KEY], **kw)

    def flagged_refused(self, **kw):
        code, calls, logs = self.flagged(**kw)
        self.assertEqual((code, calls, logs), (4, [], []))

    def test_merges_despite_high_risk_and_logs_flagged(self):
        pr = self.pr()
        pr["bot"]["high_risk"] = True
        code, calls, logs = self.flagged(infos=[pr] * 2 + [{**pr, "review_decision": "APPROVED", "merge_state": "CLEAN"}] * 3)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][-1], f"'pr-ship flagged: new behaviour behind {self.KEY}, findings answered, CI green'")
        self.assertEqual(calls[-1], ["gh", "pr", "merge", "7", "-R", self.F, "--squash", "--match-head-commit", self.HEAD])
        self.assertEqual([(p, e["stage"], e["flag"]) for p, e in logs], [("flagged", "pending", self.KEY), ("flagged", "merged", self.KEY)])
        self.assertTrue(any(p.startswith("monitor: ") and "--flagged " + self.KEY in p for p in self.printed))

    def test_no_watch_skips_monitor_line(self):
        code, _, _ = self.flagged(argv=["--no-watch"])
        self.assertEqual(code, 0)
        self.assertFalse(any(p.startswith(("watch:", "monitor:")) for p in self.printed))

    def test_claude_skip_refuses(self):
        self.write_ledger([("codex", "pass", self.HEAD), ("claude", "skip", "old1")])
        self.flagged_refused()

    def test_missing_opus_refusal_names_reviewer_and_receipt_command(self):
        self.write_ledger([("codex", "pass", self.HEAD), ("claude", "skip", "old1")])
        self.flagged_refused()
        text = "\n".join(self.printed)
        self.assertIn("Run the Opus reviewer on this head", text)
        self.assertIn("review-receipt claude pass", text)

    def test_carried_claude_pass_after_opus_fail_refuses(self):
        self.write_ledger([("claude", "pass", "old1"), ("claude", "fail", "old1"), ("codex", "pass", self.HEAD),
                           ("claude", "pass", self.HEAD, "codex-delta")])
        self.flagged_refused()

    def write_mode_rows(self, sha):
        with open(self.ledger, "w") as f:
            f.write(json.dumps({"who": "codex", "verdict": "pass", "sha": self.HEAD}) + "\n")
            f.write(json.dumps({"who": "claude", "verdict": "pass", "sha": sha, "mode": "flag-check"}) + "\n")

    def test_flag_check_pass_on_an_ancestor_refuses(self):
        self.write_mode_rows("old1")
        self.flagged_refused()

    def test_flag_check_pass_on_head_merges(self):
        self.write_mode_rows(self.HEAD)
        code, _, _ = self.flagged(infos=[self.pr()] * 2 + [{**self.pr(), "review_decision": "APPROVED", "merge_state": "CLEAN"}] * 3)
        self.assertEqual(code, 0)

    def test_flag_check_pass_on_an_ancestor_does_not_count_for_any_path(self):
        self.write_mode_rows("old1")
        with mock.patch.dict(os.environ, {"PR_SHIP_REVIEW_LEDGER": self.ledger}):
            self.assertEqual(ship.review_receipts(self.HEAD, ["old1"]), ("pass", "none"))
            self.write_mode_rows(self.HEAD)
            self.assertEqual(ship.review_receipts(self.HEAD, ["old1"]), ("pass", "pass"))

    def write_carried_rows(self, fc_sha=None):
        rows = [{"who": "claude", "verdict": "fail", "sha": "old1"}, {"who": "codex", "verdict": "pass", "sha": self.HEAD},
                {"who": "claude", "verdict": "pass", "sha": self.HEAD, "via": "codex-delta"}]
        if fc_sha:
            rows.append({"who": "claude", "verdict": "pass", "sha": fc_sha, "mode": "flag-check", "confirms": "flag-coverage"})
        with open(self.ledger, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)

    def test_carried_full_pass_plus_head_flag_check_merges(self):
        self.write_carried_rows(self.HEAD)
        code, _, _ = self.flagged(infos=[self.pr()] * 2 + [{**self.pr(), "review_decision": "APPROVED", "merge_state": "CLEAN"}] * 3)
        self.assertEqual(code, 0)

    def test_carried_full_pass_alone_refuses(self):
        self.write_carried_rows()
        self.flagged_refused()

    def test_carried_full_pass_plus_ancestor_flag_check_refuses(self):
        self.write_carried_rows("old1")
        self.flagged_refused()

    def test_flag_check_alone_is_not_the_full_half(self):
        self.write_mode_rows(self.HEAD)
        with mock.patch.dict(os.environ, {"PR_SHIP_REVIEW_LEDGER": self.ledger}):
            self.assertEqual(ship.review_receipts(self.HEAD, ["old1"], full_only=True), ("pass", "none"))

    def test_direct_opus_pass_after_fail_merges(self):
        self.write_ledger([("claude", "fail", "old1"), ("codex", "pass", self.HEAD), ("claude", "pass", self.HEAD)])
        code, _, _ = self.flagged(infos=[self.pr()] * 2 + [{**self.pr(), "review_decision": "APPROVED", "merge_state": "CLEAN"}] * 3)
        self.assertEqual(code, 0)

    def test_body_without_key_refuses(self):
        self.flagged_refused(body="no flag here")
        self.flagged_refused(body=self.KEY.upper())
        self.flagged_refused(key="checkout", body="uses checkout_v2 only")
        self.flagged_refused(key="checkout", body="uses checkout.v2 only")

    def test_bad_key_refuses(self):
        for key in ("a", "ab", "_bad", "has space", "x" * 81, "a;b"):
            self.flagged_refused(key=key, body=key)

    def test_merge_freeze_refuses(self):
        self.flagged_refused(freeze_after=-1)

    def test_freeze_appearing_before_merge_refuses(self):
        code, calls, _ = self.flagged(freeze_after=3)
        self.assertEqual(code, 4)
        self.assertNotIn(["gh", "pr", "merge"], [c[:3] for c in calls])

    def test_unanswered_critical_refuses(self):
        self.flagged_refused(findings=[self.finding(severity="blocking", replies=["will not fix"])])

    def test_key_as_word_in_prose_passes(self):
        self.assertEqual(self.flagged(key="checkout", body="Gated by checkout, off in prod.")[0], 0)
        self.assertEqual(self.flagged(key="checkout", body="flag (checkout).")[0], 0)

    def test_other_gates_still_refuse(self):
        self.flagged_refused(draft=True)
        self.flagged_refused(author="someone-else")
        self.flagged_refused(base="release")
        pr = self.pr()
        pr["bot"] = {"approved": False, "verdict_head": None, "high_risk": False}
        self.assertEqual(self.flagged(infos=[pr] * 5)[0], 3)
        self.assertEqual(self.flagged(merge_state="BEHIND")[0], 3)


class FlagDarkAcceptTest(FlaggedTest):
    WARN = "Accepted (flag-dark): off in prod, reviewed"

    def warning(self, reply=None, **over):
        return self.finding(**{"severity": "blocking", "level": "warning", "replies": [reply or self.WARN], **over})

    def test_warning_accepted_flag_dark_merges_and_logs_url(self):
        code, _, logs = self.flagged(findings=[self.warning()])
        self.assertEqual(code, 0)
        self.assertEqual(logs[0][1]["flag_dark_accepted"], ["https://x/1"])

    def test_leading_markdown_and_case_ok(self):
        self.assertEqual(self.flagged(findings=[self.warning("> **accepted (flag-dark):** ok")])[0], 0)

    def test_critical_never_qualifies(self):
        self.flagged_refused(findings=[self.finding(severity="blocking", level=None, replies=[self.WARN])])

    def test_unrated_never_qualifies(self):
        self.flagged_refused(findings=[self.finding(severity=None, level=None, replies=[self.WARN])])

    def test_residual_does_not_honour_it(self):
        code, calls, logs = self.go(mode=["--residual", "r"], findings=[self.warning()])
        self.assertEqual((code, calls, logs), (4, [], []))

    def test_needs_the_flagged_checks(self):
        self.flagged_refused(findings=[self.warning()], body="no flag here")
        self.write_ledger([("codex", "pass", self.HEAD), ("claude", "skip", "old1")])
        self.flagged_refused(findings=[self.warning()])

    def test_other_reply_wording_refuses(self):
        self.flagged_refused(findings=[self.warning("Accepted: fine")])
        self.flagged_refused(findings=[self.warning("ok. Accepted (flag-dark): later")])

    def test_unreplied_still_refuses(self):
        self.flagged_refused(findings=[self.warning(replied=False, replies=[])])

    def test_level_parse(self):
        for body, want in (("\u26a0\ufe0f **Warning** (security)", "warning"), ("Medium: n+1", "warning"),
                           ("**Critical**: leak", None), ("Warning: critical leak", None), ("Nit: x", None), ("looks wrong", None)):
            self.assertEqual(ps.finding_level(body), want, body)


class FindingsQueryFieldsTest(unittest.TestCase):
    def test_comment_nodes_request_url_created_and_original_commit(self):
        for q in (ps.FINDINGS_QUERY, ps.FINDINGS_MORE):
            self.assertIn("nodes{author{login} body url createdAt originalCommit{oid}}", q)


class BotApproveTest(unittest.TestCase):
    def approve(self, cfg):
        calls = []
        with mock.patch.dict(ship.ps.CONFIG, {"bot_approver": cfg}), mock.patch.object(ship, "run", calls.append):
            ship.bot_approve("o/r", 7, "Approved by the bot")
        return calls[0]

    def test_plain_gh_passes_body_as_one_argv_item(self):
        cmd = self.approve({"login": "approver-bot", "gh": ["gh-as-approver"]})
        self.assertEqual(cmd[-1], "Approved by the bot")

    def test_ssh_wrapped_gh_quotes_body(self):
        cmd = self.approve({"login": "approver-bot", "gh": ["ssh", "approver-host", "gh"]})
        self.assertEqual(cmd[-1], "'Approved by the bot'")

    def test_login_without_gh_is_refused(self):
        with mock.patch.dict(ship.ps.CONFIG, {"bot_approver": {"login": "approver-bot"}}):
            with self.assertRaises(ship.Refused):
                ship.bot_gh()
