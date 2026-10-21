"""Trusted console configuration storage semantics."""
from __future__ import annotations

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "astrbot-plugins" / "v2_console"
sys.path.insert(0, str(MODULE))
spec = importlib.util.spec_from_file_location("v2_console_store", MODULE / "store.py")
assert spec and spec.loader
store_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store_module)
ConsoleStore = store_module.ConsoleStore
RevisionConflict = store_module.RevisionConflict


def test_defaults_are_copied_per_stable_conversation_and_restored_fieldwise(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    initial = store.read()
    assert initial["defaults"] == {"model": None, "enabled": None, "editors": []}
    with pytest.raises(ValueError, match="model and enabled settings must be configured"):
        store.create_session("qq", "bot-1", "group:123", expected_revision=initial["revision"])
    defaults = {"model": {"provider": "shared", "model": "model-a"},
                "enabled": False, "editors": ["10001"]}
    state = store.update_defaults(defaults, expected_revision=initial["revision"])
    first = store.create_session("qq", "bot-1", "group:123", expected_revision=state["revision"])
    session = first["session"]
    assert first["created"] is True
    assert session["settings"] == defaults

    changed_defaults = {"model": {"provider": "shared", "model": "model-b"},
                        "enabled": True, "editors": ["10002"]}
    state = store.update_defaults(changed_defaults, expected_revision=first["state"]["revision"])
    duplicate = store.create_session("qq", "bot-1", "group:123", expected_revision=state["revision"])
    assert duplicate["created"] is False
    assert duplicate["session"]["settings"] == defaults

    edited = {"model": {"provider": "shared", "model": "custom"},
              "enabled": False, "editors": ["10003"]}
    changed = store.update_session("qq", "bot-1", "group:123", edited,
                                   expected_revision=duplicate["state"]["revision"])
    restored = store.restore_session_field("qq", "bot-1", "group:123", "model",
                                           expected_revision=changed["state"]["revision"])
    assert restored["session"]["settings"] == {
        "model": changed_defaults["model"], "enabled": False, "editors": ["10003"]
    }
    second = store.create_session("qq", "bot-1", "group:456",
                                  expected_revision=restored["state"]["revision"])
    assert second["session"]["settings"] == changed_defaults


def test_revision_conflict_does_not_overwrite_concurrent_update(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    revision = store.read()["revision"]
    left = {"model": {"provider": "p", "model": "left"}, "enabled": True, "editors": []}
    right = {"model": {"provider": "p", "model": "right"}, "enabled": True, "editors": []}
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(store.update_defaults, val, expected_revision=revision)
                   for val in (left, right)]
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except RevisionConflict as exc:
            results.append(exc)
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert sum(isinstance(result, dict) for result in results) == 1


def test_stale_revision_raises_and_state_is_private(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    store.update_defaults({"model": None, "enabled": True, "editors": []},
                          expected_revision=state["revision"])
    with pytest.raises(RevisionConflict):
        store.update_defaults({"model": None, "enabled": False, "editors": []},
                              expected_revision=state["revision"])
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.lock_path.stat().st_mode & 0o777 == 0o600


def test_template_metadata_crud_has_stable_ids_and_no_content_fields(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    created = store.create_template("Base", expected_revision=state["revision"])
    template = created["template"]
    assert set(template) == {"id", "name"}
    renamed = store.rename_template(template["id"], "Research",
                                    expected_revision=created["state"]["revision"])
    assert renamed["template"] == {"id": template["id"], "name": "Research"}
    deleted = store.delete_template(template["id"], expected_revision=renamed["state"]["revision"])
    assert deleted["templates"] == []


def test_create_session_stores_optional_template_without_touching_workspace(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    defaults = {"model": {"provider": "shared", "model": "m"}, "enabled": True, "editors": []}
    state = store.update_defaults(defaults, expected_revision=state["revision"])
    created_template = store.create_template("Group base", expected_revision=state["revision"])
    template_id = created_template["template"]["id"]
    workspace_marker = tmp_path / "workspace" / "existing.txt"
    workspace_marker.parent.mkdir()
    workspace_marker.write_text("preserve")
    created = store.create_session("qq", "bot", "group:1", expected_revision=created_template["state"]["revision"],
                                   template_id=template_id)
    assert created["session"]["template_id"] == template_id
    assert store.read()["sessions"][store.session_key(created["session"]["identity"])]["template_id"] == template_id
    assert workspace_marker.read_text() == "preserve"
    changed = store.set_template("qq", "bot", "group:1", None,
                                 expected_revision=created["state"]["revision"])
    assert "template_id" not in changed["session"]
    assert changed["session"]["settings"] == defaults
    assert workspace_marker.read_text() == "preserve"


def test_import_session_preserves_existing_values_and_never_follows_null_defaults(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    imported_settings = {"model": {"provider": "legacy", "model": "legacy-model"},
                         "enabled": False, "editors": ["20001"]}
    imported = store.import_session("qq", "bot", "group:9", imported_settings,
                                    expected_revision=state["revision"])
    assert imported["created"] is True
    assert imported["session"]["settings"] == imported_settings
    changed = store.update_session("qq", "bot", "group:9", {
        "model": {"provider": "manual", "model": "manual-model"},
        "enabled": True, "editors": [],
    }, expected_revision=imported["state"]["revision"])
    duplicated = store.import_session("qq", "bot", "group:9", imported_settings,
                                      expected_revision=changed["state"]["revision"])
    assert duplicated["created"] is False
    assert duplicated["session"]["settings"] == changed["session"]["settings"]
    duplicate_with_ignored_payload = store.import_session(
        "qq", "bot", "group:9", {"model": None}, expected_revision=duplicated["state"]["revision"]
    )
    assert duplicate_with_ignored_payload["created"] is False
    assert duplicate_with_ignored_payload["session"]["settings"] == changed["session"]["settings"]
    with pytest.raises(ValueError, match="model must be explicitly configured"):
        store.import_session("qq", "bot", "group:10",
                             {"model": None, "enabled": True, "editors": []},
                             expected_revision=duplicate_with_ignored_payload["state"]["revision"])


def test_resume_metadata_is_atomic_preserved_validated_and_restart_safe(tmp_path):
    path = tmp_path / "private" / "console.json"
    store = ConsoleStore(path)
    state = store.read()
    imported = store.import_session("qq", "bot", "private:42", {
        "model": {"provider": "p", "model": "m"}, "enabled": False, "editors": [],
    }, expected_revision=state["revision"])
    revision = imported["state"]["revision"]
    enabled = store.update_session("qq", "bot", "private:42", {
        "model": {"provider": "p", "model": "m"}, "enabled": True, "editors": [],
    }, expected_revision=revision, resume_through_journal_id=731)
    assert enabled["state"]["revision"] == revision + 1
    assert enabled["session"]["resume_through_journal_id"] == 731

    restarted = ConsoleStore(path)
    row = restarted.read()["sessions"][restarted.session_key({
        "platform_id": "qq", "bot_id": "bot", "conversation_key": "private:42",
    })]
    assert row["resume_through_journal_id"] == 731
    changed = restarted.update_session("qq", "bot", "private:42", {
        "model": {"provider": "p", "model": "next"}, "enabled": True, "editors": [],
    }, expected_revision=restarted.read()["revision"])
    assert changed["session"]["resume_through_journal_id"] == 731

    for invalid in (-1, True, 1.5, "2"):
        with pytest.raises(ValueError, match="non-negative integer"):
            restarted.update_session("qq", "bot", "private:42", changed["session"]["settings"],
                                     expected_revision=changed["state"]["revision"],
                                     resume_through_journal_id=invalid)


def test_old_session_schema_remains_readable_and_stale_revision_cannot_write_resume_metadata(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    imported = store.import_session("qq", "bot", "group:7", {
        "model": {"provider": "p", "model": "m"}, "enabled": False, "editors": [],
    }, expected_revision=state["revision"])
    assert "resume_through_journal_id" not in imported["session"]
    with pytest.raises(RevisionConflict):
        store.update_session("qq", "bot", "group:7", imported["session"]["settings"],
                             expected_revision=state["revision"], resume_through_journal_id=12)
    assert "resume_through_journal_id" not in store.read()["sessions"][
        store.session_key(imported["session"]["identity"])
    ]


def test_create_and_restore_reject_unconfigured_model(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    state = store.update_defaults({"model": None, "enabled": True, "editors": []},
                                  expected_revision=state["revision"])
    with pytest.raises(ValueError, match="model and enabled settings must be configured"):
        store.create_session("qq", "bot", "group:1", expected_revision=state["revision"])
    state = store.update_defaults({"model": {"provider": "p", "model": "m"},
                                   "enabled": True, "editors": []},
                                  expected_revision=state["revision"])
    session = store.create_session("qq", "bot", "group:1", expected_revision=state["revision"])
    state = store.update_defaults({"model": None, "enabled": True, "editors": []},
                                  expected_revision=session["state"]["revision"])
    with pytest.raises(ValueError, match="default model must be configured"):
        store.restore_session_field("qq", "bot", "group:1", "model",
                                    expected_revision=state["revision"])


@pytest.mark.parametrize("invalid", [
    {"model": {"provider": "p", "model": "m", "api_key": "secret"}, "enabled": True, "editors": []},
    {"model": None, "enabled": 1, "editors": []},
    {"model": None, "enabled": True, "editors": ["abc"]},
    {"model": None, "enabled": True, "editors": ["1", "1"]},
])
def test_settings_reject_unknown_or_invalid_values(tmp_path, invalid):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    with pytest.raises(ValueError):
        store.update_defaults(invalid, expected_revision=store.read()["revision"])


def test_complete_identity_is_collision_free_and_store_rejects_corrupt_schema(tmp_path):
    store = ConsoleStore(tmp_path / "private" / "console.json")
    state = store.read()
    state = store.update_defaults({"model": {"provider": "p", "model": "m"}, "enabled": True, "editors": []},
                                 expected_revision=state["revision"])
    a = store.create_session("p:a", "b", "c", expected_revision=state["revision"])
    b = store.create_session("p", "a:b", "c", expected_revision=a["state"]["revision"])
    assert a["session"] != b["session"]
    path = store.path
    raw = json.loads(path.read_text())
    raw["api_key"] = "secret"
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="structure"):
        store.read()
