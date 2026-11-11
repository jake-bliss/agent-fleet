import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "board"))

import mux  # noqa: E402
import rotate  # noqa: E402

T = 1_800_000_000.0
CFG = dict(rotate.DEFAULTS)


class FakeMux:
    def __init__(self):
        self.info = {"status": "idle", "session": None}

    def split(self, p):
        return tuple(p.split("/", 1)) if "/" in p else ("", p)

    def strip_self(self, p):
        return p

    screen = "⏺ Done.\n❯ \n"
    last_turn = staticmethod(mux.last_turn)

    def read(self, pane, n):
        return self.screen, None

    def idle_guard(self, pane, session):
        return mux.idle_guard(pane, session, self)

    def get(self, pane):
        return {**self.info, "session": self.info["session"] or "s-" + pane.replace("/", "-")}


def agent(pane="t7", **kw):
    return {"pane": pane, "kind": "claude", "session": "s-" + pane.replace("/", "-"), "role": "lead", "status": "idle",
            "attention": "idle", "machine": "", "epic": "e1", "last_active": T - 3600, **kw}


def probe_of(**p):
    base = {"ctx": 40000, "idle_s": 3600, "running": "2.1.319", "installed": "2.1.319"}
    return lambda m, pane, session: {**base, **p}


class Harness(unittest.TestCase):
    def setUp(self):
        self.sd = Path(tempfile.mkdtemp())
        self.prompts, self.guards = [], []
        self.mux = FakeMux()

    def prompt(self, pane, text, guard=None, timeout=None):
        self.timeouts = getattr(self, 'timeouts', []) + [timeout]
        self.prompts.append((pane, text))
        self.guards.append(guard)
        return "ok", None

    def go(self, agents, mate=None, cfg=None, t=T, probe=None, prompt=None, **p):
        return rotate.run(agents, mate, self.mux, prompt or self.prompt, t=t, cfg={**CFG, **(cfg or {})}, sd=self.sd,
                          probe=probe or probe_of(**p))


class TestDue(Harness):
    def test_small_context_is_left_alone(self):
        self.assertEqual(self.go([agent()]), [])

    def test_large_context_is_compacted(self):
        acts = self.go([agent()], ctx=200000)
        self.assertEqual(self.prompts, [("t7", "/compact")])
        self.assertEqual(acts[0]["event"], "compact")

    def test_old_process_or_stale_binary_alone_does_nothing(self):
        self.go([agent()], age_s=90 * 3600, running="2.1.312")
        self.assertEqual(self.prompts, [])

    def test_idle_comes_from_the_probe_for_remote_terminals(self):
        a = agent("node-a/t5", machine="node-a", last_active=None)
        self.assertEqual(self.go([a], ctx=200000, idle_s=10), [])
        self.assertEqual(self.go([a], ctx=200000, idle_s=9999)[0]["event"], "compact")

    def test_probe_without_idle_is_skipped(self):
        self.assertEqual(self.go([agent()], probe=lambda m, p, s: {"ctx": 200000}), [])


class TestEligibility(Harness):
    def test_working_blocked_drafts_asks_and_recent_are_skipped(self):
        agents = [agent("t1", status="working"), agent("t2", attention="blocked"), agent("t3", last_active=T - 10),
                  agent("t4", attention="unsent"), agent("t5", attention="asks")]
        self.assertEqual(self.go(agents, cfg={"max_per_tick": 9}, ctx=200000), [])

    def test_excluded_machine_non_lead_and_codex_are_skipped(self):
        agents = [agent("laptop/t1", machine="laptop"), agent("t2", role="crew"), agent("t3", kind="codex")]
        with mock.patch.object(rotate, "EXCLUDED_MACHINES", ("laptop",)):
            self.go(agents, cfg={"max_per_tick": 9}, ctx=200000)
        self.assertEqual(self.prompts, [])

    def test_mate_and_remote_leads_are_included(self):
        agents = [agent("t2", role="loose"), agent("node-a/t5", machine="node-a")]
        self.go(agents, mate="t2", cfg={"max_per_tick": 9}, ctx=200000)
        self.assertEqual({p for p, _ in self.prompts}, {"t2", "node-a/t5"})

    def test_background_work_and_waits_are_still_compacted(self):
        self.go([agent(attention="background", watch={"state": "waiting"})], ctx=200000)
        self.assertEqual(self.prompts, [("t7", "/compact")])


class TestRace(Harness):
    def guard(self):
        self.go([agent()], ctx=200000)
        return self.guards[0]

    def test_guard_refuses_a_terminal_that_went_working(self):
        g = self.guard()
        self.mux.info = {"status": "working", "session": None}
        self.assertIn("no longer idle", g())

    def test_guard_refuses_a_different_session(self):
        g = self.guard()
        self.mux.info = {"status": "idle", "session": "other"}
        self.assertIsNotNone(g())

    def test_guard_allows_idle_same_session(self):
        self.assertIsNone(self.guard()())

    def test_guard_refuses_when_an_unsent_draft_appeared_after_the_snapshot(self):
        g = self.guard()
        self.mux.screen = "⏺ Done.\n❯ actually hold off on the merge, I"
        self.assertIn("unsent draft", g())

    def test_guard_refuses_when_the_screen_read_fails(self):
        g = self.guard()
        self.mux.read = lambda pane, n: (None, "hc read timed out")
        self.assertIn("unreadable", g())

    def test_mux_prompt_sends_no_key_when_guard_refuses(self):
        sent, orig = [], mux.hc
        mux.hc = lambda *a, **k: (sent.append(a) or ("", None))
        try:
            out = mux.prompt("node-b/t5", "/compact", guard=lambda: "went working")
        finally:
            mux.hc = orig
        self.assertEqual(out, (None, "went working"))
        self.assertEqual(sent, [])


class TestLimits(Harness):
    def test_max_per_tick_caps_actions_and_probing(self):
        calls = []
        agents = [agent(f"node-b/t{i}", machine="node-b") for i in range(6)]
        acts = self.go(agents, cfg={"max_per_tick": 1},
                       probe=lambda m, p, s: calls.append(p) or {"ctx": 200000, "idle_s": 9999})
        self.assertEqual((len(acts), len(calls)), (1, 1))

    def test_deadline_stops_probing(self):
        calls = []
        rotate.run([agent()], None, self.mux, self.prompt, t=T, cfg=CFG, sd=self.sd, deadline_s=0,
                   probe=lambda m, p, s: calls.append(p))
        self.assertEqual(calls, [])

    def test_send_loop_stops_at_the_deadline_and_bounds_the_send(self):
        agents = [agent("t1"), agent("t2")]
        self.go(agents, cfg={"max_per_tick": 9, "deadline_s": 1000}, ctx=200000)
        self.assertLessEqual(max(self.timeouts), 10)
        self.prompts.clear()
        clock = {"now": 100.0}

        def slow_probe(m, pane, session):
            clock["now"] += 4
            return {"ctx": 200000, "idle_s": 9999}

        with mock.patch.object(rotate.time, "monotonic", lambda: clock["now"]):
            acts = rotate.run(agents, None, self.mux, self.prompt, t=T, cfg=CFG, sd=Path(tempfile.mkdtemp()),
                              probe=slow_probe, deadline_s=6)
        self.assertEqual((acts, self.prompts), ([], []))

    def test_compact_gap_blocks_a_second_compact(self):
        self.go([agent()], ctx=200000)
        self.assertEqual(self.go([agent()], t=T + 600, ctx=200000), [])
        self.assertEqual(len(self.go([agent()], t=T + 4 * 3600, ctx=200000)), 1)

    def test_refused_send_is_journaled_as_failed_and_backs_off(self):
        refuse = lambda pane, text, guard=None, timeout=None: (None, "no longer idle")
        self.assertEqual(self.go([agent()], prompt=refuse, ctx=200000)[0]["event"], "failed")
        self.assertEqual(self.go([agent()], t=T + 60, ctx=200000), [])
        self.assertEqual(self.go([agent()], t=T + 700, ctx=200000)[0]["event"], "compact")

    def test_dry_run_changes_nothing(self):
        acts = rotate.run([agent()], None, self.mux, self.prompt, t=T, cfg=CFG, sd=self.sd,
                          probe=probe_of(ctx=200000), dry_run=True)
        self.assertEqual(acts[0]["event"], "would-compact")
        self.assertEqual(self.prompts, [])
        self.assertFalse(rotate.journal(self.sd).exists())


class TestSwitch(Harness):
    def test_kill_switch_in_config(self):
        self.assertEqual(self.go([agent()], cfg={"enabled": False}, ctx=200000), [])

    def test_env_kill_switch(self):
        os.environ["ADW_ROTATE"] = "0"
        try:
            self.assertFalse(rotate.config()["enabled"])
        finally:
            del os.environ["ADW_ROTATE"]

    def test_config_file_overrides_known_keys_only(self):
        d = Path(tempfile.mkdtemp())
        (d / "rotate.json").write_text(json.dumps({"ctx_tokens": 99, "bogus": 1}))
        c = rotate.config(d)
        self.assertEqual(c["ctx_tokens"], 99)
        self.assertNotIn("bogus", c)


class TestDigest(Harness):
    def test_actions_and_stale_binaries_are_listed(self):
        self.go([agent()], ctx=200000, running="2.1.312")
        lines = rotate.recent(self.sd, T + 60)
        self.assertIn("compact `t7` [e1] context 200k", lines[0])
        self.assertIn("1 session(s) run a claude binary older", lines[1])


class TestProbe(unittest.TestCase):
    def run_probe(self, home, lines):
        proj = Path(home) / ".claude/projects/p"
        proj.mkdir(parents=True)
        (proj / "sid.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        r = subprocess.run(["python3", "-c", rotate.PROBE, str(os.getpid()), "sid"], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(home)})
        return json.loads(r.stdout)

    def test_context_and_idle_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as h:
            u = {"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}}
            r = self.run_probe(h, [u])
            self.assertEqual(r["ctx"], 125)
            self.assertLess(r["idle_s"], 60)

    def test_compact_boundary_after_usage_resets_context(self):
        with tempfile.TemporaryDirectory() as h:
            u = {"type": "assistant", "message": {"usage": {"input_tokens": 500000}}}
            b = {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 13806}}
            self.assertEqual(self.run_probe(h, [u, b])["ctx"], 13806)


if __name__ == "__main__":
    unittest.main()


class TestConfigHome(unittest.TestCase):
    def test_rotate_json_read_from_adw_home(self):
        home = Path(tempfile.mkdtemp())
        (home / "rotate.json").write_text(json.dumps({"enabled": False}))
        with mock.patch.dict(os.environ, {"ADW_HOME": str(home)}):
            self.assertFalse(rotate.config()["enabled"])
