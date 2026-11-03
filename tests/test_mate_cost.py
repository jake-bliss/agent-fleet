import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def turn(ts, model, cache_read, msg_id):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": msg_id, "model": model,
                        "usage": {"input_tokens": 0, "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": 0, "output_tokens": 0}}}


class MateCostTest(unittest.TestCase):
    def test_child_only_day_is_reported(self):
        home = Path(tempfile.mkdtemp())
        proj = home / ".claude/projects/-home-u-projects"
        (proj / "s1/subagents").mkdir(parents=True)
        start = {"type": "user", "message": {"content": "<command-name>/first-mate</command-name>"}}
        with open(proj / "s1.jsonl", "w") as f:
            for o in (start, turn("2026-11-01T10:00:00Z", "claude-opus-5-5", 1_000_000, "a")):
                f.write(json.dumps(o) + "\n")
        with open(proj / "s1/subagents/agent-x.jsonl", "w") as f:
            f.write(json.dumps(turn("2026-11-02T10:00:00Z", "claude-sonnet-5-5", 1_000_000, "b")) + "\n")
        out = subprocess.run([sys.executable, str(ROOT / "bin/mate-cost")], capture_output=True,
                             text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"}).stdout
        rows = {l.split()[0]: l.split() for l in out.splitlines() if l.startswith("2026-")}
        self.assertEqual(rows["2026-11-01"][4:], ["0.5", "0.0", "0.5"])
        self.assertEqual(rows["2026-11-02"][1], "0")
        self.assertEqual(rows["2026-11-02"][4:], ["0.0", "0.3", "0.3"])


if __name__ == "__main__":
    unittest.main()
