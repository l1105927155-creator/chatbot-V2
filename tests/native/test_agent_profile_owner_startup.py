"""Pinned DSH owner Profile startup with private host roots and no model request."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_owner_profile_initializes_without_inference():
    worker = r'''
import json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from dsh_client import DshClient
with TemporaryDirectory(prefix="owner-profile-startup-", dir=root / ".runtime") as tmp:
    home, workspace = Path(tmp) / "dsh-home", Path(tmp) / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    os.chmod(home, 0o700)
    os.chmod(workspace, 0o700)
    os.environ["V2_QQ_OWNERS"] = json.dumps([{
        "platform_id": "qq", "bot_id": "bot", "user_id": "owner"
    }])
    client = DshClient(
        dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
        dsh_home=home, workspace=workspace,
        patch=root / "dsh/profile/v2-agent-owner.patch.yml",
        instructions=root / "dsh/profile/agent-owner.md",
        provider="owner-startup-no-provider", model="owner-startup-no-model",
        enable_authorization=True,
    )
    try:
        assert client.runtime.client._proc.poll() is None
        assert client._sessions == set()
        assert (home / "agent-bot-personas").is_dir()
        assert (home / "agent-bot-scenes").is_dir()
        assert (home / "v2-turn-sources").is_dir()
        stderr = "\n".join(client.runtime.client._stderr_lines).lower()
        forbidden = ("dependencyerror", "dependency error", "err_module_not_found",
                     "cannot find package", "failed to load plugin", "could not resolve plugin")
        assert not any(marker in stderr for marker in forbidden), stderr
    finally:
        client.close()
    assert not tuple((home / "sessions").glob("*.jsonl")) if (home / "sessions").exists() else True
    print(json.dumps({"initialized": True, "model_calls": 0}))
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
        raise AssertionError(f"owner Profile startup exceeded 60 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"initialized": true' in stdout
