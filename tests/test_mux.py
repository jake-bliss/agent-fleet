import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import os
import sys
import unittest
from unittest import mock

ROOT = _Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import asks  # noqa: E402
import board  # noqa: E402

LS = {"machines": [], "terminals": [
    {"address": "t2", "agent": "claude-code", "cwd": "/h", "name": "first-mate", "state": "live", "status": "working"},
    {"address": "node-b/t7", "agent": "claude-code", "cwd": "/t", "state": "live", "status": "idle"},
    {"address": "t3", "agent": "herdr", "state": "live"},
    {"address": "t1", "agent": "hc", "state": "live"}]}
GET = {"title": "work", "state": "live", "foreground": {"pid": 42}, "status": {"app": "claude-code", "state": "idle"}}


class FakeHC:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=15):
        self.calls.append(args)
        if args[:1] == ("ls",):
            return json.dumps(LS), None
        if args[:1] == ("get",):
            return json.dumps(GET), None
        if args[:1] == ("read",):
            return "screen text", None
        return "", None


class HcCase(unittest.TestCase):
    def setUp(self):
        self.hc = FakeHC()
        for p in (mock.patch.object(asks.mux, "hc", self.hc),
                  mock.patch.object(asks.mux, "session_from_pid", lambda pid, m: f"sess-{pid}-{m}")):
            p.start()
            self.addCleanup(p.stop)


class TestIds(unittest.TestCase):
    def test_both_backends_valid(self):
        for p in ("t7", "node-b/t7", "laptop/t7"):
            self.assertTrue(asks.mux.valid(p), p)
            self.assertTrue(board.PANE_ID.fullmatch(p), p)
            self.assertTrue(board.PIN_KEY.fullmatch("p:" + p), p)
        for p in ("", "t", "x7", "w1:p3", "dell/w1:p3", "t7;rm", "a b/t7"):
            self.assertFalse(asks.mux.valid(p), p)

    def test_backend_of_id(self):
        self.assertTrue(asks.mux.is_hc("node-b/t7"))
        self.assertFalse(asks.mux.is_hc("w1:p3"))

    def test_own_pane_is_bare_and_pane_ref_qualifies(self):
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t4", "ADW_HC_MACHINE": "node-b"}):
            self.assertEqual(asks.own_pane(), "t4")
            self.assertEqual(asks.pane_ref(asks.own_pane()), "node-b/t4")
            self.assertEqual(asks.mux.strip_self("node-b/t4"), "t4")
            self.assertEqual(asks.mux.strip_self("node-a/t4"), "node-a/t4")
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t4"}):
            self.assertEqual(asks.own_pane(), "t4")
            self.assertEqual(asks.pane_ref("t4"), "t4")
        self.assertIsNone(asks.own_pane())

    def test_wider_terminal_ids(self):
        self.assertTrue(asks.mux.valid("node-a/tab12"))
        self.assertTrue(board.PANE_ID.fullmatch("t7a"))

    def test_machine_ignores_a_shared_hc_machine_key(self):
        with mock.patch.object(asks.mux, "config", lambda: {"hc_machine": "from-file"}):
            self.assertIsNone(asks.mux.machine())

    def test_machine_falls_back_to_a_listed_node_hostname(self):
        nodes = lambda: {"hc_nodes": ["node-a", "node-b"]}
        with mock.patch.object(asks.mux, "config", nodes), \
                mock.patch.object(asks.mux.socket, "gethostname", return_value="node-a.lan"):
            self.assertEqual(asks.mux.machine(), "node-a")
        with mock.patch.object(asks.mux, "config", nodes), \
                mock.patch.object(asks.mux.socket, "gethostname", return_value="hub"):
            self.assertIsNone(asks.mux.machine())
        with mock.patch.dict(os.environ, {"ADW_HC_MACHINE": "node-b"}), mock.patch.object(asks.mux, "config", nodes), \
                mock.patch.object(asks.mux.socket, "gethostname", return_value="node-a"):
            self.assertEqual(asks.mux.machine(), "node-b")


class TestDispatch(HcCase):
    def test_hc_ids_use_hc(self):
        asks.prompt("node-b/t7", "hello")
        asks.send_keys("t7", ["enter"])
        self.assertEqual(asks.read_pane("t7", 50), ("screen text", None))
        self.assertEqual(asks.focus("t7")[1], asks.mux.NO_FOCUS)
        self.assertEqual(self.hc.calls, [("send", "node-b/t7", "hello"), ("keys", "t7", "enter"),
                                         ("read", "t7", "--lines", "50")])

    def test_agent_info(self):
        self.assertEqual(asks.agent_info("t2"), {"status": "idle", "session": "sess-42-None"})
        self.assertEqual(asks.agent_info("node-b/t7")["session"], "sess-42-node-b")

    def test_pane_ref_and_list_agents_shapes(self):
        self.assertEqual(asks.pane_ref("node-b/t7"), "node-b/t7")
        got = asks.list_agents()
        self.assertEqual([(a["pane_id"], a["backend"]) for a in got], [("t2", "hc"), ("node-b/t7", "hc")])
        with mock.patch.object(asks.mux, "available", lambda: False):
            self.assertIsNone(asks.list_agents())


class TestSelfPrefix(unittest.TestCase):
    def run_hc(self, *args):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(tuple(cmd[1:]))
            return mock.Mock(returncode=0, stdout=json.dumps(GET), stderr="")

        with mock.patch.object(asks.mux.subprocess, "run", fake_run):
            asks.mux.hc(*args)
        return calls[0]

    def test_hc_strips_the_callers_own_machine(self):
        with mock.patch.dict(os.environ, {"ADW_HC_MACHINE": "node-a"}):
            self.assertEqual(self.run_hc("get", "node-a/t3"), ("get", "t3"))
            self.assertEqual(self.run_hc("send", "node-a/t3", "x"), ("send", "t3", "x"))
            self.assertEqual(self.run_hc("get", "node-b/t3"), ("get", "node-b/t3"))
            self.assertEqual(self.run_hc("ls", "--json"), ("ls", "--json"))
        self.assertEqual(self.run_hc("get", "node-a/t3"), ("get", "node-a/t3"))

    def test_agent_info_of_own_pane_reaches_the_local_core(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(tuple(cmd[1:]))
            return mock.Mock(returncode=0, stdout=json.dumps(GET), stderr="")

        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t4", "ADW_HC_MACHINE": "node-b"}), \
                mock.patch.object(asks.mux.subprocess, "run", fake_run), \
                mock.patch.object(asks.mux, "session_from_pid", lambda p, m: "s"):
            self.assertEqual(asks.agent_info(asks.own_pane())["status"], "idle")
            self.assertEqual(asks.agent_info(asks.pane_ref(asks.own_pane()))["status"], "idle")
        self.assertEqual(calls, [("get", "t4", "--json")] * 2)


class TestSlashCommands(unittest.TestCase):
    def run_prompt(self, text, info=None):
        calls = []
        with mock.patch.object(asks.mux, "hc", lambda *a, **k: calls.append(a) or ("", None)), \
                mock.patch.object(asks.mux, "get", lambda p: info if info is not None else {"status": "idle", "session": "s"}), \
                mock.patch.object(asks.mux.time, "sleep", lambda s: calls.append(("sleep", s))):
            return asks.prompt("node-a/t3", text), calls

    def test_slash_command_is_typed_not_pasted(self):
        r, calls = self.run_prompt("/adw-lead Epic-1.x")
        self.assertEqual(r[1], None)
        self.assertEqual(calls, [("keys", "node-a/t3", "esc"),
                                 ("keys", "node-a/t3", "/", "a", "d", "w", "-", "l", "e", "a", "d", "space",
                                  "shift+e", "p", "i", "c", "-", "1", ".", "x"),
                                 ("sleep", 1), ("keys", "node-a/t3", "enter")])

    def test_plain_text_and_multiline_slash_are_sent(self):
        for text in ("hello /clear", "/clear\nmore"):
            r, calls = self.run_prompt(text)
            self.assertEqual(calls, [("send", "node-a/t3", text)])

    def test_blocked_or_gone_terminal_is_not_typed_into(self):
        r, calls = self.run_prompt("/clear", info={"status": "blocked", "session": None})
        self.assertIsNotNone(r[1])
        self.assertEqual(calls, [])

    def test_untypeable_character_is_an_error_not_a_pasted_message(self):
        r, calls = self.run_prompt("/adw-lead my_epic")
        self.assertIn("cannot type", r[1])
        self.assertEqual(calls, [])


class TestAutoCompactOverHc(unittest.TestCase):
    def test_lead_gets_a_bare_compact(self):
        import tempfile
        sent = []
        lead = {"role": "lead", "kind": "claude", "session": "s", "context": 10**9, "status": "idle", "attention": "idle",
                "last_active": 1, "epic": "e"}
        with mock.patch.object(board, "MATE", _Path(tempfile.mkdtemp())), \
                mock.patch.object(board.asks, "prompt", lambda p, t: sent.append((p, t)) or ("", None)):
            board.auto_compact({"agents": [{**lead, "pane": "node-a/t3"}]}, {})
        self.assertEqual([(p, t.split()[0]) for p, t in sent], [("node-a/t3", "/compact")])


class TestSenderLock(unittest.TestCase):
    def test_concurrent_senders_do_not_interleave(self):
        import threading
        log = []

        def fake_hc(*a, **k):
            log.append(a[2:] if a[0] == "keys" else a[:1])
            time_sleep(0.01)
            return "", None

        time_sleep = __import__("time").sleep
        with mock.patch.object(asks.mux, "hc", fake_hc), \
                mock.patch.object(asks.mux, "get", lambda p: {"status": "idle", "session": "s"}), \
                mock.patch.object(asks.mux.time, "sleep", lambda s: None):
            ts = [threading.Thread(target=asks.prompt, args=("t3", "/clear")) for _ in range(3)] + \
                 [threading.Thread(target=asks.prompt, args=("t3", "plain text")) for _ in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        chars = ("/", "c", "l", "e", "a", "r")
        runs, i = [], 0
        while i < len(log):
            if log[i] == ("send",):
                runs.append("send")
                i += 1
            else:
                self.assertEqual(log[i:i + 3], [("esc",), chars, ("enter",)])
                runs.append("slash")
                i += 3
        self.assertEqual(sorted(runs), ["send"] * 3 + ["slash"] * 3)

    def test_busy_terminal_times_out_with_an_error_and_types_nothing(self):
        calls = []
        with mock.patch.object(asks.mux, "hc", lambda *a, **k: calls.append(a) or ("", None)):
            with asks.mux.terminal_lock("node-a/t9"):
                self.assertIn("busy", asks.mux.prompt("node-a/t9", "hello", timeout=0.1)[1])
        self.assertEqual(calls, [])


class TestLockKeyIsNormalised(unittest.TestCase):
    def test_bare_and_own_machine_addresses_share_a_lock(self):
        calls = []
        with mock.patch.dict(os.environ, {"ADW_HC_MACHINE": "hub"}), \
                mock.patch.object(asks.mux, "hc", lambda *a, **k: calls.append(a) or ("", None)):
            with asks.mux.terminal_lock("t3"):
                self.assertIn("busy", asks.mux.prompt("hub/t3", "hi", timeout=0.1)[1])
            with asks.mux.terminal_lock("hub/t3"):
                self.assertIn("busy", asks.mux.prompt("t3", "hi", timeout=0.1)[1])
            with asks.mux.terminal_lock("node-a/t3"):
                self.assertIsNone(asks.mux.prompt("t3", "hi", timeout=0.1)[1])
        self.assertEqual(len(calls), 1)


class TestStatus(unittest.TestCase):
    def test_unknown_states_fail_closed(self):
        n = asks.mux.norm_status
        self.assertEqual([n("idle"), n("working"), n("done"), n({"app": "x", "state": "idle"})], ["idle", "working", "done", "idle"])
        for v in (None, "", "weird", "blocked", {"state": None}, {}):
            self.assertEqual(n(v), "blocked", v)


class TestFiles(unittest.TestCase):
    def test_remote_pid_reads_use_paths_relative_to_that_home(self):
        calls = []
        with mock.patch.object(asks.mux, "hc", lambda *a, **k: calls.append(a) or ('{"sessionId": "abc"}', None)):
            self.assertEqual(asks.mux.session_from_pid(42, "node-a"), "abc")
        self.assertEqual(calls, [("file", "read", "--on", "node-a", ".claude/sessions/42.json")])

    def test_local_reads_use_the_expanded_home_and_the_hook_path(self):
        import tempfile
        home = _Path(tempfile.mkdtemp())
        (home / ".claude/sessions").mkdir(parents=True)
        (home / ".claude/sessions/42.json").write_text('{"sessionId": "loc"}')
        (home / ".claude/adw/hc-sessions/local").mkdir(parents=True)
        (home / ".claude/adw/hc-sessions/local/t3").write_text("rec\n")
        (home / ".claude/adw/hc-sessions/node-a").mkdir(parents=True)
        (home / ".claude/adw/hc-sessions/node-a/t4").write_text("node-a-rec\n")
        with mock.patch.dict(os.environ, {"HOME": str(home), "ADW_SESSIONS_DIR": str(home / ".claude/adw/hc-sessions")}):
            self.assertEqual(asks.mux.session_from_pid(42, None), "loc")
            self.assertEqual(asks.mux.read_session_record("t3", None), "rec")
            with mock.patch.object(asks.mux, "config", lambda: {"shared_machines": ["node-a"]}):
                self.assertEqual(asks.mux.read_session_record("t4", "node-a"), "node-a-rec")
            with mock.patch.dict(os.environ, {"ADW_HC_MACHINE": "node-a"}):
                self.assertIsNone(asks.mux.read_session_record("t3", None))

    def test_other_machines_are_read_over_ssh_at_the_target_from_machine_ls(self):
        ls = {"machines": [{"name": "laptop", "target": "u@laptop.ts"}, {"name": "node-b", "target": "u@node-b-host"}]}
        calls = []
        with mock.patch.object(asks.mux, "hc", lambda *a, **k: ('%s' % json.dumps(ls), None)), \
                mock.patch.object(asks.mux, "ssh_cat", lambda t, path: calls.append((t, path)) or "laptop-sess\n"):
            self.assertEqual(asks.mux.read_session_record("t7", "laptop"), "laptop-sess")
            self.assertIsNone(asks.mux.read_session_record("t7", "nowhere"))
        self.assertEqual(calls, [("u@laptop.ts", "~/.claude/adw/hc-sessions/laptop/t7")])

    def test_session_falls_back_to_resume_in_argv(self):
        rec = {"argv": ["claude", "--resume", "abc-1", "--dangerously-skip-permissions"]}
        with mock.patch.object(asks.mux, "session_from_pid", lambda p, m: None), \
                mock.patch.object(asks.mux, "read_session_record", lambda t, m: None):
            self.assertEqual(asks.mux.session_of("t3", rec), "abc-1")
            self.assertIsNone(asks.mux.session_of("t3", {"argv": ["claude"]}))


class TestOwnMachineIsLocal(unittest.TestCase):
    def test_own_machine_address_reads_locally_with_no_hc_call(self):
        import tempfile
        home = _Path(tempfile.mkdtemp())
        (home / ".claude/sessions").mkdir(parents=True)
        (home / ".claude/sessions/42.json").write_text('{"sessionId": "mine"}')
        with mock.patch.dict(os.environ, {"HOME": str(home), "ADW_HC_MACHINE": "hub"}), \
                mock.patch.object(asks.mux, "hc", side_effect=AssertionError("hc called")):
            self.assertEqual(asks.mux.session_of("hub/t3", {"foreground": {"pid": 42}}), "mine")
            self.assertEqual(asks.mux.session_from_pid(42, "hub"), "mine")
            self.assertIsNone(asks.mux.read_session_record("t3", "hub"))


class TestStart(unittest.TestCase):
    def start(self, out, on):
        with mock.patch.object(asks.mux, "hc", lambda *a, **k: (out, None)):
            return asks.mux.start("n", "/x", ["claude"], on=on)

    def test_address_is_qualified_for_remote_runs(self):
        self.assertEqual(self.start('{"address": "t2"}', "node-b"), ("node-b/t2", None))
        self.assertEqual(self.start('{"address": "node-b/t2"}', "node-b"), ("node-b/t2", None))
        self.assertEqual(self.start('{"address": "t2"}', None), ("t2", None))

    def test_wait_idle(self):
        seq = iter([None, {"status": "blocked"}, {"status": "idle"}])
        with mock.patch.object(asks.mux, "get", lambda p: next(seq)):
            self.assertTrue(asks.mux.wait_idle("t2", sleep=lambda s: None))
        with mock.patch.object(asks.mux, "get", lambda p: {"status": "blocked"}):
            self.assertFalse(asks.mux.wait_idle("t2", timeout=0, sleep=lambda s: None))


class TestHook(unittest.TestCase):
    HOOK = ROOT / "hooks/hc-session-state.sh"

    def run_hook(self, home, action, env, stdin):
        import subprocess
        base = {"PATH": os.environ["PATH"], "HOME": str(home), "ADW_HOME": str(home / ".claude/adw"), "ADW_SESSIONS_DIR": str(home / ".claude/adw/hc-sessions")}
        subprocess.run(["sh", str(self.HOOK), action], input=stdin, text=True, env={**base, **env}, check=True, cwd=home)

    def test_records_per_machine_and_terminal_and_deletes_on_end(self):
        import tempfile
        home = _Path(tempfile.mkdtemp())
        sid = "2ff623c7-d9a1-4cc4-8788-97e1c818b547"
        f = home / ".claude/adw/hc-sessions/node-a/t3"
        env = {"HC_TERMINAL_ID": "t3", "ADW_HC_MACHINE": "node-a"}
        self.run_hook(home, "start", env, json.dumps({"session_id": sid}))
        self.assertEqual(f.read_text().strip(), sid)
        self.run_hook(home, "end", env, "{}")
        self.assertFalse(f.exists())
        self.run_hook(home, "start", {**env, "HERDR_PANE_ID": "w1:p1"}, json.dumps({"session_id": sid}))
        self.assertEqual(f.read_text().strip(), sid)
        self.run_hook(home, "start", {"HC_TERMINAL_ID": "t3"}, json.dumps({"session_id": sid}))
        self.assertTrue((home / ".claude/adw/hc-sessions/local/t3").exists())

    def test_a_listed_node_records_under_its_hostname_without_the_env_var(self):
        import shutil
        import socket
        import tempfile
        home = _Path(tempfile.mkdtemp())
        (home / ".claude/adw/board").mkdir(parents=True)
        shutil.copy(ROOT / "board/mux.py", home / ".claude/adw/board/mux.py")
        host = socket.gethostname().split(".")[0]
        (home / ".claude/adw/mux.json").write_text(json.dumps({"hc_nodes": [host]}))
        self.run_hook(home, "start", {"HC_TERMINAL_ID": "t4"}, json.dumps({"session_id": "2ff623c7-d9a1"}))
        self.assertTrue((home / f".claude/adw/hc-sessions/{host}/t4").exists())


class TestListing(HcCase):
    def test_hc_agents_normalised(self):
        agents = asks.mux.list_agents()
        self.assertEqual([a["pane_id"] for a in agents], ["t2", "node-b/t7"])
        a = agents[1]
        self.assertEqual((a["agent"], a["agent_status"], a["machine"], a["backend"]), ("claude", "idle", "node-b", "hc"))
        self.assertEqual(a["agent_session"]["value"], "sess-42-node-b")
        self.assertEqual(agents[0]["name"], "first-mate")

    def test_unavailable_is_none(self):
        with mock.patch.object(asks.mux, "hc", lambda *a, **k: (None, "down")):
            self.assertIsNone(asks.list_agents())


class TestBoardHc(HcCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(board.asks.mux, "hc", self.hc),
                  mock.patch.object(board.asks.mux, "session_from_pid", lambda pid, m: "s1")):
            p.start()
            self.addCleanup(p.stop)

    def test_pane_view(self):
        v = board.pane_view("node-b/t7", 100)
        self.assertEqual((v["text"], v["status"]), ("screen text", "idle"))

class TestGuard(unittest.TestCase):
    def test_real_socket_is_refused(self):
        before = _isolate.HC_LOG.read_text() if _isolate.HC_LOG.exists() else ""
        with mock.patch.dict(os.environ, {"HC_TERMINAL_SOCKET": "/real/default.sock"}):
            with self.assertRaises(RuntimeError):
                asks.mux.hc("send", "t2", "x")
        after = _isolate.HC_LOG.read_text() if _isolate.HC_LOG.exists() else ""
        self.assertEqual(before, after)

    def test_real_binary_is_refused(self):
        with mock.patch.dict(os.environ, {"HC_BIN": "/usr/local/bin/herdr-core"}):
            with self.assertRaises(RuntimeError):
                asks.mux.hc("ls")


if __name__ == "__main__":
    unittest.main()
