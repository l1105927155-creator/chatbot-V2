"""Bundled workspace persona templates preserve user state across startup and retries."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "astrbot-plugins/v2_console"
PACKAGE = "console_persona_test_pkg"
spec = importlib.util.spec_from_file_location(
    PACKAGE, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = module
spec.loader.exec_module(module)

from importlib import import_module

ConsoleStore = import_module(PACKAGE + ".store").ConsoleStore
installer = import_module(PACKAGE + ".preset_templates")
install = installer.install_workspace_persona_presets


def test_presets_initialize_each_complete_workspace_and_keep_existing_template(tmp_path):
    store = ConsoleStore(tmp_path / "private/state.json")
    default = store.create_template("我原来的默认模板", expected_revision=0)["template"]
    tpl = tmp_path / "templates"
    tpl.mkdir(mode=0o700)
    original = tpl / default["id"]
    original.mkdir(mode=0o700)
    (original / "persona.md").write_text("我的原始人格")

    before = store.read()["revision"]
    assert install(store, tpl, REPO) == 6
    after = store.read()
    assert after["revision"] == before + 1
    assert after["templates"][0] == default
    assert len(after["templates"]) == 7
    assert (original / "persona.md").read_text() == "我的原始人格"
    source = REPO / "dsh/templates/group/AGENTS.md"
    for row in after["templates"][1:]:
        assert (tpl / row["id"] / "persona.md").read_text().startswith("# Persona")
        assert (tpl / row["id"] / "AGENTS.md").read_bytes() == source.read_bytes()
    assert install(store, tpl, REPO) == 0
    assert store.read()["revision"] == after["revision"]


def test_renamed_edited_and_deleted_presets_remain_so_after_restart(tmp_path):
    store = ConsoleStore(tmp_path / "private/state.json")
    tpl = tmp_path / "templates"
    install(store, tpl, REPO)
    state = store.read()
    first, second = state["templates"][:2]
    store.rename_template(first["id"], "我的重命名", expected_revision=state["revision"])
    (tpl / first["id"] / "persona.md").write_text("自主编辑", encoding="utf-8")
    state = store.read()
    store.delete_template(second["id"], expected_revision=state["revision"])
    before = store.read()
    assert install(ConsoleStore(store.path), tpl, REPO) == 0
    assert store.read() == before
    assert (tpl / first["id"] / "persona.md").read_text() == "自主编辑"
    assert all(item["id"] != second["id"] for item in before["templates"])


def test_retry_after_metadata_commit_skips_existing_edits(tmp_path):
    store = ConsoleStore(tmp_path / "private/state.json")
    tpl = tmp_path / "templates"
    install(store, tpl, REPO)
    first = store.read()["templates"][0]
    (tpl / first["id"] / "persona.md").write_text("运行后的改动", encoding="utf-8")
    (store.root / installer._MARKER).unlink()
    previous = store.read()
    assert install(store, tpl, REPO) == 0
    assert store.read() == previous
    assert (tpl / first["id"] / "persona.md").read_text() == "运行后的改动"


def test_unregistered_conflicting_preset_files_are_not_overwritten(tmp_path):
    store = ConsoleStore(tmp_path / "private/state.json")
    tpl = tmp_path / "templates"
    first_id = "5b387989-9f5e-4373-b39a-03e2eac99b1f"
    tpl.mkdir(mode=0o700)
    folder = tpl / first_id
    folder.mkdir(mode=0o700)
    (folder / "persona.md").write_text("私人内容", encoding="utf-8")
    with pytest.raises(FileExistsError, match="conflicts"):
        install(store, tpl, REPO)
    assert (folder / "persona.md").read_text() == "私人内容"
    assert store.read()["templates"] == []


def _legacy_seed(store, tpl: Path, monkeypatch):
    """Simulate the v1 console catalog and private installation marker."""
    tpl.mkdir(mode=0o700)
    old_entries = (
        [{"id": key, "name": entry[0]} for key, entry in installer._LEGACY_V1.items()]
        + [{"id": key, "name": entry[0]} for key, entry in installer._LEGACY_RENAMES.items()]
    )
    store.seed_templates(old_entries)
    for item in old_entries:
        folder = tpl / item["id"]
        folder.mkdir(mode=0o700)
        (folder / "AGENTS.md").write_bytes(b"legacy guide")
        (folder / "persona.md").write_bytes(b"stock persona")
    marker = store.root / installer._LEGACY_MARKER
    marker.write_bytes(b"installed-v1\n")
    marker.chmod(0o600)

    stock_hash = installer._git_blob(b"stock persona")
    monkeypatch.setattr(installer, "_LEGACY_GUIDE_BLOB", installer._git_blob(b"legacy guide"))
    monkeypatch.setattr(installer, "_LEGACY_V1", {
        key: (entry[0], stock_hash) for key, entry in installer._LEGACY_V1.items()
    })
    monkeypatch.setattr(installer, "_LEGACY_RENAMES", {
        key: (entry[0], entry[1], stock_hash)
        for key, entry in installer._LEGACY_RENAMES.items()
    })


def test_upgrade_v1_retires_only_untouched_unused_templates(tmp_path, monkeypatch):
    store = ConsoleStore(tmp_path / "private/state.json")
    tpl = tmp_path / "templates"
    _legacy_seed(store, tpl, monkeypatch)
    legacy = installer._LEGACY_V1
    stock, referenced, edited, renamed = list(legacy)
    (tpl / edited / "persona.md").write_text("customized")
    store.rename_template(renamed, "我的自定义名称",
                          expected_revision=store.read()["revision"])

    state = store.update_defaults(
        {"model": {"provider": "p", "model": "m"}, "enabled": True, "editors": []},
        expected_revision=store.read()["revision"])
    store.create_session("qq", "bot", "group:15",
                         expected_revision=state["revision"], template_id=referenced)
    assert install(store, tpl, REPO) == 4
    names = {row["id"]: row["name"] for row in store.read()["templates"]}
    assert stock not in names
    assert (tpl / stock / "persona.md").read_bytes() == b"stock persona"
    assert names[referenced] == legacy[referenced][0]
    assert names[edited] == legacy[edited][0]
    assert names[renamed] == "我的自定义名称"
    for template_id, (_old, new, _hash) in installer._LEGACY_RENAMES.items():
        assert names[template_id] == new
        assert (tpl / template_id / "persona.md").read_bytes() == b"stock persona"
    assert len(names) == 9
    before = store.read()
    assert install(ConsoleStore(store.path), tpl, REPO) == 0
    assert store.read() == before


def test_upgrade_keeps_old_template_with_custom_guide_or_extra_files(tmp_path, monkeypatch):
    store = ConsoleStore(tmp_path / "private/state.json")
    tpl = tmp_path / "templates"
    _legacy_seed(store, tpl, monkeypatch)
    keys = list(installer._LEGACY_V1)
    (tpl / keys[0] / "AGENTS.md").write_text("custom guide")
    (tpl / keys[1] / "my-notes.txt").write_text("extra content")
    install(store, tpl, REPO)
    retained = {row["id"] for row in store.read()["templates"]}
    assert keys[0] in retained
    assert keys[1] in retained
