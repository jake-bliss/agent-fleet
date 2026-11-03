"""Imported first by every test that loads board/*.py: no test may reach a real herdr app core, a real ADW_HOME or ssh."""
import os
import stat
import tempfile
from pathlib import Path

for k in [k for k in os.environ if k.startswith(("HC_", "HERDR_"))] + ["ADW_HC_MACHINE", "ADW_MACHINE", "ADW_MACHINES", "ADW_MUX_TEST"]:
    os.environ.pop(k, None)

_dir = Path(tempfile.mkdtemp(prefix="adw-test-"))
FAKE_HC = _dir / "fake-hc"
HC_LOG = _dir / "calls.log"
FAKE_HC.write_text(f'#!/bin/sh\necho "$@" >> {HC_LOG}\nexit 97\n')
FAKE_HC.chmod(FAKE_HC.stat().st_mode | stat.S_IXUSR)
os.environ["HC_BIN"] = str(FAKE_HC)
os.environ["ADW_MUX_TEST"] = "1"
os.environ["ADW_HOME"] = str(_dir / "home")
os.environ["ADW_LOCK_DIR"] = str(_dir / "locks")
os.environ["ADW_STATE_DIR"] = str(_dir / "state")
os.environ["ADW_SESSIONS_DIR"] = str(_dir / "sessions")
