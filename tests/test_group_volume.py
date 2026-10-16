from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
