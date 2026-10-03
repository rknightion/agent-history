"""The catalogue services import loops.py; it must not pull in the Mac collector."""

import subprocess
import sys


def test_loops_does_not_import_the_collector():
    # The server image has no tzdata, and collect_git builds a ZoneInfo at import time.
    code = "import sys, agent_history.loops; sys.exit('agent_history.collect_git' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
