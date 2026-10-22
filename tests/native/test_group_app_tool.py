"""Native DSH group_app tool's bounded timeout and cancellation contract."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def test_group_app_uses_native_shell_deadline_and_abort_signal():
    result = subprocess.run(
        [NODE, str(ROOT / "tests/native/group-app-tool-probe.mjs")],
        text=True, capture_output=True, timeout=10,
        env={**os.environ, "V2_REPO_ROOT": str(ROOT)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "timeout/cancellation contract passed" in result.stdout
