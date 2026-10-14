"""Start the pinned group Agent Profile without a model call or QQ connection."""
from __future__ import annotations
import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"

def test_group_agent_profile_initializes_without_inference():
    worker = r'''
import json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from dsh_client import DshClient
def files(path):
    return tuple(sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())) if path.exists() else ()
with TemporaryDirectory(prefix="agent-profile-startup-", dir=root / ".runtime") as tmp:
    home = Path(tmp) / "dsh-home"
    workspace = Path(tmp) / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    sessions = home / "sessions"
    before = files(sessions)
    client = DshClient(
        dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
        dsh_home=home, workspace=workspace,
        patch=root / "dsh/profile/v2-agent-group.patch.yml",
        instructions=root / "dsh/profile/agent-group.md",
        provider="group-startup-no-provider", model="group-startup-no-model",
        enable_authorization=False,
    )
    try:
        assert client.runtime.client._proc.poll() is None
        assert client._sessions == set()
        persona_root = home / "agent-bot-personas"
        assert persona_root.is_dir()
        assert persona_root.stat().st_mode & 0o777 == 0o700
        assert files(persona_root) == ()
        scene_root = home / "agent-bot-scenes"
        assert scene_root.is_dir()
        assert scene_root.stat().st_mode & 0o777 == 0o700
        assert files(scene_root) == ()
        stderr = "\n".join(client.runtime.client._stderr_lines).lower()
        forbidden = ("dependencyerror", "dependency error", "err_module_not_found",
                     "cannot find package", "failed to load plugin", "could not resolve plugin")
        assert not any(marker in stderr for marker in forbidden), stderr
    finally:
        client.close()
    assert files(sessions) == before, "initialize created a session"
    print(json.dumps({"initialized": True, "model_calls": 0, "session_files": len(before)}))
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
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"group Profile startup exceeded 60 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"initialized": true' in stdout
