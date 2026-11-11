import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENROLL = ROOT / "bin" / "aw-lead-enroll"
AS = ROOT / "bin" / "aw-as"
LEAD_TOKEN = "FAKE-LEAD-TOKEN-not-real"
ENROLLER_TOKEN = "FAKE-ENROLLER-TOKEN-not-real"
URL = "http://aw.invalid"

FAKE_AW = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_ARGV"
echo "$1 $2 $3 url=${AW_URL:-} token=${AW_TOKEN:-}" >> "$FAKE_ENV"
case "$*" in
  *"agents list"*) [[ -z "${FAKE_LIST_FAIL:-}" ]] || exit 1; echo '{"agents":[["other","alice","hc-pane","h/t0","agent","-",1],["lead-ep","'"${FAKE_OWNER:-fleet}"'","hc-pane","h/t1","agent","-",1]]}' ;;
  *"agents register"*) [[ -z "${FAKE_REGISTER_FAIL:-}" ]] || { echo "agent exists" >&2; exit 1; } ;;
  *"agents delivery"*|*"agents target"*) [[ -z "${FAKE_TARGET_FAIL:-}" ]] || exit 1 ;;
  *"tokens mint"*) [[ -z "${FAKE_MINT_FAIL:-}" ]] || exit 1; echo '{"agent":"x","token":"'"$FAKE_MINT_TOKEN"'"}' ;;
  *"tokens rotate"*) echo '{"agent":"x","token":"'"$FAKE_MINT_TOKEN"'"}' ;;
esac
exit 0
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aw-lead-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.cfg = self.home / ".config" / "aw"
        self.cfg.mkdir(parents=True)
        self.cfg.chmod(0o700)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        aw = self.bin / "aw"
        aw.write_text(FAKE_AW)
        aw.chmod(0o755)
        self.argv = self.tmp / "argv.log"
        self.envlog = self.tmp / "env.log"
        self.argv.write_text("")
        self.envlog.write_text("")

    def write_cfg(self, name, token, mode=0o600):
        f = self.cfg / name
        f.write_text(f"AW_URL={URL}\nAW_TOKEN={token}\n")
        f.chmod(mode)
        return f

    def run_script(self, script, *args, **extra):
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home), "FAKE_ARGV": str(self.argv),
               "FAKE_ENV": str(self.envlog), "FAKE_MINT_TOKEN": LEAD_TOKEN}
        env.update(extra)
        return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def calls(self):
        return [l for l in self.argv.read_text().splitlines() if l]


class EnrollTest(Base):
    def test_first_run_registers_and_mints(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "ep", "node-b/t18")
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], "admin agents register fleet lead-ep --delivery hc-pane --target node-b/t18")
        self.assertEqual(calls[1], "admin agents delivery lead-ep hc-pane --target node-b/t18")
        self.assertEqual(calls[2], "admin tokens mint lead-ep --json")
        f = self.cfg / "lead-ep"
        self.assertEqual(f.read_text(), f"AW_URL={URL}\nAW_TOKEN={LEAD_TOKEN}\n")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual((self.cfg / "lead-targets/ep").read_text(), "node-b/t18")
        self.assertIn(f"token={ENROLLER_TOKEN}", self.envlog.read_text())
        for blob in (p.stdout, p.stderr, self.argv.read_text()):
            self.assertNotIn(LEAD_TOKEN, blob)
            self.assertNotIn(ENROLLER_TOKEN, blob)

    def test_enroller_token_never_in_any_exec_argv(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        shimlog = self.tmp / "shim.log"
        for name in ("env", "timeout"):
            real = shutil.which(name)
            sh = self.bin / name
            sh.write_text(f'#!/bin/sh\necho "{name} $*" >> "{shimlog}"\nexec {real} "$@"\n')
            sh.chmod(0o755)
        p = self.run_script(ENROLL, "ep", "node-b/t18")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("timeout 8 aw", shimlog.read_text())
        self.assertNotIn(ENROLLER_TOKEN, shimlog.read_text())
        self.assertIn(f"token={ENROLLER_TOKEN}", self.envlog.read_text())

    def test_access_pair_copied_to_lead_file(self):
        f = self.write_cfg("enroller", ENROLLER_TOKEN)
        with f.open("a") as h:
            h.write("AW_ACCESS_CLIENT_ID=fake-id\nAW_ACCESS_CLIENT_SECRET=fake-secret\n")
        self.run_script(ENROLL, "ep", "node-b/t18")
        text = (self.cfg / "lead-ep").read_text()
        self.assertIn("AW_ACCESS_CLIENT_ID=fake-id\n", text)
        self.assertIn("AW_ACCESS_CLIENT_SECRET=fake-secret\n", text)

    def test_failed_target_writes_no_cache(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "ep", "node-b/t18", FAKE_TARGET_FAIL="1")
        self.assertEqual(p.returncode, 0)
        self.assertFalse((self.cfg / "lead-targets/ep").exists())
        self.assertFalse((self.cfg / "lead-ep").exists())
        self.write_cfg("lead-ep", LEAD_TOKEN)
        (self.cfg / "lead-targets").mkdir()
        (self.cfg / "lead-targets/ep").write_text("old/t1")
        p = self.run_script(ENROLL, "ep", "node-b/t18", FAKE_TARGET_FAIL="1")
        self.assertEqual((self.cfg / "lead-targets/ep").read_text(), "old/t1")

    def test_retarget_only_makes_one_call_and_no_token_file(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "--retarget-only", "ep", "node-b/t7")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls(), ["admin agents list --json", "admin agents target lead-ep node-b/t7"])
        self.assertFalse((self.cfg / "lead-ep").exists())
        self.assertFalse((self.cfg / "lead-targets/ep").exists())
        for blob in (p.stdout, p.stderr, self.argv.read_text()):
            self.assertNotIn(ENROLLER_TOKEN, blob)

    def test_foreign_owned_agent_is_never_retargeted_or_minted(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        for args, extra in ((("ep", "h/t1"), {"FAKE_REGISTER_FAIL": "1"}), (("--retarget-only", "ep", "h/t1"), {})):
            self.argv.write_text("")
            p = self.run_script(ENROLL, *args, FAKE_OWNER="alice", **extra)
            self.assertEqual(p.returncode, 0)
            self.assertIn("not an agent of human fleet", p.stderr)
            self.assertFalse(any(" target " in c or "mint" in c for c in self.calls()))
            self.assertFalse((self.cfg / "lead-ep").exists())

    def test_list_failure_refuses(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "ep", "h/t1", FAKE_REGISTER_FAIL="1", FAKE_LIST_FAIL="1")
        self.assertEqual(p.returncode, 0)
        self.assertFalse(any(" target " in c or "mint" in c for c in self.calls()))

    def test_cache_names_do_not_collide_with_credentials(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        for epic in ("billing", "billing.target"):
            self.run_script(ENROLL, epic, f"h/{epic}")
        self.assertEqual((self.cfg / "lead-billing").read_text().count("AW_TOKEN="), 1)
        self.assertTrue((self.cfg / "lead-billing.target").read_text().startswith("AW_URL="))
        self.assertEqual((self.cfg / "lead-targets" / "billing").read_text(), "h/billing")
        self.assertEqual(stat.S_IMODE((self.cfg / "lead-targets").stat().st_mode), 0o700)

    def test_retarget_only_updates_cache_when_lead_file_exists(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        self.write_cfg("lead-ep", LEAD_TOKEN)
        self.run_script(ENROLL, "--retarget-only", "ep", "node-b/t8")
        self.assertEqual((self.cfg / "lead-targets/ep").read_text(), "node-b/t8")
        self.assertEqual((self.cfg / "lead-ep").read_text(), f"AW_URL={URL}\nAW_TOKEN={LEAD_TOKEN}\n")

    def test_retarget_only_no_enroller_warns(self):
        p = self.run_script(ENROLL, "--retarget-only", "ep", "node-b/t7")
        self.assertEqual(p.returncode, 0)
        self.assertIn("warning", p.stderr)
        self.assertEqual(self.calls(), [])

    def test_same_target_makes_no_calls(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        self.run_script(ENROLL, "ep", "node-b/t18")
        self.argv.write_text("")
        p = self.run_script(ENROLL, "ep", "node-b/t18")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_changed_target_retargets(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        self.run_script(ENROLL, "ep", "node-b/t18")
        self.argv.write_text("")
        p = self.run_script(ENROLL, "ep", "node-b/t22")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), ["admin agents target lead-ep node-b/t22"])
        self.assertEqual((self.cfg / "lead-targets/ep").read_text(), "node-b/t22")

    def test_existing_agent_without_local_token_is_retargeted(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "ep", "node-b/t5", FAKE_REGISTER_FAIL="1")
        self.assertEqual(p.returncode, 0)
        calls = self.calls()
        self.assertIn("admin agents delivery lead-ep hc-pane --target node-b/t5", calls)
        self.assertIn("admin tokens mint lead-ep --json", calls)
        self.assertTrue((self.cfg / "lead-ep").exists())

    def test_existing_fleet_agent_gets_hc_pane_delivery(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "ep", "node-b/t5", FAKE_REGISTER_FAIL="1")
        self.assertEqual(p.returncode, 0)
        self.assertIn("admin agents delivery lead-ep hc-pane --target node-b/t5", self.calls())

    def test_mint_failure_never_rotates_and_writes_nothing(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        p = self.run_script(ENROLL, "ep", "node-b/t5", FAKE_MINT_FAIL="1")
        self.assertEqual(p.returncode, 0)
        self.assertFalse(any("rotate" in c for c in self.calls()))
        self.assertFalse((self.cfg / "lead-ep").exists())
        self.assertFalse((self.cfg / "lead-targets/ep").exists())

    def test_no_enroller_warns_and_exits_zero(self):
        p = self.run_script(ENROLL, "ep", "node-b/t18")
        self.assertEqual(p.returncode, 0)
        self.assertIn("warning", p.stderr)
        self.assertEqual(p.stderr.count("\n"), 1)
        self.assertEqual(self.calls(), [])

    def test_loose_enroller_mode_refused(self):
        self.write_cfg("enroller", ENROLLER_TOKEN, mode=0o644)
        p = self.run_script(ENROLL, "ep", "node-b/t18")
        self.assertEqual(p.returncode, 0)
        self.assertIn("0600", p.stderr)
        self.assertEqual(self.calls(), [])

    def test_no_aw_warns(self):
        self.write_cfg("enroller", ENROLLER_TOKEN)
        (self.bin / "aw").unlink()
        shim = self.tmp / "nobin"
        shim.mkdir()
        for t in ("bash", "jq", "stat", "mkdir", "chmod", "cat", "env", "mktemp", "mv", "rm", "timeout"):
            w = shutil.which(t)
            if w:
                (shim / t).symlink_to(w)
        p = self.run_script(ENROLL, "ep", "node-b/t18", PATH=str(shim))
        self.assertEqual(p.returncode, 0)
        self.assertIn("aw not installed", p.stderr)


class AsTest(Base):
    def test_refuses_loose_mode(self):
        self.write_cfg("lead-ep", LEAD_TOKEN, mode=0o644)
        p = self.run_script(AS, "lead-ep", "inbox")
        self.assertEqual(p.returncode, 1)
        self.assertIn("0600", p.stderr)
        self.assertEqual(self.calls(), [])

    def test_refuses_foreign_owner(self):
        f = self.write_cfg("lead-ep", LEAD_TOKEN)
        stub = self.tmp / "stubs"
        stub.mkdir()
        (stub / "id").write_text("#!/bin/sh\necho 99999\n")
        (stub / "id").chmod(0o755)
        p = self.run_script(AS, "lead-ep", "inbox", PATH=f"{stub}:{self.bin}:{os.environ['PATH']}")
        self.assertEqual(p.returncode, 1)
        self.assertIn("owned by you", p.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(f.exists())

    def test_passes_args_and_uses_lead_identity(self):
        self.write_cfg("config", "FAKE-DEFAULT-TOKEN-not-real")
        self.write_cfg("lead-ep", LEAD_TOKEN)
        p = self.run_script(AS, "lead-ep", "inbox", "--json", AW_TOKEN="FAKE-DEFAULT-TOKEN-not-real")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls(), ["inbox --json"])
        self.assertIn(f"token={LEAD_TOKEN}", self.envlog.read_text())
        self.assertNotIn("FAKE-DEFAULT-TOKEN", self.envlog.read_text())

    def test_only_known_keys_exported(self):
        f = self.write_cfg("lead-ep", LEAD_TOKEN)
        with f.open("a") as h:
            h.write("PATH=/evil\n")
        p = self.run_script(AS, "lead-ep", "inbox")
        self.assertEqual(p.returncode, 0, p.stderr)


class StatusHookTest(unittest.TestCase):
    def setUp(self):
        sys_path = str(ROOT)
        if sys_path not in _sys.path:
            _sys.path.insert(0, sys_path)
        import status
        self.status = status
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        d = self.root / "ep"
        d.mkdir()
        (d / "graph.json").write_text("{}")
        self.d = d

    def run_lead(self, run_side):
        s = self.status
        with mock.patch.object(s, "EPICS", self.root), mock.patch.object(s, "session_for", return_value="s"), \
                mock.patch.object(s, "rebind_asks", return_value=0), mock.patch.object(s, "pane_ref", return_value="node-b/t1"), \
                mock.patch.object(s.subprocess, "run", side_effect=run_side) as run, \
                mock.patch.object(_sys, "argv", ["adw-status", "lead", "ep", "--pane", "t1"]):
            s.main()
        return run

    def test_passes_epic_and_ref(self):
        with mock.patch.dict(os.environ, {"AW_LEAD_ENROLL": str(ROOT / "bin/aw-lead-enroll")}):
            run = self.run_lead(lambda *a, **k: None)
        self.assertEqual(run.call_args.args[0], [str(ROOT / "bin/aw-lead-enroll"), "ep", "node-b/t1"])
        self.assertEqual(run.call_args.kwargs["timeout"], 40)
        self.assertIsNone(run.call_args.kwargs["stderr"])
        self.assertTrue((self.d / "lead.json").exists())

    def test_failures_never_break_lead(self):
        for exc in (FileNotFoundError("aw-lead-enroll"), subprocess.TimeoutExpired("aw-lead-enroll", 5), OSError("x")):
            (self.d / "lead.json").unlink(missing_ok=True)

            def boom(*a, **k):
                raise exc

            self.run_lead(boom)
            self.assertTrue((self.d / "lead.json").exists())

    def test_real_timeout_on_hanging_enroller(self):
        bindir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bindir)
        h = bindir / "hang"
        h.write_text("#!/bin/sh\nsleep 30\n")
        h.chmod(0o755)
        s = self.status
        real = subprocess.run
        fast = lambda *a, **k: real(*a, **{**k, "timeout": 1})
        t0 = time.time()
        with mock.patch.dict(os.environ, {"AW_LEAD_ENROLL": str(h)}), \
                mock.patch.object(s, "EPICS", self.root), mock.patch.object(s, "session_for", return_value="s"), \
                mock.patch.object(s, "rebind_asks", return_value=0), mock.patch.object(s, "pane_ref", return_value="h/t1"), \
                mock.patch.object(s.subprocess, "run", side_effect=fast), \
                mock.patch.object(_sys, "argv", ["adw-status", "lead", "ep", "--pane", "t1"]):
            s.main()
        self.assertLess(time.time() - t0, 15)
        self.assertTrue((self.d / "lead.json").exists())


class RespawnTest(Base):
    def load(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("adw_respawn_t", str(ROOT / "bin/adw-respawn"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("adw_respawn_t", loader))
        loader.exec_module(mod)
        return mod

    def test_move_leads_retargets_local_and_remote_and_swallows_missing_binary(self):
        mod = self.load()
        state = self.tmp / "state"
        (state / "epics" / "ep").mkdir(parents=True)
        lead = state / "epics" / "ep" / "lead.json"
        stub = self.bin / "stub-enroll"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{self.argv}"\n')
        stub.chmod(0o755)
        path = {"AW_LEAD_ENROLL": str(stub)}
        with mock.patch.object(mod, "STATE", state), mock.patch.dict(os.environ, path):
            for old, new in (("t1", "t9"), ("hub/t1", "hub/t9")):
                lead.write_text(json.dumps({"pane": old, "session": "s"}))
                self.assertEqual(mod.move_leads(old, new, "s"), 1)
            self.assertEqual(self.calls(), ["--retarget-only ep t9", "--retarget-only ep hub/t9"])
            lead.write_text(json.dumps({"pane": "x/t1", "session": "s"}))
            with mock.patch.dict(os.environ, {"AW_LEAD_ENROLL": str(self.tmp / "missing")}):
                self.assertEqual(mod.move_leads("x/t1", "x/t2", "s"), 1)
        self.assertEqual(json.loads(lead.read_text())["pane"], "x/t2")


class StallRepointTest(Base):
    def setUp(self):
        super().setUp()
        import stall
        self.stall = stall
        self.root = self.tmp / "root"
        (self.root / "epics" / "ep").mkdir(parents=True)
        self.lead = self.root / "epics" / "ep" / "lead.json"
        self.lead.write_text(json.dumps({"pane": "h/t1", "session": "s"}))

    def test_repoint_retargets_aw_and_swallows_missing_binary(self):
        stub = self.bin / "stub-enroll"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{self.argv}"\n')
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, {"AW_LEAD_ENROLL": str(stub)}):
            self.assertTrue(self.stall.repoint_lead(self.root, "ep", "h/t9", "s"))
        self.assertEqual(self.calls(), ["--retarget-only ep h/t9"])
        with mock.patch.dict(os.environ, {"AW_LEAD_ENROLL": str(self.tmp / "missing")}):
            self.assertTrue(self.stall.repoint_lead(self.root, "ep", "h/t10", "s"))
        self.assertEqual(json.loads(self.lead.read_text())["pane"], "h/t10")



if __name__ == "__main__":
    unittest.main()
