"""Load the real Phase 5 DSH profile without creating a session or prompting."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_phase5_authorized_profile_initializes_without_inference() -> None:
    worker = r'''
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(os.environ['V2_REPO_ROOT'])
sys.path.insert(0, str(root / 'astrbot-plugins' / 'v2_dsh_router'))
from dsh_client import AcpRuntime, DshClient

def files(path):
    if not path.exists():
        return ()
    return tuple(sorted(str(item.relative_to(path)) for item in path.rglob('*') if item.is_file()))

runtime = root / '.runtime' / 'dsh-runtime'
before_sessions = None
client = None
with TemporaryDirectory(prefix='phase5-startup-', dir=root / '.runtime') as directory:
    isolated = Path(directory)
    home = isolated / 'dsh-home'
    workspace = isolated / 'workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    sessions = home / 'sessions'
    before_sessions = files(sessions)
    client = DshClient(
        dsh_bin=runtime / 'node_modules' / '.bin' / 'dsh',
        dsh_home=home,
        workspace=workspace,
        patch=root / 'dsh' / 'profile' / 'v2-qq.patch.yml',
        instructions=root / 'dsh' / 'profile' / 'qq-agent.md',
        provider='phase5-startup-no-provider',
        model='phase5-startup-no-model',
        enable_authorization=True,
    )
    try:
        assert type(client.runtime) is AcpRuntime
        assert client.runtime.client._proc.poll() is None, 'ACP runtime exited after initialize'
        assert client._sessions == set()
        assert client.turn_sources.directory.exists()
        assert client.turn_sources.directory.stat().st_mode & 0o777 == 0o700
        assert files(client.turn_sources.directory) == ()
        assert client.scope_root.exists()
        assert client.scope_root.stat().st_mode & 0o777 == 0o700
        stderr = '\n'.join(client.runtime.client._stderr_lines).lower()
        forbidden = (
            'dependencyerror', 'dependency error', 'err_module_not_found',
            'cannot find package', 'failed to load plugin', 'could not resolve plugin',
        )
        assert not any(marker in stderr for marker in forbidden), stderr
    finally:
        client.close()
        client = None

    assert files(sessions) == before_sessions, 'initialize created a DSH session'
    epoch_directory = home / 'v2-turn-sources'
    assert epoch_directory.exists()
    assert not any(epoch_directory.iterdir()), 'private turn epoch directory was not cleaned'
    assert (home / 'v2-conversation-scopes').stat().st_mode & 0o777 == 0o700
    print(json.dumps({'initialized': True, 'session_files': len(before_sessions), 'source_cleaned': True}))
'''
    env = {
        **os.environ,
        "V2_REPO_ROOT": str(ROOT),
        "V2_DSH_PROVIDER": "phase5-startup-no-provider",
        "V2_DSH_MODEL": "phase5-startup-no-model",
    }
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
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
        raise AssertionError(f"pinned DSH initialization exceeded 60 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"initialized": true' in stdout, stdout
