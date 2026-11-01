from __future__ import annotations

import importlib.util
import fcntl
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/group-capability-backup.py"
SPEC = importlib.util.spec_from_file_location("group_capability_backup", SCRIPT)
backup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(backup)


def test_snapshot_tracks_existing_and_absent_paths_then_restores(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "skills/example").mkdir(parents=True)
    (workspace / "skills/example/SKILL.md").write_text("old skill\n")
    (workspace / "apps/example").mkdir(parents=True)
    (workspace / "apps/example/main.py").write_text("old app\n")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(
        workspace, backup_dir / "operation-1",
        ["skills/example/SKILL.md", "apps/example/main.py", "apps/example/config.json"],
    )

    manifest = json.loads((snapshot / backup.MANIFEST).read_text())
    assert [row["before_exists"] for row in manifest["files"]] == [True, True, False]
    assert "data" not in json.dumps(manifest)

    backup.record_plan(workspace, snapshot, {
        "skills/example/SKILL.md": backup.digest(b"published skill\n"),
        "apps/example/main.py": None,
        "apps/example/config.json": backup.digest(b"generated config\n"),
    })
    (workspace / "skills/example/SKILL.md").write_text("published skill\n")
    (workspace / "apps/example/main.py").unlink()
    (workspace / "apps/example/config.json").write_text("generated config\n")
    restored = backup.restore(workspace, snapshot)

    assert restored == ["skills/example/SKILL.md", "apps/example/main.py", "apps/example/config.json"]
    assert (workspace / "skills/example/SKILL.md").read_text() == "old skill\n"
    assert (workspace / "apps/example/main.py").read_text() == "old app\n"
    assert not (workspace / "apps/example/config.json").exists()
    assert backup.restore(workspace, snapshot) == restored


def test_restore_stops_on_any_post_publish_drift_before_changing_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "first.py").write_text("before one")
    (workspace / "second.py").write_text("before two")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(workspace, backup_dir / "backup", ["first.py", "second.py"])
    backup.record_plan(workspace, snapshot, {
        "first.py": backup.digest(b"published one"),
        "second.py": backup.digest(b"published two"),
    })
    (workspace / "first.py").write_text("published one")
    (workspace / "second.py").write_text("published two")
    (workspace / "second.py").write_text("someone's later edit")

    with pytest.raises(backup.BackupError, match="restore stopped.*second.py"):
        backup.restore(workspace, snapshot)
    assert (workspace / "first.py").read_text() == "published one"
    assert (workspace / "second.py").read_text() == "someone's later edit"


def test_restore_recovers_after_publication_crashes_midway_and_is_repeatable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("old a")
    (workspace / "b.py").write_text("old b")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(workspace, backup_dir / "backup", ["a.py", "b.py"])
    backup.record_plan(workspace, snapshot, {
        "a.py": backup.digest(b"new a"),
        "b.py": backup.digest(b"new b"),
    })

    # Simulate a process crash after only the first replacement reached disk.
    (workspace / "a.py").write_text("new a")
    assert (workspace / "b.py").read_text() == "old b"
    assert backup.restore(workspace, snapshot) == ["a.py", "b.py"]
    assert (workspace / "a.py").read_text() == "old a"
    assert (workspace / "b.py").read_text() == "old b"
    assert backup.restore(workspace, snapshot) == ["a.py", "b.py"]


def test_restore_rechecks_each_file_after_preflight_and_preserves_late_edit(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("old a")
    (workspace / "b.py").write_text("old b")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(workspace, backup_dir / "backup", ["a.py", "b.py"])
    backup.record_plan(workspace, snapshot, {
        "a.py": backup.digest(b"new a"),
        "b.py": backup.digest(b"new b"),
    })
    (workspace / "a.py").write_text("new a")
    (workspace / "b.py").write_text("new b")

    read_current = backup._read_current
    calls = {"a.py": 0, "b.py": 0}

    def mutate_after_preflight(root, rel):
        calls[rel] += 1
        if rel == "b.py" and calls[rel] == 2:
            (workspace / "b.py").write_text("late user edit")
        return read_current(root, rel)

    monkeypatch.setattr(backup, "_read_current", mutate_after_preflight)
    with pytest.raises(backup.BackupError, match="changed after restore preflight.*b.py"):
        backup.restore(workspace, snapshot)
    assert (workspace / "a.py").read_text() == "old a"
    assert (workspace / "b.py").read_text() == "late user edit"


def test_verified_mount_is_checked_under_native_lock_and_fd_stays_pinned(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock_path = Path(str(workspace) + ".volume.lock")
    observed = {}

    class FakeGroupVolume:
        @staticmethod
        def paths(runtime_root, platform_id, bot_id, group_id):
            return {"workspace": workspace}

        @staticmethod
        def verified_group_workspace(root):
            probe = os.open(lock_path, os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            observed["fd"] = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            return observed["fd"]

    monkeypatch.setattr(backup, "_group_volume_module", lambda: FakeGroupVolume)
    with backup._verified_group_operation(tmp_path, "platform", "bot", "group") as selected:
        assert selected == workspace
        assert os.fstat(observed["fd"]).st_ino == workspace.stat().st_ino
    with pytest.raises(OSError):
        os.fstat(observed["fd"])


@pytest.mark.parametrize("path", ["../outside", "/absolute", ".", "./apps/x/main.py",
                                  "apps/x/data/db.sqlite", "foo\\bar"])
def test_snapshot_rejects_escape_and_data_paths(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    with pytest.raises(backup.BackupError):
        backup.create_snapshot(workspace, backup_dir / "backup", [path])
    assert not (backup_dir / "backup").exists()


def test_snapshot_rejects_symlinked_file_and_parent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside"
    target.write_text("secret")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    (workspace / "linked-file").symlink_to(target)
    with pytest.raises(backup.BackupError, match="regular, unlinked"):
        backup.create_snapshot(workspace, backup_dir / "one", ["linked-file"])

    (workspace / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="real directory"):
        backup.create_snapshot(workspace, backup_dir / "two", ["linked-dir/outside"])
    assert target.read_text() == "secret"


def test_restore_refuses_unrecorded_snapshot_or_workspace_mismatch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "x.py").write_text("old")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(workspace, backup_dir / "backup", ["x.py"])
    with pytest.raises(backup.BackupError, match="no recorded post-change"):
        backup.restore(workspace, snapshot)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(backup.BackupError, match="does not match workspace"):
        backup.restore(other, snapshot)


def test_restore_keeps_business_data_added_after_publication(tmp_path):
    workspace = tmp_path / "workspace"
    app = workspace / "apps/example"
    (app / "data").mkdir(parents=True)
    code = app / "app.py"
    code.write_text("old code")
    state = app / "data/state.json"
    state.write_text('{"count": 1}')
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(workspace, backups / "operation", ["apps/example/app.py"])
    backup.record_plan(workspace, snapshot, {"apps/example/app.py": backup.digest(b"new code")})
    code.write_text("new code")
    state.write_text('{"count": 2}')
    (app / "data/new-event.json").write_text('{"member": "synthetic"}')

    backup.restore(workspace, snapshot)

    assert code.read_text() == "old code"
    assert state.read_text() == '{"count": 2}'
    assert (app / "data/new-event.json").read_text() == '{"member": "synthetic"}'


def test_corrupt_snapshot_stops_before_restoring_any_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("old a")
    (workspace / "b.py").write_text("old b")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    snapshot = backup.create_snapshot(workspace, backups / "operation", ["a.py", "b.py"])
    backup.record_plan(workspace, snapshot, {
        "a.py": backup.digest(b"new a"), "b.py": backup.digest(b"new b"),
    })
    (workspace / "a.py").write_text("new a")
    (workspace / "b.py").write_text("new b")
    manifest = json.loads((snapshot / backup.MANIFEST).read_text())
    (snapshot / manifest["files"][1]["blob"]).write_bytes(b"corrupted")

    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.restore(workspace, snapshot)

    assert (workspace / "a.py").read_text() == "new a"
    assert (workspace / "b.py").read_text() == "new b"
