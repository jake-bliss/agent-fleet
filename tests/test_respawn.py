import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = _Path(__file__).resolve().parent.parent
RESPAWN = ROOT / "bin/adw-respawn"
SESSION = "5e55-aaaa"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ["FAKE_STATE"]
d = json.load(open(st))
a = sys.argv[1:]
d["log"].append(a)
def out(x):
    json.dump(d, open(st, "w"))
    print(json.dumps(x)) if x is not None else None
def die(msg):
    json.dump(d, open(st, "w"))
    print(msg, file=sys.stderr)
    sys.exit(1)
T = d["terminals"]
cmd = a[0]
if cmd == "ls":
    out({"terminals": [{"address": k, "state": v["state"], "status": v["status"]["state"], "agent": "claude-code"} for k, v in T.items()]})
elif cmd == "get":
    if d.get("get_errors", 0) > 0 and len([c for c in d["log"] if c[0] == "get"]) > d.get("get_ok", 1):
        die("connection timed out")
    if a[1] not in T:
        print(json.dumps({"error": {"kind": "not_found", "message": "no terminal"}}))
        json.dump(d, open(st, "w"))
        sys.exit(3)
    out(T[a[1]])
elif cmd == "keys":
    if a[1] not in T:
        die("no such terminal")
    if a[2:] == ["enter"] and not d.get("stubborn"):
        T[a[1]]["state"] = "finished"
    out(None)
elif cmd == "send":
    out(None)
elif cmd == "rm":
    if a[1] not in T:
        die("no such terminal")
    if T[a[1]]["state"] != "finished":
        die("terminal is live")
    del T[a[1]]
    out(None)
elif cmd == "run":
    opts, labels, rest, i = {}, {}, [], 1
    while i < len(a):
        if a[i] == "--":
            rest = a[i + 1:]
            break
        if a[i] == "--label":
            k, _, v = a[i + 1].partition("=")
            labels[k] = v
            i += 2
        elif a[i] in ("--on", "--cwd", "--name"):
            opts[a[i]] = a[i + 1]
            i += 2
        else:
            opts[a[i]] = True
            i += 1
    if any(t["state"] != "finished" and t.get("name") == opts.get("--name") for t in T.values()):
        die("name in use")
    d["next"] += 1
    addr = ("%s/" % opts["--on"] if "--on" in opts else "") + "t%d" % d["next"]
    T[addr] = {"address": addr, "state": "live", "name": opts.get("--name"), "cwd": opts.get("--cwd"), "labels": labels,
               "argv": rest, "status": {"app": "claude-code", "state": "idle"}}
    if d.get("run_fails_after_create"):
        d["run_fails_after_create"] = False
        die("lost connection")
    out({"address": addr.split("/")[-1]})
else:
    die("unsupported " + cmd)
'''


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = _Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.adw = self.home / ".claude/adw"
        (self.adw / "asks").mkdir(parents=True)
        (self.adw / "epics/e1").mkdir(parents=True)
        (self.adw / "board").symlink_to(ROOT / "board")
        self.bin = self.tmp / "fake-hc"
        self.bin.write_text(FAKE)
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.state = self.tmp / "state.json"
        self.env = {**os.environ, "HOME": str(self.home), "ADW_HOME": str(self.adw), "HC_BIN": str(self.bin), "ADW_MUX_TEST": "1",
                    "FAKE_STATE": str(self.state), "ADW_SESSIONS_DIR": str(self.tmp / "sessions"), "ADW_LOCK_DIR": str(self.tmp / "locks"),
                    "ADW_RESPAWN_TIMEOUT": "2"}
        stub = self.tmp / "stubbin"
        stub.mkdir()
        (stub / "claude").write_text("#!/bin/sh\n")
        (stub / "claude").chmod(0o755)
        self.env["PATH"] = f"{stub}:{os.environ['PATH']}"
        self.env["SHELL"] = "/bin/sh"
        self.env.pop("ADW_HC_MACHINE", None)
        for k in [k for k in self.env if k.startswith("HC_") and k != "HC_BIN"]:
            del self.env[k]
        self.write({"terminals": {}, "log": [], "next": 20})

    def write(self, d):
        self.state.write_text(json.dumps(d))

    def data(self):
        return json.loads(self.state.read_text())

    def add(self, addr, state="idle", argv=None, labels=None, name="lead-1", cwd="/work", sess=SESSION, term_state="live"):
        d = self.data()
        d["terminals"][addr] = {"address": addr, "state": term_state, "name": name, "cwd": cwd, "labels": labels or {},
                                "argv": argv or ["claude", "--dangerously-skip-permissions", "--settings", "/s.json", "--resume", "old"],
                                "status": {"app": "claude-code", "state": state}}
        self.write(d)
        if sess:
            m, _, local = addr.rpartition("/")
            rec = self.tmp / "sessions" / (m or "local") / local
            rec.parent.mkdir(parents=True, exist_ok=True)
            rec.write_text(sess + "\n")

    def run_respawn(self, *args):
        return subprocess.run([sys.executable, str(RESPAWN), *args], env=self.env, capture_output=True, text=True, timeout=60)

    def journal(self, jid):
        return self.adw / "state/respawn" / f"{jid}.json"

    def ask(self, i, status, pane="t7", session=SESSION, **kw):
        (self.adw / "asks" / f"d-{i:06x}.json").write_text(json.dumps({"id": f"d-{i:06x}", "status": status, "created": i, "pane": pane, "session": session, **kw}))

    def read_ask(self, i):
        return json.loads((self.adw / "asks" / f"d-{i:06x}.json").read_text())

    def inner(self, addr):
        argv = self.data()["terminals"][addr]["argv"]
        self.assertEqual(argv[:2], ["zsh", "-lic"])
        return shlex.split(argv[2])


class TestRespawn(Rig):
    def test_local_roundtrip_keeps_name_labels_cwd_and_adds_resume(self):
        self.add("t7", labels={"herdr.space": "spc1", "adw.group": "g"})
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.data()
        self.assertNotIn("t7", d["terminals"])
        new = r.stdout.split()[0]
        t = d["terminals"][new]
        self.assertEqual((t["name"], t["cwd"]), ("lead-1", "/work"))
        self.assertEqual(t["labels"], {"herdr.space": "spc1", "adw.group": "g", "adw.respawn": "local-t7"})
        self.assertEqual(shlex.split(t["argv"][2].removeprefix("exec ")),
                         ["claude", "--dangerously-skip-permissions", "--settings", "/s.json", "--resume", SESSION])
        run = next(c for c in d["log"] if c[0] == "run")
        self.assertTrue({"--bg", "--no-parent", "--outlive-parent"} <= set(run))
        self.assertFalse(self.journal("local-t7").exists())

    def test_launch_uses_the_login_shell_not_a_hardcoded_zsh(self):
        self.add("t7")
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        run = next(c for c in self.data()["log"] if c[0] == "run")
        i = run.index("--")
        self.assertEqual(run[i + 1:i + 3], ["/bin/sh", "-lc"])

    def test_missing_launcher_fails_before_the_old_terminal_is_exited(self):
        self.add("t7", argv=["no-such-agent-binary", "--resume", "old"])
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not on PATH", r.stderr)
        d = self.data()
        self.assertIn("t7", d["terminals"])
        self.assertFalse(any(c[0] in ("keys", "send", "rm", "run") for c in d["log"]))
        self.assertFalse(self.journal("local-t7").exists())

    def test_bypass_added_when_missing(self):
        self.add("t7", argv=["claude", "--model", "opus", "--resume", "x"])
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.data()["terminals"][r.stdout.split()[0]]
        self.assertEqual(shlex.split(t["argv"][2].removeprefix("exec ")),
                         ["claude", "--dangerously-skip-permissions", "--model", "opus", "--resume", SESSION])

    def test_missing_session_record_falls_back_to_argv_then_refuses(self):
        self.add("t7", sess=None)
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--resume old", self.data()["terminals"][r.stdout.split()[0]]["argv"][2].replace("'", ""))
        self.setUp()
        self.add("t7", sess=None, argv=["claude"])
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no session id", r.stderr)
        self.assertIn("t7", self.data()["terminals"])
        self.assertFalse(any(c[0] in ("keys", "rm", "run") for c in self.data()["log"]))

    def test_working_refused_unless_forced(self):
        self.add("t7", state="working")
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("working", r.stderr)
        self.assertFalse(any(c[0] in ("keys", "rm", "run") for c in self.data()["log"]))
        self.assertEqual(self.run_respawn("--force", "t7").returncode, 0)

    def test_finish_timeout_leaves_terminal_untouched_and_retry_finishes(self):
        self.add("t7")
        d = self.data()
        d["stubborn"] = True
        self.write(d)
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not finish", r.stderr)
        d = self.data()
        self.assertEqual(d["terminals"]["t7"]["state"], "live")
        self.assertFalse(any(c[0] in ("rm", "run") for c in d["log"]))
        self.assertEqual(json.loads(self.journal("local-t7").read_text())["step"], "exiting")
        d["stubborn"] = False
        self.write(d)
        self.assertEqual(self.run_respawn("--retry", "t7").returncode, 0)

    def test_remote_machine_and_labels_roundtrip(self):
        self.add("node-a/t3", labels={"k": "v", "where": "x / y"}, name="mate", cwd="/home/j")
        r = self.run_respawn("node-a/t3")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = r.stdout.split()[0]
        self.assertRegex(new, r"^node-a/t\d+$")
        t = self.data()["terminals"][new]
        self.assertEqual((t["name"], t["cwd"]), ("mate", "/home/j"))
        self.assertEqual(t["labels"], {"k": "v", "where": "x / y", "adw.respawn": "node-a-t3"})
        run = next(c for c in self.data()["log"] if c[0] == "run")
        self.assertEqual(run[run.index("--on") + 1], "node-a")

    def retry_from(self, step, **extra):
        self.add("t7", term_state="finished" if step != "exiting" else "live")
        d = self.data()
        d["terminals"].pop("t7") if step in ("removed", "started", "rebound") else None
        self.write(d)
        j = {"old_address": "t7", "session": SESSION, "machine": None, "cwd": "/work", "name": "lead-1", "labels": {"a": "b"},
             "argv": ["claude", "--dangerously-skip-permissions"], "step": step, **extra}
        self.journal("local-t7").parent.mkdir(parents=True, exist_ok=True)
        self.journal("local-t7").write_text(json.dumps(j))

    def count_runs(self):
        return sum(1 for c in self.data()["log"] if c[0] == "run")

    def test_retry_from_removed_launches_once(self):
        self.retry_from("removed")
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.count_runs(), 1)
        self.assertFalse(self.journal("local-t7").exists())

    def test_retry_after_run_succeeded_but_journal_missed_it_adopts(self):
        self.retry_from("removed")
        d = self.data()
        d["terminals"]["t30"] = {"address": "t30", "state": "live", "name": "lead-1", "cwd": "/work", "labels": {"a": "b", "adw.respawn": "local-t7"},
                                 "argv": ["zsh"], "status": {"app": "claude-code", "state": "idle"}}
        self.write(d)
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.count_runs(), 0)
        self.assertEqual(r.stdout.split()[0], "t30")

    def test_run_that_dies_after_creating_is_adopted_by_retry(self):
        self.add("t7")
        d = self.data()
        d["run_fails_after_create"] = True
        self.write(d)
        self.assertEqual(self.run_respawn("t7").returncode, 1)
        self.assertEqual(json.loads(self.journal("local-t7").read_text())["step"], "removed")
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.count_runs(), 1)

    def test_retry_from_exiting_with_finished_terminal_removes_and_starts(self):
        self.retry_from("exiting")
        d = self.data()
        d["terminals"]["t7"]["state"] = "finished"
        self.write(d)
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.count_runs(), 1)

    def test_retry_from_started_and_rebound_do_not_launch_again(self):
        for step in ("started", "rebound"):
            self.setUp()
            self.retry_from(step)
            d = self.data()
            d["terminals"]["t40"] = {"address": "t40", "state": "live", "name": "lead-1", "cwd": "/work", "labels": {},
                                     "argv": ["zsh"], "status": {"app": "claude-code", "state": "idle"}}
            self.write(d)
            j = json.loads(self.journal("local-t7").read_text())
            j["new_address"] = "t40"
            self.journal("local-t7").write_text(json.dumps(j))
            self.ask(1, "open")
            r = self.run_respawn("--retry", "t7")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.count_runs(), 0)
            if step == "started":
                self.assertEqual(self.read_ask(1)["pane"], "t40")

    def test_decisions_and_lead_move_only_open_and_undelivered_for_the_session(self):
        self.add("t7")
        self.ask(1, "open")
        self.ask(2, "answered", attempts=3, undeliverable=True, delivery_error="x", last_attempt=5)
        self.ask(3, "answered", delivered=100)
        self.ask(4, "open", session="other")
        self.ask(5, "open", pane="t8")
        (self.adw / "epics/e1/lead.json").write_text(json.dumps({"pane": "t7", "session": SESSION, "at": "x"}))
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = r.stdout.split()[0]
        self.assertEqual(self.read_ask(1)["pane"], new)
        self.assertEqual(self.read_ask(2)["pane"], new)
        self.assertNotIn("delivered", self.read_ask(2))
        self.assertEqual(self.read_ask(3)["pane"], "t7")
        self.assertEqual(self.read_ask(4)["pane"], "t7")
        self.assertEqual(self.read_ask(5)["pane"], "t8")
        self.assertEqual(json.loads((self.adw / "epics/e1/lead.json").read_text())["pane"], new)


class TestReview(Rig):
    def test_lookup_error_aborts_with_journal_and_never_runs(self):
        self.add("t7")
        d = self.data()
        d["get_errors"], d["get_ok"] = 1, 1
        self.write(d)
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot look up", r.stderr)
        d = self.data()
        self.assertFalse(any(c[0] in ("run", "rm") for c in d["log"]))
        self.assertEqual(json.loads(self.journal("local-t7").read_text())["step"], "exiting")

    def test_respawn_of_a_respawned_terminal_keeps_custom_args(self):
        self.add("t7", argv=["claude", "--model", "opus", "--settings", "/custom.json", "--resume", "x"])
        first = self.run_respawn("t7").stdout.split()[0]
        d = self.data()
        d["terminals"][first]["labels"] = {}
        self.write(d)
        rec = self.tmp / "sessions/local" / first
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(SESSION + "\n")
        r = self.run_respawn(first)
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.data()["terminals"][r.stdout.split()[0]]
        self.assertEqual(shlex.split(t["argv"][2].removeprefix("exec ")),
                         ["claude", "--dangerously-skip-permissions", "--model", "opus", "--settings", "/custom.json", "--resume", SESSION])

    def test_force_restarts_a_blocked_terminal_with_escape_first(self):
        self.add("t7", state="needs-input")
        self.assertEqual(self.run_respawn("t7").returncode, 1)
        self.setUp()
        self.add("t7", state="needs-input")
        r = self.run_respawn("--force", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        keys = [c[2:] for c in self.data()["log"] if c[0] == "keys"]
        self.assertEqual(keys[0], ["esc"])
        self.assertEqual(keys[-1], ["enter"])


class TestMigratePane(Rig):
    def test_clears_delivery_failure_on_undelivered_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("adw_asks", ROOT / "board/asks.py")
        asks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(asks)
        asks.ASKS = self.adw / "asks"
        self.ask(2, "answered", attempts=3, undeliverable=True, delivery_error="x", last_attempt=5)
        self.ask(3, "answered", delivered=100, attempts=1)
        self.assertEqual(asks.migrate_pane("t7", "t9", SESSION), 1)
        a2 = self.read_ask(2)
        self.assertEqual(a2["pane"], "t9")
        for k in ("attempts", "undeliverable", "delivery_error", "last_attempt"):
            self.assertNotIn(k, a2)
        self.assertEqual(self.read_ask(3)["pane"], "t7")
        self.assertEqual(self.read_ask(3)["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
