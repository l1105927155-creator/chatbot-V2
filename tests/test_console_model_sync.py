"""The console's per-conversation model is the live route for every operation."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "astrbot-plugins" / "v2_dsh_router"
CONSOLE = ROOT / "astrbot-plugins" / "v2_console"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(CONSOLE))
sys.path.insert(0, str(ROOT / "astrbot-plugins"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = _load("v2_console_model_sync_journal", ROOT / "astrbot-plugins/v2_journal/journal.py")
state_module = _load("v2_console_model_sync_state", ROUTER / "state.py")
router = _load("v2_console_model_sync_service", ROUTER / "service.py")
console_store_module = _load("v2_console_model_sync_store", CONSOLE / "store.py")
ConsoleStore = console_store_module.ConsoleStore
from v2_memory.backend import GroupMemoryBackend

OLD = ("wb", "retired-model")
SELECTED = ("wb", "selected-model")
PROVIDER_SELECTED = ("wb", "provider-command-model")
PLUGIN_SELECTED = ("wb", "plugin-selected-model")


class FakeDsh:
    """Record routes without making provider, ACP, or QQ calls."""

    def __init__(self):
        self.calls = []
        self.counter = 0

    def create_session(self, *_):
        self.counter += 1
        sid = f"sid-{self.counter}"
        self.calls.append(("create", sid))
        return sid

    def model_status(self, sid, *_, model_route=None):
        self.calls.append(("status", sid, model_route))
        return {"current": {"provider": model_route[0], "model": model_route[1]}}

    def select_model(self, sid, provider, model, *_, previous_route=None):
        self.calls.append(("select", sid, (provider, model), previous_route))
        return {"current": {"provider": provider, "model": model}}

    def run(self, sid, _prompt, *_, model_route=None, **__):
        self.calls.append(("run", sid, model_route))
        return "ok"

    def drain_session(self, sid, *_):
        self.calls.append(("drain", sid))

    def compact_session(self, sid, *_, model_route=None):
        self.calls.append(("compact", sid, model_route))
        return {"compacted": True}

    def close_session(self, sid):
        self.calls.append(("close", sid))


def _append(store, conv, message_id, text="hello"):
    group = conv.key.startswith("group:")
    raw = {
        "post_type": "message",
        "message_type": "group" if group else "private",
        "self_id": int(conv.bot_id),
        "user_id": 42,
        "group_id": int(conv.key.partition(":")[2]) if group else None,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return store.append(journal.message_from_onebot(conv.platform_id, raw))[0]


def _configured_store(path):
    store = ConsoleStore(path)
    state = store.read()
    store.update_defaults(
        {"model": {"provider": SELECTED[0], "model": SELECTED[1]},
         "enabled": True, "editors": []},
        expected_revision=state["revision"],
    )
    return store


def test_console_model_is_fresh_for_chat_reset_compaction_and_restart(tmp_path, monkeypatch):
    conv = journal.Conversation("qq", "1", "group:1055300305")
    other = journal.Conversation("qq", "1", "group:1055300306")
    missing = journal.Conversation("qq", "1", "group:1055300307")
    j = journal.JournalStore(tmp_path / "journal.sqlite3")
    state_path = tmp_path / "state.sqlite3"
    state = state_module.StateStore(state_path)
    dsh = FakeDsh()
    console_path = tmp_path / "console.json"
    console = _configured_store(console_path)
    svc = router.ChatService(j, state, dsh)
    svc.console_store = console

    console.create_session(
        "qq", "1", conv.key, expected_revision=console.read()["revision"])
    console = ConsoleStore(console_path)
    console.create_session(
        "qq", "1", other.key, expected_revision=console.read()["revision"])
    console = ConsoleStore(console_path)
    # The legacy table intentionally retains a model removed from the provider catalog.
    state.get_or_create("qq", "1", conv.key, "existing-session")
    state.set_model_route("qq", "1", conv.key, OLD)
    state.get_or_create("qq", "1", other.key, "other-session")
    state.set_model_route("qq", "1", other.key, OLD)
    state.get_or_create("qq", "1", missing.key, "missing-session")
    state.set_model_route("qq", "1", missing.key, OLD)

    def forbidden_legacy_read(*_args):
        raise AssertionError("console mode must not read the stale legacy model route")

    monkeypatch.setattr(state, "get_model_route", forbidden_legacy_read)

    async def send(_):
        return None

    async def scenario():
        with pytest.raises(PermissionError, match="尚未在控制台接入"):
            await svc.model_status(missing)

        # A provider command changes the model to a value distinct from the console default.
        # The immediately following /new must carry that route to the replacement Session.
        await svc.select_model(conv, *PROVIDER_SELECTED)
        assert console.read()["sessions"][console.session_key({
            "platform_id": "qq", "bot_id": "1", "conversation_key": conv.key,
        })]["settings"]["model"] == {
            "provider": PROVIDER_SELECTED[0], "model": PROVIDER_SELECTED[1]}
        _append(j, conv, "provider-new", "/new skip_mem")
        provider_reset = await svc.reset_conversation(
            conv, "provider-new", skip_memory=True)
        provider_reset_sid = provider_reset.session_id
        assert ("status", provider_reset_sid, PROVIDER_SELECTED) in dsh.calls

        # A plugin page writer updates the same file through another Store instance.
        # The existing service and its existing ConsoleStore object must observe it now.
        updater = ConsoleStore(console_path)
        current = updater.read()
        identity = {"platform_id": "qq", "bot_id": "1", "conversation_key": conv.key}
        row = current["sessions"][updater.session_key(identity)]
        settings = dict(row["settings"])
        settings["model"] = {"provider": PLUGIN_SELECTED[0], "model": PLUGIN_SELECTED[1]}
        updater.update_session("qq", "1", conv.key, settings,
                               expected_revision=current["revision"])

        _append(j, conv, "chat-1")
        await svc.handle(conv, "chat-1", send)
        assert dsh.calls[-1] == ("run", provider_reset_sid, PLUGIN_SELECTED)

        # Slash reset with skip_memory rotates the Session but must inherit the same
        # current console model, without consulting the legacy route.
        _append(j, conv, "new-1", "/new skip_mem")
        result = await svc.reset_conversation(conv, "new-1", skip_memory=True)
        reset_sid = result.session_id
        assert ("status", reset_sid, PLUGIN_SELECTED) in dsh.calls

        # A rebuilt service/store also picks up current settings for console reset.
        restarted = router.ChatService(j, state_module.StateStore(state_path), dsh)
        restarted.console_store = ConsoleStore(console_path)
        assert restarted._selected_model_route(conv) == PLUGIN_SELECTED

        # Console reset and compaction each observe subsequent page updates.
        console_after = ConsoleStore(console_path)
        latest = console_after.read()
        row = latest["sessions"][console_after.session_key(identity)]
        settings = dict(row["settings"])
        settings["model"] = {"provider": SELECTED[0], "model": "console-reset-model"}
        console_after.update_session("qq", "1", conv.key, settings,
                                     expected_revision=latest["revision"])
        await restarted.reset_from_console(conv)
        console_reset_sid = state_module.StateStore(state_path).get(
            "qq", "1", conv.key).session_id
        assert ("status", console_reset_sid, (SELECTED[0], "console-reset-model")) in dsh.calls

        latest = restarted.console_store.read()
        row = latest["sessions"][restarted.console_store.session_key(identity)]
        settings = dict(row["settings"])
        settings["model"] = {"provider": PLUGIN_SELECTED[0], "model": "latest-compaction-model"}
        restarted.console_store.update_session("qq", "1", conv.key, settings,
                                               expected_revision=latest["revision"])
        await restarted.compact_from_console(conv)
        assert dsh.calls[-1] == ("compact", console_reset_sid,
                                 (PLUGIN_SELECTED[0], "latest-compaction-model"))

        # No conversation-scoped change leaks into the neighboring group.
        other_route = console.read()["sessions"][
            console.session_key({"platform_id": "qq", "bot_id": "1", "conversation_key": other.key})
        ]["settings"]["model"]
        assert other_route == {"provider": SELECTED[0], "model": SELECTED[1]}
        assert state_module.StateStore(state_path).get_model_route(
            "qq", "1", other.key) == OLD
        assert state_module.StateStore(state_path).get_model_route(
            "qq", "1", missing.key) == OLD

    asyncio.run(scenario())


def test_memory_and_scheduled_routes_read_current_console_model(tmp_path, monkeypatch):
    conv = journal.Conversation("qq", "1", "group:1055300305")
    console_path = tmp_path / "console.json"
    console = _configured_store(console_path)
    console.create_session("qq", "1", conv.key,
                          expected_revision=console.read()["revision"])
    console = ConsoleStore(console_path)

    # The memory backend reads the ConsoleStore on each batch route lookup.
    backend = GroupMemoryBackend.__new__(GroupMemoryBackend)
    backend.conversation = conv
    backend.router = SimpleNamespace(console_store=console)
    backend._settings = lambda store: router.session_settings(store, conv)
    assert backend._route() == SELECTED
    updater = ConsoleStore(console_path)
    latest = updater.read()
    identity = {"platform_id": "qq", "bot_id": "1", "conversation_key": conv.key}
    row = latest["sessions"][updater.session_key(identity)]
    settings = dict(row["settings"])
    settings["model"] = {"provider": PLUGIN_SELECTED[0], "model": PLUGIN_SELECTED[1]}
    updater.update_session("qq", "1", conv.key, settings,
                           expected_revision=latest["revision"])
    assert backend._route() == PLUGIN_SELECTED

    # Scheduled work uses the same live group model, even when the legacy
    # conversation_model row still names a removed model.
    scheduled = _load("v2_console_model_sync_scheduled", ROOT / "tests/test_scheduled_agent.py")
    (tmp_path / "scheduled").mkdir(exist_ok=True)
    j, state, dsh, mcp, conv = scheduled.fixture(tmp_path / "scheduled")
    scheduled.append(j, conv, "scheduled-source")
    state.get_or_create("qq", "1", conv.key, "frontend-session")
    state.set_model_route("qq", "1", conv.key, OLD)
    scheduled_console = _configured_store(tmp_path / "scheduled-console.json")
    scheduled_console.create_session("qq", "1", conv.key,
                                     expected_revision=scheduled_console.read()["revision"])
    scheduled_console = ConsoleStore(tmp_path / "scheduled-console.json")
    latest = scheduled_console.read()
    row = latest["sessions"][scheduled_console.session_key({
        "platform_id": "qq", "bot_id": "1", "conversation_key": conv.key,
    })]
    settings = dict(row["settings"])
    settings["model"] = {"provider": PLUGIN_SELECTED[0], "model": "scheduled-current"}
    scheduled_console.update_session("qq", "1", conv.key, settings,
                                     expected_revision=latest["revision"])
    service = scheduled.service_module.ChatService(
        j, state, dsh, mcp, trusted_sources=True, execution_profile="group-sandbox")
    service.console_store = ConsoleStore(tmp_path / "scheduled-console.json")

    def forbidden_legacy_read(*_args):
        raise AssertionError("scheduled console mode read stale conversation_model")

    monkeypatch.setattr(state, "get_model_route", forbidden_legacy_read)
    result = asyncio.run(service.run_scheduled(
        conv, task_id="memory-sync-test", run_id="one", prompt="controlled",
        requester_id="42", source_message_id="scheduled-source"))
    call = next(item for item in dsh.calls if item[0] == "run")
    assert result["status"] == "completed"
    assert call[5] == (PLUGIN_SELECTED[0], "scheduled-current")
