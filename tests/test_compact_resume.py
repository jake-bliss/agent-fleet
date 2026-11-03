import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import _isolate  # noqa: F401,E402
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("compact_resume", str(ROOT / "hooks/compact-resume.py"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cr = importlib.util.module_from_spec(spec)
loader.exec_module(cr)

NOW = 1_800_000_000


def ask(i, status="open", session="s1", **kw):
    return {"id": i, "status": status, "session": session, "title": f"t {i}", **kw}


class LocalAsks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "asks").mkdir()
        p = mock.patch.object(cr, "ADW", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def local(self, d):
        (self.tmp / "asks" / f"{d['id']}.json").write_text(json.dumps(d))

    def test_lists_open_asks_for_the_session(self):
        self.local(ask("d-000001"))
        self.local(ask("d-000002", session="other"))
        (open_, recent) = cr.asks("s1")
        self.assertEqual([x.split()[0] for x in open_], ["d-000001"])
        self.assertEqual(recent, [])

    def test_recent_answers_are_listed(self):
        self.local(ask("d-000004", status="answered", answer={"choice": "a", "at": __import__("time").time()}))
        (open_, recent) = cr.asks("s1")
        self.assertEqual(open_, [])
        self.assertEqual(len(recent), 1)

    def test_never_shells_out(self):
        self.local(ask("d-000001"))
        with mock.patch.object(cr.subprocess, "run", side_effect=AssertionError("no subprocess")):
            cr.asks("s1")


if __name__ == "__main__":
    unittest.main()
