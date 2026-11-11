import json
import os
import subprocess
import tempfile
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "prune-worktrees")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TestPruneWorktrees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.origin = os.path.join(t, "origin.git")
        self.repo = os.path.join(t, "repo")
        git(t, "init", "-q", "--bare", "-b", "main", self.origin)
        git(t, "clone", "-q", self.origin, self.repo)
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(self.repo, "config", k, v)
        self.commit(self.repo, "base")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.prs = []
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.tmp.cleanup()

    def commit(self, cwd, name):
        with open(os.path.join(cwd, name), "w") as f:
            f.write(name)
        git(cwd, "add", name)
        git(cwd, "commit", "-q", "-m", name)
        return git(cwd, "rev-parse", "HEAD")

    def worktree(self, branch, state, extra_local_commit=False):
        path = os.path.join(self.tmp.name, f"repo.{branch}")
        git(self.repo, "worktree", "add", "-q", "-b", branch, path)
        oid = self.commit(path, f"{branch}.txt")
        git(path, "push", "-q", "origin", f"HEAD:refs/pull/{len(self.prs) + 1}/head")
        self.prs.append({"headRefName": branch, "state": state, "headRefOid": oid, "number": len(self.prs) + 1})
        if extra_local_commit:
            self.commit(path, f"{branch}-unpushed.txt")
        return path

    def run_prune(self):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(self.tmp.name, "prs.json"), "w") as f:
            json.dump(self.prs, f)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import json, sys
rows = json.load(open({os.path.join(self.tmp.name, "prs.json")!r}))
state = sys.argv[sys.argv.index("--state") + 1].upper()
print(json.dumps([r for r in rows if state == "ALL" or r["state"] == state]))
""")
        os.chmod(gh, 0o755)
        env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"]}
        out = subprocess.run([SCRIPT, self.repo, "--apply"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_removes_only_finished_clean_idle_contained_worktrees(self):
        merged = self.worktree("merged", "MERGED")
        closed = self.worktree("closed", "CLOSED")
        open_pr = self.worktree("open", "OPEN")
        unpushed = self.worktree("unpushed", "CLOSED", extra_local_commit=True)
        dirty = self.worktree("dirty", "MERGED")
        with open(os.path.join(dirty, "scratch.txt"), "w") as f:
            f.write("work")
        busy = self.worktree("busy", "MERGED")
        self.procs.append(subprocess.Popen(["sleep", "60"], cwd=busy))
        time.sleep(0.2)
        no_pr = os.path.join(self.tmp.name, "repo.nopr")
        git(self.repo, "worktree", "add", "-q", "-b", "nopr", no_pr)

        out = self.run_prune()

        self.assertFalse(os.path.exists(merged), out)
        self.assertFalse(os.path.exists(closed), out)
        for kept in (open_pr, unpushed, dirty, busy, no_pr, self.repo):
            self.assertTrue(os.path.exists(kept), (kept, out))
        self.assertIn("removed=2 dirty-kept=1 in-use-kept=1 local-commits-kept=1", out)

    def test_deleted_worktree_dir_does_not_abort_the_run(self):
        gone = self.worktree("gone", "MERGED")
        ok = self.worktree("ok", "MERGED")
        subprocess.run(["rm", "-rf", gone], check=True)

        out = self.run_prune()

        self.assertFalse(os.path.exists(ok), out)
        self.assertNotIn("repo.gone", git(self.repo, "worktree", "list"))


if __name__ == "__main__":
    unittest.main()
