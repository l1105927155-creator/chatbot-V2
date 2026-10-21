from __future__ import annotations

import os
import io
import multiprocessing
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "astrbot-plugins/v2_console"
sys.path.insert(0, str(PLUGIN))
from files import WorkspaceFiles  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "dsh/plugins/agent-profile"))
from workspace_lock import workspace_lock  # noqa: E402


def _locked_bot_write(lock_path, target, ready, release):
    with workspace_lock(lock_path):
        Path(target).write_bytes(b"bot edit")
        ready.set()
        if not release.wait(10):
            raise RuntimeError("test release was not signaled")


@pytest.fixture
def workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    with WorkspaceFiles(root) as files:
        yield root, files


def test_create_list_read_update_and_version_conflict(workspace):
    root, files = workspace
    files.mkdir("notes")
    created = files.write("notes/first.md", b"hello", None)
    row = files.list("notes")[0]
    assert row["name"] == "first.md"
    assert row["is_dir"] is False
    assert row["version"] == created["version"]
    read = files.read("notes/first.md")
    assert read["data"] == b"hello"
    assert read["version"] == row["version"]

    updated = files.write("notes/first.md", b"updated", read["version"])
    assert (root / "notes/first.md").read_bytes() == b"updated"
    with pytest.raises(FileExistsError, match="version conflict"):
        files.write("notes/first.md", b"stale", read["version"])
    with pytest.raises(FileExistsError):
        files.write("notes/first.md", b"duplicate", None)
    assert files.read("notes/first.md")["version"] == updated["version"]


def test_upload_download_rename_and_delete_empty_directory(workspace):
    root, files = workspace
    files.mkdir("assets")
    uploaded = files.upload("assets/raw.bin", b"\x00\xffpayload")
    data, metadata = files.download("assets/raw.bin")
    assert data == b"\x00\xffpayload"
    assert metadata["version"] == uploaded["version"]
    files.rename("assets/raw.bin", "assets/moved.bin", uploaded["version"])
    assert not (root / "assets/raw.bin").exists()
    assert (root / "assets/moved.bin").read_bytes() == data
    moved = files.list("assets")[0]
    files.delete("assets/moved.bin", moved["version"])
    files.delete("assets", files.list("")[0]["version"])
    assert not (root / "assets").exists()


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "a/../../x", "a\\b", "", "."])
def test_rejects_unsafe_paths(workspace, path):
    _, files = workspace
    with pytest.raises(ValueError):
        files.read(path)


def test_root_listing_and_root_delete_protection(workspace):
    root, files = workspace
    (root / "folder").mkdir()
    rows = files.list("")
    assert rows[0]["name"] == "folder"
    assert rows[0]["is_dir"]
    with pytest.raises(ValueError):
        files.delete("", rows[0]["version"])


def test_symlinks_hardlinks_special_files_and_escape_are_blocked(workspace, tmp_path):
    root, files = workspace
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (root / "outside-link").symlink_to(outside)
    (root / "inside").write_text("safe")
    os.link(root / "inside", root / "hardlink")
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe")

    rows = {row["name"]: row for row in files.list("")}
    assert rows["outside-link"]["type"] == "other"
    assert rows["hardlink"]["type"] == "other"
    if hasattr(os, "mkfifo"):
        assert rows["pipe"]["type"] == "other"
    with pytest.raises(PermissionError):
        files.read("outside-link")
    with pytest.raises(PermissionError):
        files.read("hardlink")
    with pytest.raises(PermissionError):
        files.read("pipe")
    with pytest.raises(PermissionError):
        files.write("outside-link", b"overwritten", None)
    assert outside.read_text() == "secret"


def test_rename_does_not_replace_existing_and_delete_requires_current_version(workspace):
    _, files = workspace
    a = files.write("a.txt", b"a", None)
    files.write("b.txt", b"b", None)
    with pytest.raises(FileExistsError):
        files.rename("a.txt", "b.txt", a["version"])
    changed = files.write("a.txt", b"changed", a["version"])
    with pytest.raises(FileExistsError, match="version conflict"):
        files.delete("a.txt", a["version"])
    files.delete("a.txt", changed["version"])


def test_concurrent_updates_with_same_version_only_commit_once(workspace):
    root, files = workspace
    initial = files.write("shared.txt", b"original", None)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def update(value: bytes) -> None:
        barrier.wait()
        try:
            files.write("shared.txt", value, initial["version"])
        except FileExistsError:
            outcomes.append("conflict")
        else:
            outcomes.append("saved")

    workers = [threading.Thread(target=update, args=(value,))
               for value in (b"first", b"second")]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert sorted(outcomes) == ["conflict", "saved"]
    assert (root / "shared.txt").read_bytes() in (b"first", b"second")


def test_non_empty_directory_cannot_be_deleted_recursively(workspace):
    root, files = workspace
    files.mkdir("tree")
    child = files.write("tree/child", b"preserve", None)
    directory = next(row for row in files.list("") if row["name"] == "tree")
    with pytest.raises(OSError):
        files.delete("tree", directory["version"])
    assert (root / "tree/child").read_bytes() == b"preserve"
    files.delete("tree/child", child["version"])
    updated_directory = next(row for row in files.list("") if row["name"] == "tree")
    files.delete("tree", updated_directory["version"])


def test_root_path_must_be_real_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        WorkspaceFiles(link)


def test_console_save_waits_for_cross_process_bot_edit_then_reports_version_conflict(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    lock_path = root.parent / ".workspace-file.lock"
    with WorkspaceFiles(root, lock_path=lock_path) as files:
        original = files.write("shared.md", b"initial", None)
        context = multiprocessing.get_context("fork")
        ready, release = context.Event(), context.Event()
        bot = context.Process(target=_locked_bot_write,
                              args=(str(lock_path), str(root / "shared.md"), ready, release))
        bot.start()
        assert ready.wait(3), "simulated Bot writer did not acquire the workspace lock"

        outcome = []
        completed = threading.Event()

        def stale_console_save():
            try:
                files.write("shared.md", b"stale editor save", original["version"])
            except FileExistsError as error:
                outcome.append(error)
            finally:
                completed.set()

        editor = threading.Thread(target=stale_console_save)
        editor.start()
        time.sleep(0.2)
        assert not completed.is_set(), "console write did not wait for the Bot process lock"
        release.set()
        editor.join(3)
        bot.join(3)
        assert not editor.is_alive()
        assert bot.exitcode == 0
        assert len(outcome) == 1 and "version conflict" in str(outcome[0])
        assert (root / "shared.md").read_bytes() == b"bot edit"
        assert lock_path.stat().st_mode & 0o777 == 0o600


def test_external_workspace_lock_rejects_symlink_hardlink_and_unsafe_mode(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    lock_path = tmp_path / ".workspace-file.lock"
    target = tmp_path / "target"
    target.write_text("x")
    lock_path.symlink_to(target)
    with WorkspaceFiles(root, lock_path=lock_path) as files:
        with pytest.raises(OSError):
            files.list()
    lock_path.unlink()
    lock_path.write_text("x")
    os.chmod(lock_path, 0o600)
    os.link(lock_path, tmp_path / "lock-alias")
    with WorkspaceFiles(root, lock_path=lock_path) as files:
        with pytest.raises(PermissionError):
            files.list()
    lock_path.unlink()
    (tmp_path / "lock-alias").unlink()
    lock_path.write_text("x")
    os.chmod(lock_path, 0o644)
    with WorkspaceFiles(root, lock_path=lock_path) as files:
        with pytest.raises(PermissionError):
            files.list()


def test_export_zip_preserves_nested_empty_binary_and_unicode_hidden_entries(workspace):
    root, files = workspace
    files.mkdir("目录")
    files.mkdir("目录/空目录")
    files.write("目录/.隐藏文件", b"\x00\xffbinary\n", None)
    files.write("根文件.txt", "中文内容".encode(), None)
    output = io.BytesIO()

    files.export_zip(output)

    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        assert set(archive.namelist()) == {"目录/", "目录/空目录/", "目录/.隐藏文件", "根文件.txt"}
        assert archive.read("目录/.隐藏文件") == b"\x00\xffbinary\n"
        assert archive.read("根文件.txt") == "中文内容".encode()
        assert archive.read("目录/空目录/") == b""


def test_export_zip_skips_links_special_files_and_unsafe_names(workspace, tmp_path):
    root, files = workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "external").symlink_to(outside)
    (root / "internal").write_text("allowed")
    os.link(root / "internal", root / "hardlink")
    (root / "single-link.txt").write_text("included")
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe")
    (root / "unsafe\nname").write_text("not packaged")
    output = io.BytesIO()

    files.export_zip(output)

    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        assert archive.namelist() == ["single-link.txt"]
        assert archive.read("single-link.txt") == b"included"
    assert outside.read_text() == "secret"


def test_export_zip_observes_cross_process_workspace_lock(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    lock_path = tmp_path / ".workspace-file.lock"
    with WorkspaceFiles(root, lock_path=lock_path) as files:
        context = multiprocessing.get_context("fork")
        ready, release = context.Event(), context.Event()
        bot = context.Process(target=_locked_bot_write,
                              args=(str(lock_path), str(root / "message.txt"), ready, release))
        bot.start()
        assert ready.wait(3)
        completed = threading.Event()
        output = io.BytesIO()

        def export():
            try:
                files.export_zip(output)
            finally:
                completed.set()

        worker = threading.Thread(target=export)
        worker.start()
        time.sleep(0.2)
        assert not completed.is_set(), "export bypassed the Bot's workspace lock"
        release.set()
        worker.join(3)
        bot.join(3)
        assert not worker.is_alive()
        assert bot.exitcode == 0
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            assert archive.read("message.txt") == b"bot edit"


def test_export_zip_fails_when_file_changes_while_streaming(workspace, monkeypatch):
    root, files = workspace
    target = root / "large.bin"
    target.write_bytes(b"a" * (4 * 1024 * 1024))
    original_read = os.read
    watched_fd = None
    changed = False

    def read_and_mutate(fd, count):
        nonlocal watched_fd, changed
        chunk = original_read(fd, count)
        if watched_fd is None and chunk:
            watched_fd = fd
        if fd == watched_fd and chunk and not changed:
            changed = True
            with target.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"changed!")
                stream.flush()
                os.fsync(stream.fileno())
        return chunk

    monkeypatch.setattr("files.os.read", read_and_mutate)
    with pytest.raises(OSError, match="changed while being exported"):
        files.export_zip(io.BytesIO())
    assert changed


def test_export_zip_cancellation_cleans_up_by_raising(workspace, monkeypatch):
    root, files = workspace
    (root / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    cancelled = threading.Event()
    original_read = os.read
    watched_fd = None

    def read_then_cancel(fd, count):
        nonlocal watched_fd
        chunk = original_read(fd, count)
        if watched_fd is None and chunk:
            watched_fd = fd
        if fd == watched_fd and chunk:
            cancelled.set()
        return chunk

    monkeypatch.setattr("files.os.read", read_then_cancel)
    with pytest.raises(RuntimeError, match="cancelled"):
        files.export_zip(io.BytesIO(), cancelled=cancelled)


def test_export_zip_streams_large_files_with_bounded_memory(workspace):
    import tracemalloc

    root, files = workspace
    with (root / "large.bin").open("wb") as stream:
        stream.truncate(24 * 1024 * 1024)
    output = tempfile.TemporaryFile()
    tracemalloc.start()
    try:
        files.export_zip(output)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    output.seek(0)
    with zipfile.ZipFile(output) as archive:
        assert archive.getinfo("large.bin").file_size == 24 * 1024 * 1024



def test_directory_versions_hash_large_files_without_loading_payload(workspace):
    import tracemalloc

    root, files = workspace
    with (root / "large.bin").open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    tracemalloc.start()
    try:
        entries = files.list("")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert entries[0]["name"] == "large.bin"
    assert peak < 4 * 1024 * 1024
