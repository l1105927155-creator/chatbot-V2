"""Real bwrap, loop-volume, network, cgroup, and process-namespace checks."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "dsh/plugins/agent-profile"
sys.path.insert(0, str(PLUGIN))
import group_volume


@pytest.fixture
def group_volume_runtime(tmp_path):
    identity = "group-test-" + uuid.uuid4().hex
    state = group_volume.prepare_group(tmp_path, "platform-test", "bot-test", identity)
    workspace = Path(state["workspace"])
    try:
        yield tmp_path, identity, workspace
    finally:
        if os.path.ismount(workspace):
            group_volume.unmount_group(workspace)


def _run(workspace: Path, command: str, *, env: dict[str, str] | None = None,
         timeout: float = 20) -> subprocess.CompletedProcess[str]:
    unit = "dsh-subprocess-group-test-" + uuid.uuid4().hex
    return subprocess.run(
        ["/usr/bin/systemd-run", "--user", "--scope", "--collect", "--quiet",
         "--unit=" + unit, "--", "/usr/bin/python3",
         str(PLUGIN / "group-scope-runner.py"), "--workspace", str(workspace),
         "--workdir", ".", "--", command],
        text=True, capture_output=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def test_namespace_mount_environment_network_cgroup_and_persistence(group_volume_runtime, tmp_path):
    storage_root, identity, workspace = group_volume_runtime
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("host-only-secret")
    (workspace / "host-link").symlink_to(host_secret)
    (workspace / "persistent.txt").write_text("survives-remount")
    code = (
        "import os,socket; "
        "assert os.getcwd() == '/workspace'; "
        "assert not os.path.exists('/home'); "
        "assert not os.path.exists('/etc/shadow'); "
        f"assert not os.path.exists({str(host_secret)!r}); "
        "assert not os.path.exists('/workspace/host-link'); "
        "assert 'GROUP_SECRET_SENTINEL' not in os.environ; "
        "s=socket.socket(); s.settimeout(1); rc=s.connect_ex(('1.1.1.1',53)); "
        "print('network_rc='+str(rc)); assert rc != 0; "
        "print('mounts='+','.join(sorted(os.listdir('/'))))"
    )
    result = _run(workspace, "/usr/bin/python3 -c " + repr(code),
                  env={"GROUP_SECRET_SENTINEL": "host-secret-value"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "network_rc=" in result.stdout
    assert (workspace / "persistent.txt").read_text() == "survives-remount"
    assert os.statvfs(workspace).f_blocks * os.statvfs(workspace).f_frsize <= 1024 ** 3

    group_volume.unmount_group(workspace)
    # A crash residue for a different, incomplete allocation must block new
    # allocations while leaving this verified existing group usable.
    orphan = workspace.parent.parent / ("f" * 64)
    orphan.mkdir(mode=0o700)
    (orphan / "workspace.ext4").write_bytes(b"incomplete new allocation")
    group_volume.prepare_group(storage_root, "platform-test", "bot-test", identity)
    assert (workspace / "persistent.txt").read_text() == "survives-remount"
    with pytest.raises((OSError, ValueError)):
        group_volume.prepare_group(storage_root, "platform-test", "bot-test", "new-with-residue")


def test_group_pid_namespace_kills_descendants_when_bash_exits(group_volume_runtime):
    _, _, workspace = group_volume_runtime
    command = "(sleep 0.3; printf late > /workspace/late.txt) & exit 0"
    result = _run(workspace, command)
    assert result.returncode == 0, result.stdout + result.stderr
    time.sleep(0.8)
    assert not (workspace / "late.txt").exists(), "normal Bash completion left a descendant alive"


def test_group_workspace_enforces_disk_cap_and_recovers_after_enospc(group_volume_runtime):
    _, _, workspace = group_volume_runtime
    command = "/usr/bin/python3 -c \"import os; f=open('fill.bin','wb'); os.posix_fallocate(f.fileno(),0,2*1024**3)\""
    result = _run(workspace, command)
    assert result.returncode != 0
    assert "No space left on device" in result.stderr, result.stdout + result.stderr
    cleanup = _run(workspace, "rm -f fill.bin; printf recovered > recovered.txt")
    assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
    assert (workspace / "recovered.txt").read_text() == "recovered"
    assert not (workspace / "fill.bin").exists()


def test_missing_mount_fails_closed_before_command(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    result = _run(workspace, "touch /tmp/should-not-run")
    assert result.returncode != 0
    assert "V2 group containment unavailable" in result.stderr
    assert not (tmp_path / "should-not-run").exists()
