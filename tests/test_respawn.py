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
    out({"terminals": [{"address": k, "state": v["state"], "status": (v["status"] or {}).get("state"), "agent": "claude-code"} for k, v in T.items()]})
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
    if d.get("rm_fails"):
        die("rm failed")
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
               "argv": rest, "status": None if d.get("no_status") else {"app": "claude-code", "state": "idle"}}
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
        self.bin = self.tmp / "fake-hc"
        self.bin.write_text(FAKE)
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.state = self.tmp / "state.json"
        self.env = {**os.environ, "HOME": str(self.home), "ADW_DIR": str(ROOT / "board"), "ADW_HOME": str(self.home / ".claude/adw"), "HC_BIN": str(self.bin), "ADW_MUX_TEST": "1",
                    "FAKE_STATE": str(self.state), "ADW_SESSIONS_DIR": str(self.tmp / "sessions"), "ADW_LOCK_DIR": str(self.tmp / "locks"),
                    "ADW_RESPAWN_TIMEOUT": "2"}
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

    def launched(self, t):
        argv = t["argv"]
        return shlex.split(argv[2].removeprefix("exec ")) if len(argv) == 3 and argv[1].startswith("-") else argv


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
        self.assertEqual(self.launched(t),
                         ["claude", "--dangerously-skip-permissions", "--settings", "/s.json", "--resume", SESSION])
        run = next(c for c in d["log"] if c[0] == "run")
        self.assertTrue({"--bg", "--no-parent", "--outlive-parent"} <= set(run))
        self.assertFalse(self.journal("local-t7").exists())

    def test_bypass_added_when_missing(self):
        self.add("t7", argv=["claude", "--model", "opus", "--resume", "x"])
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.data()["terminals"][r.stdout.split()[0]]
        self.assertEqual(self.launched(t),
                         ["claude", "--dangerously-skip-permissions", "--model", "opus", "--resume", SESSION])

    def test_missing_session_record_falls_back_to_argv_then_refuses(self):
        self.add("t7", sess=None)
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("old", self.launched(self.data()["terminals"][r.stdout.split()[0]]))
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
        self.add("node-b/t3", labels={"k": "v", "where": "x / y"}, name="mate", cwd="/home/user")
        r = self.run_respawn("node-b/t3")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = r.stdout.split()[0]
        self.assertRegex(new, r"^node-b/t\d+$")
        t = self.data()["terminals"][new]
        self.assertEqual((t["name"], t["cwd"]), ("mate", "/home/user"))
        self.assertEqual(t["labels"], {"k": "v", "where": "x / y", "adw.respawn": "node-b-t3"})
        run = next(c for c in self.data()["log"] if c[0] == "run")
        self.assertEqual(run[run.index("--on") + 1], "node-b")

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
        self.assertEqual(json.loads(self.journal("local-t7").read_text())["step"], "exited")
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


class TestOrder(Rig):
    WRAP = ["/bin/bash", "-lc", "exec claude --dangerously-skip-permissions --resume old"]

    def test_wrapper_is_reused_and_direct_argv_stays_direct(self):
        self.add("t7", argv=self.WRAP)
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.data()["terminals"][r.stdout.split()[0]]["argv"]
        self.assertEqual(argv[:2], ["/bin/bash", "-lc"])
        self.assertEqual(shlex.split(argv[2]), ["exec", "claude", "--dangerously-skip-permissions", "--resume", SESSION])
        self.setUp()
        self.add("t7")
        r = self.run_respawn("t7")
        self.assertEqual(self.data()["terminals"][r.stdout.split()[0]]["argv"][0], "claude")

    def test_old_terminal_removed_only_after_replacement_reports(self):
        self.add("t7")
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.data()["log"]
        i_run = next(i for i, c in enumerate(log) if c[0] == "run")
        i_rm = next(i for i, c in enumerate(log) if c[0] == "rm")
        self.assertLess(i_run, i_rm)
        self.assertTrue(any(c[0] == "get" and c[1] == "t21" for c in log[i_run:i_rm]))
        self.assertEqual(log[i_rm], ["rm", "t7"])

    def never_reports(self):
        d = self.data()
        d["no_status"] = True
        self.write(d)

    def test_replacement_never_reports_keeps_old_and_journal_then_retry_cleans_up(self):
        self.add("t7")
        self.never_reports()
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("adw-respawn --retry t7", r.stderr)
        self.assertIn("t21", r.stderr)
        d = self.data()
        self.assertIn("t7", d["terminals"])
        self.assertEqual(d["terminals"]["t7"]["state"], "finished")
        self.assertFalse(any(c[0] == "rm" for c in d["log"]))
        self.assertTrue(self.journal("local-t7").exists())
        d["terminals"]["t21"]["state"] = "finished"
        d["no_status"] = False
        self.write(d)
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.data()
        self.assertNotIn("t21", d["terminals"])
        self.assertNotIn("t7", d["terminals"])
        self.assertEqual(self.count_runs(), 2)

    def count_runs(self):
        return sum(1 for c in self.data()["log"] if c[0] == "run")

    def test_retry_at_started_with_lookup_error_does_not_launch_a_second(self):
        self.add("t7", term_state="finished")
        self.add("t40", name="lead-1")
        j = {"old_address": "t7", "session": SESSION, "machine": None, "cwd": "/work", "name": "lead-1", "labels": {},
             "argv": ["claude"], "step": "started", "new_address": "t40"}
        self.journal("local-t7").parent.mkdir(parents=True, exist_ok=True)
        self.journal("local-t7").write_text(json.dumps(j))
        d = self.data()
        d["get_errors"], d["get_ok"] = 1, 0
        self.write(d)
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot look up", r.stderr)
        self.assertEqual(self.count_runs(), 0)
        self.assertEqual(json.loads(self.journal("local-t7").read_text())["step"], "started")

    def test_decisions_on_a_dead_prior_replacement_follow_to_the_final_one(self):
        self.add("t7", term_state="finished")
        self.add("t40", name="lead-1", term_state="finished")
        j = {"old_address": "t7", "session": SESSION, "machine": None, "cwd": "/work", "name": "lead-1", "labels": {},
             "argv": ["claude"], "step": "started", "new_address": "t40"}
        self.journal("local-t7").parent.mkdir(parents=True, exist_ok=True)
        self.journal("local-t7").write_text(json.dumps(j))
        self.ask(1, "open", pane="t40")
        self.ask(2, "open", pane="t7")
        (self.adw / "epics/e1/lead.json").write_text(json.dumps({"pane": "t40", "session": SESSION, "at": "x"}))
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = r.stdout.split()[0]
        self.assertNotEqual(new, "t40")
        self.assertEqual(self.read_ask(1)["pane"], new)
        self.assertEqual(self.read_ask(2)["pane"], new)
        self.assertEqual(json.loads((self.adw / "epics/e1/lead.json").read_text())["pane"], new)

    def test_rm_failure_after_success_is_a_warning(self):
        self.add("t7")
        d = self.data()
        d["rm_fails"] = True
        self.write(d)
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("warning", r.stderr)
        self.assertFalse(self.journal("local-t7").exists())

    def test_live_name_collision_refused_before_exit(self):
        self.add("t7")
        self.add("t8")
        r = self.run_respawn("t7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("t8", r.stderr)
        self.assertFalse(any(c[0] in ("keys", "send", "rm", "run") for c in self.data()["log"]))
        self.assertFalse(self.journal("local-t7").exists())

    def test_old_journal_at_removed_resumes(self):
        j = {"old_address": "t7", "session": SESSION, "machine": None, "cwd": "/work", "name": "lead-1", "labels": {},
             "argv": self.WRAP, "step": "removed"}
        self.journal("local-t7").parent.mkdir(parents=True, exist_ok=True)
        self.journal("local-t7").write_text(json.dumps(j))
        r = self.run_respawn("--retry", "t7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.data()["terminals"][r.stdout.split()[0]]["argv"][:2], ["/bin/bash", "-lc"])
        self.assertFalse(self.journal("local-t7").exists())


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
        self.assertEqual(self.launched(t),
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
