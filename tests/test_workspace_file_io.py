from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "dsh/plugins/agent-profile"
sys.path.insert(0, str(PLUGIN))
import workspace_file_io as workspace_io
import group_volume


@pytest.fixture
def root_fd(tmp_path):
    (tmp_path / "skills" / "demo").mkdir(parents=True)
    (tmp_path / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\nbody\n", encoding="utf-8")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield tmp_path, fd
    finally:
        os.close(fd)


def test_read_and_list_are_rooted_and_return_native_fs_shapes(root_fd):
    root, fd = root_fd
    entries = workspace_io.operate(fd, {"op": "list", "path": "skills"})
    assert [(row["name"], row["type"]) for row in entries] == [("demo", "directory")]

    skill_dir = workspace_io.operate(fd, {"op": "list", "path": "skills/demo"})
    assert skill_dir[0]["name"] == "SKILL.md"
    info = workspace_io.operate(fd, {"op": "stat", "path": "skills/demo/SKILL.md"})
    assert info["type"] == "file"
    assert workspace_io.operate(fd, {"op": "read", "path": "skills/demo/SKILL.md"})["text"].endswith("body\n")
    assert workspace_io.operate(fd, {"op": "stat", "path": "skills/missing"}) is None
    fingerprint = workspace_io.operate(fd, {"op": "fingerprint-skills"})
    assert fingerprint[0]["name"] == "demo"
    assert fingerprint[0]["skill"]["type"] == "file"


@pytest.mark.parametrize("unsafe", ["../outside", "/etc/passwd", "skills/../outside", "skills//demo"])
def test_rejects_paths_outside_or_with_ambiguous_components(root_fd, unsafe):
    _, fd = root_fd
    with pytest.raises(ValueError):
        workspace_io.operate(fd, {"op": "read", "path": unsafe})


def test_symlinks_hardlinks_fifo_and_special_files_never_become_readable(root_fd, tmp_path):
    root, fd = root_fd
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "skills/linked-dir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((PermissionError, NotADirectoryError)):
        workspace_io.operate(fd, {"op": "read", "path": "skills/linked-dir/outside.txt"})

    # Use a workspace subdirectory to verify unsafe entries are represented as other.
    (root / "skills/demo/linked.txt").symlink_to(outside)
    (root / "skills/demo/hardlinked.txt").write_text("temporary", encoding="utf-8")
    os.link(root / "skills/demo/hardlinked.txt", root / "skills/demo/hardlinked-copy.txt")
    os.mkfifo(root / "skills/demo/pipe")
    entries = {row["name"]: row for row in workspace_io.operate(fd, {"op": "list", "path": "skills/demo"})}
    assert entries["linked.txt"]["type"] == "other"
    assert entries["hardlinked.txt"]["type"] == "other"
    assert entries["pipe"]["type"] == "other"
    for name in ("linked.txt", "hardlinked.txt", "pipe"):
        assert workspace_io.operate(fd, {"op": "stat", "path": f"skills/demo/{name}"})["type"] == "other"
    for name in ("linked.txt", "hardlinked.txt", "pipe"):
        with pytest.raises((PermissionError, IsADirectoryError)):
            workspace_io.operate(fd, {"op": "read", "path": f"skills/demo/{name}"})


def test_rejects_invalid_utf8_and_oversize_text_without_blocking(tmp_path):
    (tmp_path / "invalid").write_bytes(b"\xff")
    (tmp_path / "large").write_bytes(b"x" * (workspace_io.MAX_TEXT_BYTES + 1))
    os.mkfifo(tmp_path / "fifo")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(UnicodeDecodeError):
            workspace_io.operate(fd, {"op": "read", "path": "invalid"})
        with pytest.raises(ValueError, match="size limit"):
            workspace_io.operate(fd, {"op": "read", "path": "large"})
        with pytest.raises(PermissionError):
            workspace_io.operate(fd, {"op": "read", "path": "fifo"})
    finally:
        os.close(fd)


def test_rejects_guide_over_limit(root_fd):
    root, fd = root_fd
    (root / "AGENTS.md").write_bytes(b"x" * (workspace_io.MAX_GUIDE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        workspace_io.operate(fd, {"op": "read", "path": "AGENTS.md", "kind": "guide"})


def test_directory_listing_has_a_fixed_entry_limit(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    for index in range(workspace_io.MAX_ENTRIES + 1):
        (skills / f"item-{index}").touch()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="too many entries"):
            workspace_io.operate(fd, {"op": "list", "path": "skills"})
    finally:
        os.close(fd)


def test_group_guide_bootstrap_preserves_existing_files_and_does_not_resurrect_deletions(tmp_path, monkeypatch):
    fake_repo = tmp_path / "repo"
    plugin_dir = fake_repo / "dsh/plugins/agent-profile"
    plugin_dir.mkdir(parents=True)
    template_dir = fake_repo / "dsh/templates/group"
    template_dir.mkdir(parents=True)
    (template_dir / "AGENTS.md").write_text("base agents\n", encoding="utf-8")
    (template_dir / "persona.md").write_text("base persona\n", encoding="utf-8")
    monkeypatch.setattr(group_volume, "__file__", str(plugin_dir / "group_volume.py"))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("member edits\n", encoding="utf-8")
    group_dir = tmp_path / "group-volume"
    group_dir.mkdir(mode=0o700)
    root_fd = os.open(workspace, os.O_PATH | os.O_DIRECTORY)
    monkeypatch.setattr(group_volume, "verified_group_workspace", lambda _path: os.dup(root_fd))
    try:
        group_volume._seed_group_guides(workspace, group_dir)
        assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "member edits\n"
        assert (workspace / "persona.md").read_text(encoding="utf-8") == "base persona\n"
        assert (group_dir / ".workspace-guides-initialized.json").exists()

        (workspace / "persona.md").unlink()
        group_volume._seed_group_guides(workspace, group_dir)
        assert not (workspace / "persona.md").exists()
    finally:
        os.close(root_fd)
