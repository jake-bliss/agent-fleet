import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import gzip
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))

import board  # noqa: E402

OPEN = {"id": "d-000001", "status": "open", "title": "t1", "body": "open body", "options": [{"key": "a", "label": "A"}], "created": 1}
DONE = {"id": "d-000002", "status": "answered", "title": "t2", "body": "x" * 5000, "created": 2, "epic": "e", "pane": "p",
        "options": [{"key": "a", "label": "A", "extra": 1}, {"key": "b", "label": "B"}], "recommend": "a", "cwd": "/c", "session": "s",
        "answer": {"choice": "b", "by": "auto", "at": 3, "note": "n" * 500}, "delivered": True}
GONE = {"id": "d-000003", "status": "withdrawn", "title": "t3", "body": "b", "options": [], "created": 3}


class Slim(unittest.TestCase):
    def test_open_unchanged_resolved_slimmed_withdrawn_dropped(self):
        out = board.slim_board({"x": 1, "decisions": [OPEN, DONE, GONE]})
        self.assertEqual(out["x"], 1)
        o, d = out["decisions"]
        self.assertEqual(o, OPEN)
        self.assertEqual(len(out["decisions"]), 2)
        self.assertNotIn("body", d)
        self.assertEqual(d["options"], [{"key": "b", "label": "B"}])
        self.assertEqual(d["answer"], {"choice": "b", "by": "auto", "at": 3})
        self.assertTrue(d["slim"] and d["delivered"])
        self.assertEqual(DONE["body"], "x" * 5000)


class Serve(unittest.TestCase):
    def test_gzip_and_lazy_decision(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "d-000002.json").write_text(json.dumps(DONE))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        patches = [mock.patch.object(board.asks, "ASKS", tmp), mock.patch.object(board.asks, "apply_defaults", lambda: None),
                   mock.patch.object(board.asks, "retry_deliveries", lambda: None),
                   mock.patch.object(board, "build", lambda a: {"decisions": [OPEN, DONE, GONE]})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        threading.Thread(target=board.serve, args=(Namespace(serve=port),), daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/decision?id=d-000002").read()
                break
            except urllib.error.URLError:
                time.sleep(0.1)
        req = urllib.request.Request(base + "/api/board", headers={"Accept-Encoding": "gzip"})
        r = urllib.request.urlopen(req)
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        gz = gzip.decompress(r.read())
        plain = urllib.request.urlopen(base + "/api/board").read()
        self.assertEqual(gz, plain)
        self.assertEqual([d["id"] for d in json.loads(plain)["decisions"]], ["d-000001", "d-000002"])
        full = json.loads(urllib.request.urlopen(base + "/api/decision?id=d-000002").read())
        self.assertEqual(full["body"], DONE["body"])
        for bad in ("d-ffffff", "nope"):
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(base + "/api/decision?id=" + bad)
            self.assertEqual(c.exception.code, 404)


if __name__ == "__main__":
    unittest.main()


class AcceptsGzipTest(unittest.TestCase):
    def test_negotiation(self):
        self.assertTrue(board.accepts_gzip("gzip, deflate"))
        self.assertTrue(board.accepts_gzip("br;q=1.0, gzip;q=0.5"))
        self.assertTrue(board.accepts_gzip("*"))
        self.assertFalse(board.accepts_gzip("gzip;q=0"))
        self.assertFalse(board.accepts_gzip("gzip;q=0.0, identity"))
        self.assertFalse(board.accepts_gzip(""))
        self.assertFalse(board.accepts_gzip("deflate, x-gzip-not"))
        self.assertFalse(board.accepts_gzip("*, gzip;q=0"))
        self.assertTrue(board.accepts_gzip("*;q=0, gzip"))
        self.assertFalse(board.accepts_gzip("*;q=0"))


class PageScript(unittest.TestCase):
    def test_inline_script_parses(self):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = (Path(__file__).resolve().parent.parent / "board" / "board_page.html").read_text()
        body = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        f = Path(tempfile.mkdtemp()) / "page.js"
        f.write_text(body)
        p = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
