import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "test_review_tier.sh"


class ReviewTierShellSuite(unittest.TestCase):
    def test_shell_suite_passes(self):
        res = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=300)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
