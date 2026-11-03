import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys = _sys
sys.path.insert(0, str(ROOT / "board"))

import reqs  # noqa: E402


def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = reqs.main(list(argv))
    return code, out.getvalue()


class ReqsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ADW_STATE_DIR": self.dir, "ADW_REQ_BY": "test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir)

    def test_add_list_order_and_close(self):
        run("add", "low thing", "--who", "Ann", "-p", "p3")
        run("add", "urgent", "--who", "Bo", "--client", "Acme", "-p", "P0")
        run("add", "due soon", "--who", "Cy", "--due", "2026-11-01")
        code, out = run("ls")
        self.assertEqual(code, 0)
        self.assertEqual([l.split()[0] for l in out.splitlines()], ["r-2", "r-3", "r-1"])
        run("done", "r-2", "shipped")
        self.assertIn("r-2", run("ls")[1])
        run("told", "r-2")
        self.assertNotIn("r-2", run("ls")[1])
        self.assertIn("r-2", run("ls", "--all")[1])
        notes = run("show", "r-2")[1]
        self.assertIn("test: shipped", notes)
        self.assertIn("test: told", notes)

    def test_add_requires_who(self):
        self.assertEqual(run("add", "x")[0], 2)

    def test_set_records_changes(self):
        run("add", "x", "--who", "Ann")
        run("set", "r-1", "-p", "P1", "--owner", "node-a/t4")
        r = json.loads(run("ls", "--json")[1])[0]
        self.assertEqual((r["priority"], r["owner"]), ("P1", "node-a/t4"))
        self.assertIn("priority=P1", r["notes"][0]["text"])
        run("set", "r-1", "--due", "2026-12-01")
        run("set", "r-1", "--due", "none", "--owner", "")
        r = json.loads(run("ls", "--json")[1])[0]
        self.assertEqual((r["due"], r["owner"]), (None, None))
        self.assertEqual(run("set", "r-1", "--who", "")[0], 2)

    def test_stale_and_overdue(self):
        run("add", "x", "--who", "Ann", "-p", "P1", "--due", "2020-01-01")
        r = reqs.read()["items"][0]
        self.assertFalse(reqs.stale(r, r["updated"] + 3600))
        self.assertTrue(reqs.stale(r, r["updated"] + 25 * 3600))
        self.assertTrue(reqs.overdue(r))
        r["status"] = "waiting"
        self.assertFalse(reqs.stale(r, r["updated"] + 99 * 3600))

    def test_link_must_open(self):
        run("add", "x", "--who", "A", "--link", "https://chat.example.test/thread/1")
        self.assertEqual(reqs.read()["items"][0]["link"], "https://chat.example.test/thread/1")
        self.assertEqual(run("add", "y", "--who", "A", "--link", "PROJ-1")[0], 2)

    def test_bad_inputs(self):
        self.assertEqual(run("show", "7")[0], 2)
        self.assertEqual(run("add", "x", "--who", "A", "--due", "tomorrow")[0], 2)
        with self.assertRaises(SystemExit):
            run("done", "r-9")

    def test_wrapper_forwards_quoted_args_without_checkout(self):
        home = Path(self.dir) / "home"
        bindir = Path(self.dir) / "bin"
        bindir.mkdir()
        (bindir / "ssh").write_text('#!/bin/sh\nshift\nprintf "%s" "$1" > "$SSH_OUT"\n')
        (bindir / "ssh").chmod(0o755)
        out = Path(self.dir) / "ssh.out"
        env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}", "SSH_OUT": str(out),
               "HC_TERMINAL_ID": "t25", "ADW_REQ_BY": "", "ADW_HOME": str(home), "ADW_HUB": "u@hub"}
        subprocess.run(["sh", str(ROOT / "bin/req"), "add", "it's $(rm -rf x)", "--who", "Ann"], env=env, check=True)
        cmd = out.read_text()
        self.assertIn("reqs.py 'add' 'it'\\''s $(rm -rf x)' '--who' 'Ann'", cmd)
        self.assertIn("ADW_REQ_BY='t25' exec", cmd)
        env["ADW_REQ_BY"] = "x; touch pwned"
        subprocess.run(["sh", str(ROOT / "bin/req"), "ls"], env=env, check=True)
        self.assertIn("ADW_REQ_BY='x; touch pwned' exec", out.read_text())


class BoardRequestsTest(unittest.TestCase):
    def test_digest_and_mate_event(self):
        import board
        r = {"id": "r-4", "priority": "P1", "status": "doing", "who": "Bo", "title": "fix   the export",
             "owner": "node-a/t3", "updated": 1000.0, "stale": True, "overdue": False}
        b = {"epics": [], "agents": [], "decisions": [], "interrupts": [], "requests": [r]}
        with mock.patch.object(board, "needs_you", return_value=[]):
            text = board.digest(b)
            ev = board.mate_events(b)
        self.assertIn("## Requests (1)", text)
        self.assertIn("- `r-4` P1 doing · Bo: fix the export · STALE", text)
        self.assertEqual(ev["request:r-4:stale:1000"],
                         "request r-4 P1 stale (doing, owner node-a/t3) Bo: fix the export")


class RenderRequestsTest(unittest.TestCase):
    def test_terminal_board_and_page_show_requests(self):
        import board
        r = {"id": "r-4", "priority": "P1", "status": "doing", "who": "Bo", "title": "fix the export",
             "owner": None, "updated": 1000.0, "stale": True, "overdue": False}
        b = {"epics": [], "agents": [], "decisions": [], "interrupts": [], "requests": [r]}
        text = board.render(b, False)
        self.assertIn("REQUESTS", text)
        self.assertIn("r-4", text)
        self.assertIn("STALE", text)
        self.assertNotIn("REQUESTS", board.render({**b, "requests": []}, False))
        page = (ROOT / "board/board_page.html").read_text()
        self.assertIn("requestRow", page)
        self.assertIn('<details ${dk("requests", true)}>', page)
        self.assertNotIn('dk("requests")} open', page)


if __name__ == "__main__":
    unittest.main()
