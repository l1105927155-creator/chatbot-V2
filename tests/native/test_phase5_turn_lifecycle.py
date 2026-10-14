"""Exercise turn-source binding on the real pinned DSH AgentLoop."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "dsh-runtime"
HELPER = ROOT / "tests" / "native" / "phase5-turn-lifecycle.mjs"
NODE = shutil.which("node")


def test_native_turn_source_lifecycle_and_stale_inbox_recovery() -> None:
    with tempfile.TemporaryDirectory(prefix="phase5-turn-lifecycle-", dir=ROOT / ".runtime") as directory:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", dir=RUNTIME, delete=False) as helper:
            helper.write(HELPER.read_text(encoding="utf-8"))
            helper_path = Path(helper.name)
        try:
            outputs = []
            for mode in ("seed", "resume"):
                result = subprocess.run(
                    [NODE, str(helper_path), mode],
                    cwd=RUNTIME,
                    env={
                        **os.environ,
                        "V2_PHASE5_LIFECYCLE_ROOT": directory,
                        "V2_PHASE5_AUTHORITY": str(ROOT / "dsh" / "plugins" / "qq-permission" / "authority.mjs"),
                    },
                    text=True,
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                outputs.append(json.loads(result.stdout))
        finally:
            helper_path.unlink(missing_ok=True)
    assert outputs == [
        {
            "phase": "seed",
            "ownerThenMember": ["1105927155", "2200000002", "1105927155"],
            "cancelledSignalObserved": True,
            "persistedStaleInbox": True,
        },
        {
            "phase": "resume",
            "persistedStaleInboxDenied": True,
            "independentTurnRecovered": True,
        },
    ]
