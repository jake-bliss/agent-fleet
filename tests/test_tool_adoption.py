import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "tool-adoption.py"


def run(command, mode=None, **extra):
    payload = {"tool_name": "Bash", "tool_input": dict({"command": command}, **extra)}
    if mode:
        payload["permission_mode"] = mode
    res = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(payload),
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout)["hookSpecificOutput"] if res.stdout.strip() else None


class ToolAdoptionTest(unittest.TestCase):
    def test_pure_sleep_is_backgrounded_allow_only_in_bypass(self):
        out = run("sleep 30", mode="bypassPermissions")
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"], {"command": "sleep 30", "run_in_background": True})
        self.assertIn("run_in_background", out["additionalContext"])
        for mode in ("default", None):
            self.assertEqual(run("sleep 30", mode=mode)["permissionDecision"], "ask")

    def test_probe_loops_are_backgrounded(self):
        for cmd in ("until [ -f /tmp/x ]; do sleep 5; done",
                    "while ! test -e /tmp/x && ! grep -q ok /tmp/y; do sleep 30; done",
                    "until curl -sf localhost:3000/up; do sleep 10; done"):
            out = run(cmd, mode="bypassPermissions")
            self.assertEqual(out["permissionDecision"], "allow", cmd)
            self.assertEqual(out["updatedInput"]["command"], cmd)
            self.assertTrue(out["updatedInput"]["run_in_background"])

    def test_chained_or_heredoc_waits_are_denied(self):
        for cmd in ("sleep 30 && git push --force",
                    "sleep 30; rm -rf x",
                    "until [ -f /tmp/x ]; do sleep 5; done; git push",
                    "while true; do git push; sleep 30; done",
                    "cat <<EOF\nwhile x; do sleep 30; done\nEOF"):
            out = run(cmd, mode="bypassPermissions")
            self.assertEqual(out["permissionDecision"], "deny", cmd)
            self.assertNotIn("updatedInput", out)
            self.assertIn("pr-state", out["permissionDecisionReason"])

    def test_checks_flag_tail_has_no_backtracking(self):
        import time
        cmd = "gh pr checks 1" + " --watch" * 30 + " --bad"
        start = time.monotonic()
        out = run(cmd)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(out["permissionDecision"], "deny")

    def test_short_sleep_and_background_untouched(self):
        self.assertIsNone(run("sleep 5"))
        self.assertIsNone(run("sleep 60", run_in_background=True))

    def test_gh_pr_checks_rewritten(self):
        out = run("gh pr checks 123")
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"]["command"], "pr-state 123")

    def test_gh_pr_checks_watch_waits_in_background(self):
        out = run("gh pr checks 123 --watch")
        self.assertEqual(out["updatedInput"]["command"], "pr-state 123 --wait ci-green")
        self.assertTrue(out["updatedInput"]["run_in_background"])

    def test_gh_pr_checks_repo_is_translated(self):
        out = run("gh pr checks 123 --repo owner/B --watch")
        self.assertEqual(out["updatedInput"]["command"], "pr-state 123 -R owner/B --wait ci-green")
        self.assertEqual(run("gh pr checks 5 -R o/r")["updatedInput"]["command"], "pr-state 5 -R o/r")

    def test_gh_pr_checks_json_is_denied_not_rewritten(self):
        for cmd in ("gh pr checks 123 --json name,state", "gh pr checks 123 --repo o/r --jq .[]"):
            out = run(cmd)
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertNotIn("updatedInput", out)
            self.assertIn("pr-state 123", out["permissionDecisionReason"])

    def test_raw_merge_is_denied_toward_pr_ship_and_raw_ok_bypasses(self):
        out = run("gh pr merge 77 --squash")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("pr-ship 77", out["permissionDecisionReason"])
        self.assertIsNone(run("gh pr merge 77 # raw-ok"))

    def test_loop_with_trailing_or_mutating_command_is_denied(self):
        for cmd in ("until curl -sf localhost:3000/up; do sleep 10; done; curl -X POST localhost:3000/deploy",
                    "until curl -sf localhost:3000/up; do sleep 10; done && curl localhost:3000/x",
                    "until curl -sf -X POST localhost:3000/up; do sleep 10; done",
                    "until curl -sf -d x localhost:3000/up; do sleep 10; done",
                    "until curl -sf -o /tmp/f localhost:3000/up; do sleep 10; done",
                    "until curl -sfO localhost:3000/up; do sleep 10; done",
                    "until curl -sf --data-binary @f localhost:3000/up; do sleep 10; done"):
            out = run(cmd, mode="bypassPermissions")
            self.assertEqual(out["permissionDecision"], "deny", cmd)
            self.assertNotIn("updatedInput", out)

    def test_plain_curl_loop_still_backgrounded(self):
        for cmd in ("until curl -sf localhost:3000/up; do sleep 5; done",
                    "until curl -sf -X GET -o /dev/null localhost:3000/up; do sleep 5; done >/dev/null 2>&1"):
            out = run(cmd, mode="bypassPermissions")
            self.assertEqual(out["permissionDecision"], "allow", cmd)
            self.assertTrue(out["updatedInput"]["run_in_background"])

    def test_raw_ok_bypasses(self):
        self.assertIsNone(run("gh pr checks 123 --json name # raw-ok because"))
        self.assertIsNone(run("sleep 60 # raw-ok"))


if __name__ == "__main__":
    unittest.main()
