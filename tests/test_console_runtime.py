"""Adapter contract tests using isolated Router/DSH fakes."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "astrbot-plugins" / "v2_console"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parent))
from v2_console.runtime import PartialRuntimeOperation, RuntimeAdapter  # noqa: E402
from v2_console.store import ConsoleStore  # noqa: E402
from v2_console.templates import TemplateService  # noqa: E402


@dataclass
class StateRow:
    platform_id: str
    bot_id: str
    conversation_key: str


@dataclass(frozen=True)
class Owner:
    platform_id: str
    bot_id: str
    user_id: str


class SharedModels:
    def __init__(self, catalog=None):
        self.catalog = catalog or [{"provider": "shared", "models": ["model-a", "model-b"],
                                    "names": ["model-a", "model-b"]}]

    def snapshot(self):
        return "fingerprint", self.catalog


class FakeState:
    def __init__(self, rows=(), routes=None):
        self.rows = list(rows)
        self.routes = dict(routes or {})

    def export_rows(self):
        return list(self.rows)

    def get_model_route(self, *identity):
        return self.routes.get(tuple(identity))

    def get(self, *identity):
        return object() if tuple(identity) in {
            (row.platform_id, row.bot_id, row.conversation_key) for row in self.rows
        } else None


class FakeJournal:
    def __init__(self, high_watermark=731):
        self.value = high_watermark
        self.calls = []

    def high_watermark(self, conversation):
        self.calls.append((conversation.platform_id, conversation.bot_id, conversation.key))
        return self.value


class FakeService:
    def __init__(self, state=None, workspace=None):
        self.state = state or FakeState()
        self.dsh = SimpleNamespace(settings={"provider": "shared", "model": "model-a",
                                             "workspace": str(workspace or "/tmp/owner")},
                                   runtime=SimpleNamespace(shared_models=SharedModels()))
        self._disabled_identities = set()
        self._active_runs = {}
        self.enabled_calls = []
        self.journal = FakeJournal()
        self.cancel_calls = []
        self.enabled_error = None
        self.model_status_calls = 0
        self.reset_calls = []
        self.compact_calls = []
        self.cancel_result = True
        self._locks = {}

    def _lock(self, conversation):
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        return self._locks.setdefault(identity, asyncio.Lock())

    async def set_enabled(self, conversation, enabled):
        self.enabled_calls.append((conversation.platform_id, conversation.bot_id,
                                   conversation.key, enabled))
        if self.enabled_error:
            raise self.enabled_error
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        if enabled:
            self._disabled_identities.discard(identity)
        else:
            self._disabled_identities.add(identity)
            await self.cancel(conversation)
        return enabled

    async def model_status(self, _conversation):
        self.model_status_calls += 1
        return {"current": {"provider": "shared", "model": "model-a"},
                "choices": [{"provider": "shared", "model": "model-a"}]}

    async def cancel(self, conversation):
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        self.cancel_calls.append(identity)
        return self.cancel_result

    async def reset_from_console(self, conversation):
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        self.reset_calls.append(identity)
        return SimpleNamespace(session_id="fresh-session", history_after_journal_id=73)

    async def compact_from_console(self, conversation):
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        self.compact_calls.append(identity)
        return {"compacted": True, "session_id": "existing-session"}


class FakeAstrConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0
        self.fail_save = False

    def save_config(self):
        if self.fail_save:
            raise OSError("failed config save")
        self.save_count += 1


class FakeRouter:
    def __init__(self, owner_service=None):
        self.owners = {Owner(platform_id="qq", bot_id="bot", user_id="42")}
        self.allowed_conversations = frozenset({"private:42"})
        self.services = {"owner": owner_service or FakeService()}
        self.clients = {"owner": self.services["owner"].dsh}
        self._group_runtimes = {}
        self._active = set()
        self._closing = False
        self.created_groups = []

    async def _ensure_group_service(self, platform, bot, group):
        workspace = self.created_groups_root / f"{platform}-{bot}-{group}"
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        service = FakeService(workspace=workspace)
        name = f"group:{group}"
        self.services[name] = service
        self.clients[name] = service.dsh
        self._group_runtimes[name] = {
            "platform_id": platform, "bot_id": bot, "group_id": group,
            "workspace": workspace,
        }
        self.created_groups.append((platform, bot, group))
        return service

    @staticmethod
    def _conversation_type(platform, bot, key):
        return SimpleNamespace(platform_id=platform, bot_id=bot, key=key)


class FakeContext:
    def __init__(self, router):
        self.router = router
        self.config = FakeAstrConfig(platform_settings={"id_whitelist": ["qq:GroupMessage:7"]})

    def get_registered_star(self, name):
        return SimpleNamespace(star_cls=self.router) if name == "v2_dsh_router" else None

    def get_config(self):
        return self.config


def make_adapter(tmp_path, router=None):
    router = router or FakeRouter()
    router.created_groups_root = tmp_path / "group-workspaces"
    context = FakeContext(router)
    repo = tmp_path / "repo"
    runtime_dir = repo / ".runtime"
    runtime_dir.mkdir(parents=True, mode=0o700)
    config = {
        "version": 2, "execution_contract": 2, "provider": "shared", "model": "model-a",
        "allowed_conversations": ["group:7"], "owners": [
            {"platform_id": "qq", "bot_id": "bot", "user_id": "42"}
        ],
    }
    service_config = runtime_dir / "agent-service.json"
    service_config.write_text(json.dumps(config))
    service_config.chmod(0o600)
    return RuntimeAdapter(context, repo), context, router, service_config


def identity_row(platform="qq", bot="bot", key="group:7"):
    return {"identity": {"platform_id": platform, "bot_id": bot, "conversation_key": key}}


def test_discover_imports_only_admitted_owner_and_group_without_inferred_editors(tmp_path):
    owner = FakeService(FakeState([
        StateRow("qq", "bot", "private:42"),
        StateRow("qq", "bot", "private:99"),
    ], {("qq", "bot", "private:42"): ("legacy", "owner-model")}))
    router = FakeRouter(owner)
    group = FakeService(FakeState([
        StateRow("qq", "bot", "group:7"),
        StateRow("qq", "bot", "group:8"),
    ]), workspace=tmp_path / "group")
    router.services["group:stable"] = group
    router._group_runtimes["group:stable"] = {
        "platform_id": "qq", "bot_id": "bot", "group_id": "7",
        "workspace": tmp_path / "group",
    }
    router.allowed_conversations = frozenset({"private:42", "group:7"})
    adapter, _context, _router, _config = make_adapter(tmp_path, router)
    store = ConsoleStore(tmp_path / "console" / "state.json")

    asyncio.run(adapter.discover(store))
    state = store.read()
    identities = {tuple(row["identity"].values()) for row in state["sessions"].values()}
    assert identities == {("qq", "bot", "private:42"), ("qq", "bot", "group:7")}
    owner_row = state["sessions"][store.session_key({
        "platform_id": "qq", "bot_id": "bot", "conversation_key": "private:42"
    })]
    assert owner_row["settings"] == {
        "model": {"provider": "legacy", "model": "owner-model"},
        "enabled": True, "editors": [],
    }
    assert state["sessions"][store.session_key({
        "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:7"
    })]["settings"]["editors"] == []


def test_validate_model_uses_catalog_without_creating_a_session(tmp_path):
    adapter, _context, router, _config = make_adapter(tmp_path)
    asyncio.run(adapter.validate_model({"provider": "shared", "model": "model-b"}))
    with pytest.raises(ValueError, match="共享模型目录"):
        asyncio.run(adapter.validate_model({"provider": "missing", "model": "model-b"}))
    assert router.services["owner"].model_status_calls == 0


def test_update_checks_revision_model_and_persists_before_runtime_enable(tmp_path):
    adapter, _context, router, _config = make_adapter(tmp_path)
    store = ConsoleStore(tmp_path / "console" / "state.json")
    initial = store.read()
    settings = {"model": {"provider": "shared", "model": "model-b"},
                "enabled": True, "editors": ["100"]}
    imported = store.import_session("qq", "bot", "private:42", {
        "model": {"provider": "shared", "model": "model-a"},
        "enabled": False, "editors": [],
    }, expected_revision=initial["revision"])
    router.services["owner"]._disabled_identities.add(("qq", "bot", "private:42"))
    result = asyncio.run(adapter.update(identity_row(key="private:42"), settings,
                                         imported["state"]["revision"], store))
    assert result["revision"] == imported["state"]["revision"] + 1
    assert result["session"]["settings"] == settings
    assert router.services["owner"].enabled_calls == [("qq", "bot", "private:42", True)]
    assert result["session"]["resume_through_journal_id"] == 731
    assert router.services["owner"].journal.calls == [("qq", "bot", "private:42")]


def test_disabled_update_does_not_wait_for_active_turn_lock_before_cancelling(tmp_path):
    adapter, _context, router, _config = make_adapter(tmp_path)
    store = ConsoleStore(tmp_path / "console" / "state.json")
    state = store.import_session("qq", "bot", "private:42", {
        "model": {"provider": "shared", "model": "model-a"},
        "enabled": True, "editors": [],
    }, expected_revision=store.read()["revision"])["state"]
    service = router.services["owner"]
    conversation = router._conversation_type("qq", "bot", "private:42")
    lock = service._lock(conversation)

    async def exercise():
        await lock.acquire()
        try:
            return await asyncio.wait_for(adapter.update(identity_row(key="private:42"), {
                "model": {"provider": "shared", "model": "model-a"},
                "enabled": False, "editors": [],
            }, state["revision"], store), timeout=0.5)
        finally:
            lock.release()

    result = asyncio.run(exercise())
    assert result["session"]["settings"]["enabled"] is False
    assert service.enabled_calls == [("qq", "bot", "private:42", False)]


def test_update_rejects_stale_revision_before_any_runtime_change(tmp_path):
    adapter, _context, router, _config = make_adapter(tmp_path)
    store = ConsoleStore(tmp_path / "console" / "state.json")
    state = store.read()
    imported = store.import_session("qq", "bot", "private:42", {
        "model": {"provider": "shared", "model": "model-a"},
        "enabled": True, "editors": [],
    }, expected_revision=state["revision"])
    store.update_defaults({"model": {"provider": "shared", "model": "model-b"},
                           "enabled": True, "editors": []},
                          expected_revision=imported["state"]["revision"])
    with pytest.raises(Exception, match="revision conflict"):
        asyncio.run(adapter.update(identity_row(key="private:42"), {
            "model": {"provider": "shared", "model": "model-a"},
            "enabled": False, "editors": [],
        }, imported["state"]["revision"], store))
    assert router.services["owner"].enabled_calls == []


def prepare_group_add(tmp_path):
    adapter, context, router, config_path = make_adapter(tmp_path)
    store = ConsoleStore(tmp_path / "console" / "state.json")
    state = store.read()
    state = store.update_defaults({"model": {"provider": "shared", "model": "model-a"},
                                   "enabled": True, "editors": ["42"]},
                                  expected_revision=state["revision"])
    template = store.create_template("New group", expected_revision=state["revision"])
    template_id = template["template"]["id"]
    templates_root = tmp_path / "console" / "templates"
    templates_root.mkdir(mode=0o700)
    TemplateService(templates_root).create_directory(template_id)
    (templates_root / template_id / "AGENTS.md").write_text("group guide")
    return adapter, context, router, store, config_path, templates_root, template_id, template["state"]["revision"]


def test_add_group_syncs_both_admission_gates_and_initializes_selected_template_once(tmp_path):
    adapter, context, router, store, config_path, templates, template_id, revision = prepare_group_add(tmp_path)
    result = asyncio.run(adapter.add_group({
        "platform_id": "qq", "bot_id": "bot", "group_id": "123",
        "template_id": template_id, "expected_revision": revision,
    }, store, templates))
    assert result["added"] is True
    assert result["completed"][-1] == "Router 会话准入已启用"
    assert "group:123" in router.allowed_conversations
    assert "qq:GroupMessage:123" in context.config["platform_settings"]["id_whitelist"]
    assert context.config.save_count == 1
    assert "group:123" in json.loads(config_path.read_text())["allowed_conversations"]
    workspace = Path(result["workspace"])
    assert (workspace / "AGENTS.md").read_text() == "group guide"
    session = store.read()["sessions"][store.session_key({
        "platform_id": "qq", "bot_id": "bot", "conversation_key": "group:123"
    })]
    assert session["settings"]["editors"] == ["42"]
    assert session["template_id"] == template_id


def test_add_group_partial_failure_is_reported_and_agent_stays_unadmitted(tmp_path, monkeypatch):
    adapter, context, router, store, config_path, templates, template_id, revision = prepare_group_add(tmp_path)
    runtime_module = sys.modules[RuntimeAdapter.__module__]
    monkeypatch.setattr(runtime_module, "atomic_json", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(PartialRuntimeOperation) as failure:
        asyncio.run(adapter.add_group({
            "platform_id": "qq", "bot_id": "bot", "group_id": "123",
            "template_id": template_id, "expected_revision": revision,
        }, store, templates))
    assert failure.value.completed == (
        "会话设置已持久化", "群工作区与运行时已准备",
        "工作区模板已安全初始化", "AstrBot 平台会话白名单已同步",
    )
    assert "group:123" not in router.allowed_conversations
    assert "group:123" not in json.loads(config_path.read_text())["allowed_conversations"]
    assert "qq:GroupMessage:123" in context.config["platform_settings"]["id_whitelist"]
    assert store.session_key({"platform_id": "qq", "bot_id": "bot",
                             "conversation_key": "group:123"}) in store.read()["sessions"]


def test_add_group_rejects_identity_not_matching_owner_pair(tmp_path):
    adapter, _context, _router, store, _config, templates, template_id, revision = prepare_group_add(tmp_path)
    with pytest.raises(PermissionError, match="唯一已配置"):
        asyncio.run(adapter.add_group({
            "platform_id": "other", "bot_id": "bot", "group_id": "123",
            "template_id": template_id, "expected_revision": revision,
        }, store, templates))


def test_models_without_existing_session_return_todo_without_creating_one(tmp_path):
    adapter, _context, router, _config = make_adapter(tmp_path)
    result = asyncio.run(adapter.models(identity_row(key="private:42")))
    assert result["available"] is True
    assert result["current"] == {"provider": "shared", "model": "model-a"}
    assert result["choices"] == [
        {"provider": "shared", "model": "model-a", "name": "model-a"},
        {"provider": "shared", "model": "model-b", "name": "model-b"},
    ]
    assert router.services["owner"].model_status_calls == 0


def test_workspace_and_context_actions_validate_identity_and_call_native_service(tmp_path):
    adapter, _context, router, _config = make_adapter(tmp_path)
    owner_workspace = tmp_path / "owner"
    owner_workspace.mkdir()
    router.services["owner"].dsh.settings["workspace"] = str(owner_workspace)
    assert asyncio.run(adapter.workspace(identity_row(key="private:42"))) == owner_workspace
    with pytest.raises(PermissionError):
        asyncio.run(adapter.workspace(identity_row(platform="other", key="private:42")))
    new_result = asyncio.run(adapter.context_action("new", identity_row(key="private:42")))
    compact_result = asyncio.run(adapter.context_action("compact", identity_row(key="private:42")))
    assert new_result == {"available": True, "reset": True, "session_id": "fresh-session",
                         "history_after_journal_id": 73}
    assert compact_result == {"available": True, "compacted": True,
                              "session_id": "existing-session"}
    assert router.services["owner"].reset_calls == [("qq", "bot", "private:42")]
    assert router.services["owner"].compact_calls == [("qq", "bot", "private:42")]


def test_quiesce_closes_router_and_cancels_existing_session_work(tmp_path):
    service = FakeService(FakeState([StateRow("qq", "bot", "private:42")]))
    router = FakeRouter(service)
    adapter, _context, _router, _config = make_adapter(tmp_path, router)
    asyncio.run(adapter.quiesce())
    assert router._closing is True
    assert service.cancel_calls == [("qq", "bot", "private:42")]
    assert service._disabled_identities == {("qq", "bot", "private:42")}


def test_failed_restart_restores_only_sessions_disabled_by_quiesce(tmp_path):
    service = FakeService(FakeState([StateRow("qq", "bot", "private:42")]))
    already_disabled = ("qq", "bot", "private:99")
    service._disabled_identities.add(already_disabled)
    router = FakeRouter(service)
    adapter, _context, _router, _config = make_adapter(tmp_path, router)
    identity = ("qq", "bot", "private:42")
    service._active_runs[identity] = SimpleNamespace(done=asyncio.Event())

    with pytest.raises(RuntimeError, match="未提交重启请求"):
        asyncio.run(adapter.quiesce())
    assert router._closing is True
    assert identity in service._disabled_identities

    asyncio.run(adapter.restart_failed())
    assert router._closing is False
    assert identity not in service._disabled_identities
    assert already_disabled in service._disabled_identities
    assert service.enabled_calls[-1] == ("qq", "bot", "private:42", True)
