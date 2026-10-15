from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent-backup.py"
SPEC = importlib.util.spec_from_file_location("agent_backup", SCRIPT)
agent_backup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(agent_backup)


def fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "upstream.lock.json").write_text('{"version":1}\n')
    for component in agent_backup.REQUIRED:
        path = root / component
        if component.endswith(".json") or component.endswith(".ext4"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixed owner volume bytes" if component.endswith(".ext4") else b'{"bytes":33554432}')
        else:
            path.mkdir(parents=True)
            (path / "sentinel.txt").write_text("sentinel:" + component)
    for rel, payload in (
        ("sessions/native-session.json", '{"sessionId":"native-1"}'),
        ("agent-bot-personas/persona.json", '{"persona":"owner"}'),
        ("agent-bot-scenes/scene.json", '{"scene":"research"}'),
    ):
        path = root / ".runtime/dsh-home-owner" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    return root


@pytest.fixture(autouse=True)
def no_runtime_side_effects(monkeypatch):
    monkeypatch.setattr(agent_backup, "_owner_volume", lambda root: None)


def make_archive(root: Path, output: Path) -> Path:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    agent_backup.create_backup(root, output)
    return output


def test_backup_quarantine_restore_preserves_native_state(tmp_path):
    root = fixture_root(tmp_path)
    archive = make_archive(root, tmp_path / "private" / "runtime.tar.gz")
    # Quarantine the complete allowed component roots, as the real restore procedure requires.
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    for component in agent_backup.COMPONENTS:
        path = root / component
        if path.exists():
            target = quarantine / Path(component).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
    agent_backup.restore_backup(root, archive)
    assert (root / ".runtime/dsh-home-owner/sessions/native-session.json").read_text() == '{"sessionId":"native-1"}'
    assert (root / ".runtime/dsh-home-owner/agent-bot-personas/persona.json").exists()
    assert (root / ".runtime/dsh-home-owner/agent-bot-scenes/scene.json").exists()
    assert (root / ".runtime/owner-storage/workspace.ext4").read_bytes() == b"fixed owner volume bytes"


def test_restore_rejects_hash_corruption_before_mutation(tmp_path):
    root = fixture_root(tmp_path)
    archive = make_archive(root, tmp_path / "private" / "backup.tar.gz")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        extracted = {m.name: source.extractfile(m).read() if m.isfile() else None for m in members}
    victim = ".runtime/dsh-home-owner/sessions/native-session.json"
    extracted[victim] = b"X" * len(extracted[victim])
    broken = tmp_path / "private" / "broken.tar.gz"
    with tarfile.open(broken, "w:gz") as tar:
        for member in members:
            data = extracted[member.name]
            if member.isfile():
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            else:
                tar.addfile(member)
    with pytest.raises(agent_backup.BackupError, match="checksum mismatch|content hash mismatch"):
        agent_backup.restore_backup(root, broken)
    assert (root / ".runtime/dsh-home-owner/sessions/native-session.json").read_text() == '{"sessionId":"native-1"}'


def test_restore_rejects_traversal_before_mutation(tmp_path):
    root = fixture_root(tmp_path)
    archive = make_archive(root, tmp_path / "private" / "backup.tar.gz")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        contents = {m.name: source.extractfile(m).read() if m.isfile() else None for m in members}
    evil = tmp_path / "private" / "traversal.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        for member in members:
            if member.name == ".runtime/dsh-home-owner/sessions/native-session.json":
                data = contents.pop(member.name)
                contents["../escape"] = data
                member.name = "../escape"
            data = contents.get(member.name)
            tar.addfile(member, io.BytesIO(data) if member.isfile() else None)
    with pytest.raises(agent_backup.BackupError, match="unsafe|outside"):
        agent_backup.restore_backup(root, evil)
    assert not (tmp_path / "escape").exists()
    assert (root / ".runtime/dsh-home-owner/sessions/native-session.json").exists()


def test_backup_refuses_active_singleton_lock(tmp_path):
    root = fixture_root(tmp_path)
    lock_path = root / agent_backup.LOCK_REL
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(mode=0o600)
    import fcntl
    with lock_path.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(agent_backup.BackupError, match="singleton lock"):
            agent_backup.create_backup(root, tmp_path / "private" / "backup.tar.gz")


def test_backup_refuses_legacy_dsh_home_writer(tmp_path):
    root = fixture_root(tmp_path)
    env = os.environ.copy()
    env["DSH_HOME"] = str(root / ".runtime/dsh-home-owner")
    process = subprocess.Popen(["/bin/sleep", "10"], env=env, cwd="/")
    try:
        import time
        time.sleep(0.03)
        proc_env = (Path("/proc") / str(process.pid) / "environ").read_bytes().split(b"\0")
        assert any(item == b"DSH_HOME=" + os.fsencode(str(root / ".runtime/dsh-home-owner")) for item in proc_env)
        assert (Path("/proc") / str(process.pid)).stat().st_uid == os.getuid()
        expected_home = os.fsencode(str((root / ".runtime/dsh-home-owner").resolve()))
        assert any(item.startswith(b"DSH_HOME=") and item.partition(b"=")[2] == expected_home for item in proc_env)
        with pytest.raises(agent_backup.BackupError, match="live runtime writer"):
            agent_backup._proc_writer_check(root)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_writer_probe_allows_same_cwd_with_unrelated_astrbot_root(tmp_path):
    root = fixture_root(tmp_path)
    cwd = root / ".runtime/astrbot"
    isolated_root = root / ".runtime/sandbox-ui-check/astrbot"
    isolated_root.mkdir(parents=True)
    env = os.environ.copy()
    env["ASTRBOT_ROOT"] = str(isolated_root)
    env["SANDBOX_DSH_MODE"] = "disabled"
    process = subprocess.Popen(["/bin/sleep", "10"], env=env, cwd=cwd)
    try:
        import time
        time.sleep(0.03)
        agent_backup._proc_writer_check(root)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_writer_probe_blocks_matching_astrbot_root(tmp_path):
    root = fixture_root(tmp_path)
    env = os.environ.copy()
    env["ASTRBOT_ROOT"] = str(root / ".runtime/astrbot")
    process = subprocess.Popen(["/bin/sleep", "10"], env=env, cwd="/")
    try:
        import time
        time.sleep(0.03)
        with pytest.raises(agent_backup.BackupError, match="AstrBot data root"):
            agent_backup._proc_writer_check(root)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_known_unmounted_owner_volume_is_prepared_and_unmounted_for_each_backup(tmp_path):
    workspace = tmp_path / ".runtime/dsh-workspace-owner"
    workspace.mkdir(parents=True, mode=0o700)
    image = tmp_path / ".runtime/owner-storage/workspace.ext4"
    image.parent.mkdir(mode=0o700)
    image.write_bytes(b"known image fixture")
    image.chmod(0o600)
    manifest_path = Path(str(workspace) + ".volume.json")
    data = {"version": 1, "workspace": str(workspace), "image": str(image), "bytes": image.stat().st_size}
    manifest_path.write_text(json.dumps(data))
    manifest_path.chmod(0o600)
    state = {"mounted": False, "prepares": 0, "unmounts": 0}

    def private_file(path):
        fd = os.open(path, os.O_RDONLY)
        return fd, os.fstat(fd)

    def verified(path):
        assert state["mounted"]
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY)

    def prepare(path, backing, size):
        # The real owner_volume.prepare validates a known manifest and image;
        # this focused fixture models only its mount/unmount state transition.
        assert manifest_path.exists() and backing.exists() and size == image.stat().st_size
        state["prepares"] += 1
        state["mounted"] = True

    def unmount(path):
        assert state["mounted"]
        state["unmounts"] += 1
        state["mounted"] = False

    volume = SimpleNamespace(
        manifest_path=lambda path: Path(str(path) + ".volume.json"),
        _manifest=lambda path: json.loads(manifest_path.read_text()),
        _private_file=private_file,
        verified_workspace=verified,
        prepare=prepare,
        unmount=unmount,
    )
    agent_backup._verify_and_unmount_owner_volume(volume, workspace, image)
    agent_backup._verify_and_unmount_owner_volume(volume, workspace, image)
    assert state == {"mounted": False, "prepares": 2, "unmounts": 2}


def test_restore_refuses_existing_components_without_overwrite(tmp_path):
    root = fixture_root(tmp_path)
    archive = make_archive(root, tmp_path / "private" / "backup.tar.gz")
    with pytest.raises(agent_backup.BackupError, match="quarantine it first"):
        agent_backup.restore_backup(root, archive)
    assert (root / ".runtime/dsh-home-owner/sessions/native-session.json").exists()


def test_restore_rejects_symlinked_ancestor_before_mutation(tmp_path):
    root = fixture_root(tmp_path)
    archive = make_archive(root, tmp_path / "private" / "backup.tar.gz")
    for component in agent_backup.COMPONENTS:
        path = root / component
        if path.exists() or path.is_symlink():
            path.unlink() if path.is_file() or path.is_symlink() else shutil.rmtree(path)
    plugin_data = root / ".runtime/astrbot/data/plugin_data"
    shutil.rmtree(plugin_data)
    redirect = root / ".runtime/redirect"
    redirect.mkdir()
    plugin_data.symlink_to(redirect, target_is_directory=True)
    with pytest.raises(agent_backup.BackupError, match="real directory ancestors"):
        agent_backup.restore_backup(root, archive)
    assert not (root / ".runtime/dsh-home-owner").exists()
    assert list(redirect.iterdir()) == []


def test_restore_rejects_changed_upstream_lock_before_mutation(tmp_path):
    root = fixture_root(tmp_path)
    archive = make_archive(root, tmp_path / "private" / "backup.tar.gz")
    for component in agent_backup.COMPONENTS:
        path = root / component
        if path.exists() or path.is_symlink():
            path.unlink() if path.is_file() or path.is_symlink() else shutil.rmtree(path)
    (root / "upstream.lock.json").write_text('{"version":2}\n')
    with pytest.raises(agent_backup.BackupError, match="upstream.lock.json differs"):
        agent_backup.restore_backup(root, archive)
    assert not (root / ".runtime/dsh-home-owner").exists()
