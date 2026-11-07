"""The server runs deploy/autoupdate.sh directly (systemd ExecStart), so it must stay executable
in git. A commit once dropped the bit, and every auto-deploy after it failed (status 203/EXEC)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def test_autoupdate_script_stays_executable_in_git():
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files", "-s", "deploy/autoupdate.sh"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    assert out.startswith("100755"), out
