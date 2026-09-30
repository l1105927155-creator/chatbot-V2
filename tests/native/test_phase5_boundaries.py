"""Runtime evidence for pinned DSH tool scope and execution gates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tests" / "native" / "phase5-boundaries.mjs"
NODE = shutil.which("node")


def test_pinned_dsh_native_tool_boundaries() -> None:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", HELPER.read_text(encoding="utf-8")],
        cwd=ROOT / ".runtime" / "dsh-runtime",
        env={
            **os.environ,
            "V2_DSH_NATIVE_TEST": "1",
            "V2_DSH_NATIVE_HELPER": str(HELPER),
        },
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["global_restrict"] == {
        "visible": ["agent_local", "global_keep", "mcp__fixture__probe"],
        "global_hidden_error": "Error: unknown tool \"global_hidden\"",
        "agent_local_result": "ran:agent_local",
        "mcp_result": "Error: agent guard denied MCP call",
    }
    assert evidence["pre_execute_and_guard"] == {
        "waterfall_order": ["pass-through", "deny-listener"],
        "denied": 'Error: latest rule denied "guarded"',
        "body_calls": 0,
    }
    assert evidence["concurrent_agents"] == {
        "results": ["left-result", "right-result"],
        "agent_ids": ["left", "right"],
    }
    assert evidence["dynamic_rule"] == {
        "results": ["operation-1", 'Error: latest rule denied "operation"'],
        "body_calls": 1,
    }
