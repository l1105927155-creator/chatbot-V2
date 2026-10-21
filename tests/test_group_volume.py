from __future__ import annotations

import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "dsh/plugins/agent-profile"
sys.path.insert(0, str(PLUGIN))
import group_volume


def test_group_volume_identity_is_stable_and_scoped_by_all_three_dimensions(tmp_path):
    first = group_volume.paths(tmp_path / ".runtime", "platform", "bot-a", "group-1")
    same = group_volume.paths(tmp_path / ".runtime", "platform", "bot-a", "group-1")
    other_bot = group_volume.paths(tmp_path / ".runtime", "platform", "bot-b", "group-1")
    other_group = group_volume.paths(tmp_path / ".runtime", "platform", "bot-a", "group-2")
    other_platform = group_volume.paths(tmp_path / ".runtime", "platform-2", "bot-a", "group-1")

    assert first == same
    assert first["workspace"] == tmp_path / ".runtime/group-storage" / first["key"] / "workspace"
    assert first["image"] == tmp_path / ".runtime/group-storage" / first["key"] / "workspace.ext4"
    assert len({other_bot["key"], other_group["key"], other_platform["key"], first["key"]}) == 4


def test_group_volume_rejects_control_characters():
    for value in ("", " group", "group\n1", "g\x00"):
        try:
            group_volume.group_key("platform", "bot", value)
        except ValueError:
            continue
        raise AssertionError(f"unsafe group identifier accepted: {value!r}")


def test_group_volume_capacity_is_one_gib():
    assert group_volume.GROUP_BYTES == 1024 ** 3


def _mock_volume_backend(monkeypatch, rows):
    monkeypatch.setattr(group_volume, "enumerate_groups",
                        lambda root, require_mounted=True: list(rows))

    def prepare(workspace, image, capacity):
        if not any(row["key"] == image.parent.name for row in rows):
            rows.append({"key": image.parent.name, "directory": str(image.parent),
                         "workspace": str(workspace), "image": str(image), "bytes": capacity})
        return {"workspace": str(workspace), "image": str(image), "bytes": capacity}

    monkeypatch.setattr(group_volume.owner_volume, "prepare", prepare)


def test_cumulative_budget_counts_retained_volumes_and_rejects_third(tmp_path, monkeypatch):
    storage = tmp_path / ".runtime/group-storage"
    rows = []
    _mock_volume_backend(monkeypatch, rows)
    monkeypatch.setattr(group_volume.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 10 * group_volume.GROUP_BYTES})())

    for group in ("one", "two"):
        group_volume.prepare_group(tmp_path / ".runtime", "p", "b", group,
                                   total_bytes=2 * group_volume.GROUP_BYTES)
    with pytest.raises(OSError, match="cumulative group storage budget exhausted"):
        group_volume.prepare_group(tmp_path / ".runtime", "p", "b", "three",
                                   total_bytes=2 * group_volume.GROUP_BYTES)

    assert len(rows) == 2
    assert not (storage / group_volume.group_key("p", "b", "three")).exists()


def test_existing_group_remains_available_after_budget_and_free_space_shrink(tmp_path, monkeypatch):
    key = group_volume.group_key("p", "b", "kept")
    (tmp_path / ".runtime").mkdir(mode=0o700)
    directory = tmp_path / ".runtime/group-storage" / key
    directory.mkdir(parents=True)
    directory.parent.chmod(0o700)
    directory.chmod(0o700)
    row = {"key": key, "directory": str(directory), "workspace": str(directory / "workspace"),
           "image": str(directory / "workspace.ext4"), "bytes": group_volume.GROUP_BYTES}
    rows = [row]
    _mock_volume_backend(monkeypatch, rows)
    monkeypatch.setattr(group_volume.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 0})())

    result = group_volume.prepare_group(tmp_path / ".runtime", "p", "b", "kept",
                                        total_bytes=group_volume.GROUP_BYTES,
                                        min_free_bytes=group_volume.GROUP_BYTES)

    assert result["key"] == key
    assert len(rows) == 1


def test_new_group_rejected_for_low_free_space_without_volume_artifacts(tmp_path, monkeypatch):
    rows = []
    _mock_volume_backend(monkeypatch, rows)
    monkeypatch.setattr(group_volume.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": group_volume.GROUP_BYTES})())

    with pytest.raises(OSError, match="insufficient host free space"):
        group_volume.prepare_group(tmp_path / ".runtime", "p", "b", "low-space",
                                   total_bytes=2 * group_volume.GROUP_BYTES,
                                   min_free_bytes=group_volume.GROUP_BYTES)

    key = group_volume.group_key("p", "b", "low-space")
    assert not (tmp_path / ".runtime/group-storage" / key).exists()
    assert not (tmp_path / ".runtime/group-storage").exists()
    assert rows == []


def test_failed_new_volume_creation_rolls_back_only_new_owned_artifacts(tmp_path, monkeypatch):
    rows = []
    _mock_volume_backend(monkeypatch, rows)
    monkeypatch.setattr(group_volume.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 10 * group_volume.GROUP_BYTES})())
    (tmp_path / ".runtime").mkdir(mode=0o700)
    existing = tmp_path / ".runtime/group-storage" / ("a" * 64)
    existing.mkdir(parents=True)
    existing.parent.chmod(0o700)
    sentinel = existing / "retained"
    sentinel.write_text("keep")

    def fail_after_partial_files(workspace, image, capacity):
        workspace.mkdir(mode=0o700)
        image.write_bytes(b"partial")
        group_volume.owner_volume.manifest_path(workspace).write_text("partial")
        Path(str(workspace) + ".volume.lock").touch(mode=0o600)
        raise OSError("simulated mount failure")

    monkeypatch.setattr(group_volume.owner_volume, "prepare", fail_after_partial_files)
    with pytest.raises(OSError, match="simulated mount failure"):
        group_volume.prepare_group(tmp_path / ".runtime", "p", "b", "failed",
                                   total_bytes=2 * group_volume.GROUP_BYTES)

    failed = tmp_path / ".runtime/group-storage" / group_volume.group_key("p", "b", "failed")
    assert not failed.exists()
    assert sentinel.read_text() == "keep"


def test_parallel_creations_cannot_oversubscribe_total_budget(tmp_path, monkeypatch):
    (tmp_path / ".runtime").mkdir(mode=0o700)
    (tmp_path / ".runtime/group-storage").mkdir(mode=0o700)
    rows = []
    rows_lock = threading.Lock()

    def enumerate_locked(root, require_mounted=True):
        with rows_lock:
            return list(rows)

    monkeypatch.setattr(group_volume, "enumerate_groups", enumerate_locked)
    monkeypatch.setattr(group_volume.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 10 * group_volume.GROUP_BYTES})())

    def prepare(workspace, image, capacity):
        # Leave a real check-to-reservation window. Without the global flock
        # both threads can observe zero reservations and must both allocate.
        time.sleep(0.1)
        with rows_lock:
            rows.append({"key": image.parent.name, "directory": str(image.parent),
                         "workspace": str(workspace), "image": str(image), "bytes": capacity})
        return {"workspace": str(workspace), "image": str(image), "bytes": capacity}

    monkeypatch.setattr(group_volume.owner_volume, "prepare", prepare)

    def create(name):
        try:
            group_volume.prepare_group(tmp_path / ".runtime", "p", "b", name,
                                       total_bytes=group_volume.GROUP_BYTES)
            return "created"
        except OSError as exc:
            if "cumulative group storage budget exhausted" in str(exc):
                return "rejected"
            raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ("race-a", "race-b")))

    assert sorted(outcomes) == ["created", "rejected"]
    assert len(rows) == 1


def test_budget_env_defaults_to_five_retained_groups_and_rejects_bad_values(monkeypatch):
    monkeypatch.delenv(group_volume._TOTAL_ENV, raising=False)
    monkeypatch.delenv(group_volume._FREE_ENV, raising=False)
    assert group_volume._budget_bytes(None, group_volume._TOTAL_ENV,
                                      group_volume.DEFAULT_TOTAL_BYTES) == 5 * group_volume.GROUP_BYTES
    monkeypatch.setenv(group_volume._TOTAL_ENV, "5120")
    assert group_volume._budget_bytes(None, group_volume._TOTAL_ENV, 1) == 5 * group_volume.GROUP_BYTES
    monkeypatch.setenv(group_volume._TOTAL_ENV, "1.5")
    with pytest.raises(ValueError, match=group_volume._TOTAL_ENV):
        group_volume._budget_bytes(None, group_volume._TOTAL_ENV, 1)


@pytest.mark.parametrize('mode,allowed', [(0o755, True), (0o775, False), (0o777, False)])
def test_budget_lock_parent_allows_readability_but_rejects_foreign_writers(tmp_path, monkeypatch, mode, allowed):
    runtime = tmp_path / '.runtime'
    runtime.mkdir(mode=mode)
    runtime.chmod(mode)
    rows = []
    _mock_volume_backend(monkeypatch, rows)
    monkeypatch.setattr(group_volume.shutil, 'disk_usage',
                        lambda path: type('Usage', (), {'free': 10 * group_volume.GROUP_BYTES})())
    if allowed:
        group_volume.prepare_group(runtime, 'p', 'b', 'one')
        lock = runtime / '.group-storage-budget.lock'
        assert lock.stat().st_mode & 0o777 == 0o600
        assert len(rows) == 1
    else:
        with pytest.raises(PermissionError, match='parent'):
            group_volume.prepare_group(runtime, 'p', 'b', 'one')
        assert not (runtime / 'group-storage').exists()


def test_group_info_created_on_prepare_and_backfilled_for_existing_volume(tmp_path, monkeypatch):
    import json

    rows = []
    _mock_volume_backend(monkeypatch, rows)
    monkeypatch.setattr(group_volume.shutil, 'disk_usage',
                        lambda path: type('Usage', (), {'free': 10 * group_volume.GROUP_BYTES})())
    result = group_volume.prepare_group(tmp_path / '.runtime', 'qq-platform', '123456', '622671856')
    info = Path(result['directory']) / 'group-info.json'
    expected = {'version': 1, 'platform_id': 'qq-platform',
                'bot_id': '123456', 'group_id': '622671856'}
    assert json.loads(info.read_text()) == expected
    assert info.stat().st_mode & 0o777 == 0o600
    assert info.parent != Path(result['workspace'])
    # Exercise the real reuse branch without allocating/mounting an ext4 image.
    Path(result['image']).touch()
    group_volume.owner_volume.manifest_path(Path(result['workspace'])).touch()
    info.unlink()
    again = group_volume.prepare_group(tmp_path / '.runtime', 'qq-platform', '123456', '622671856')
    assert again['key'] == result['key']
    assert json.loads(info.read_text()) == expected
    assert not list(info.parent.glob('.group-info-*.tmp'))


def test_group_info_replacement_does_not_modify_link_target(tmp_path):
    import json
    import os

    directory = tmp_path / 'group'
    directory.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_text('keep')
    info = directory / 'group-info.json'
    info.symlink_to(outside)
    group_volume._write_group_info(directory, 'p', 'b', 'g')
    assert outside.read_text() == 'keep' and not info.is_symlink()
    info.unlink()
    os.link(outside, info)
    group_volume._write_group_info(directory, 'p', 'b', 'other')
    assert outside.read_text() == 'keep'
    assert json.loads(info.read_text())['group_id'] == 'other'


def test_group_info_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    directory = tmp_path / 'group'
    directory.mkdir()
    info = directory / 'group-info.json'
    info.write_text('previous')
    def fail_replace(*args): raise OSError('simulated write failure')
    monkeypatch.setattr(group_volume.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='simulated write failure'):
        group_volume._write_group_info(directory, 'p', 'b', 'g')
    assert info.read_text() == 'previous'
    assert not list(directory.glob('.group-info-*.tmp'))
