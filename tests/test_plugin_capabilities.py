from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))

from plugin_capabilities import PluginCapabilities, PluginCapabilityError


class FakeProxy:
    name = "bili_get_user_dynamics"
    description = "fixture dynamics query"
    parameters = {"type": "object", "properties": {}}
    active = True

    def __init__(self):
        self.calls = []

    async def call(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return "native guarded dynamics result"


class FakePlugin:
    def __init__(self, plugin_id, instance):
        self.name = plugin_id
        self.activated = True
        self.star_cls = instance
        self.star_handler_full_names = []


class FakeContext:
    def __init__(self, plugins, proxy=None):
        self.plugins = plugins
        self.proxy = proxy
        self.manager_calls = 0

    def get_registered_star(self, name):
        return self.plugins.get(name)

    def get_all_stars(self):
        return list(self.plugins.values())

    def get_llm_tool_manager(self):
        self.manager_calls += 1
        return SimpleNamespace(get_full_tool_set=lambda: SimpleNamespace(tools=[self.proxy]))


class FakeBili:
    def __init__(self):
        self.calls = []
        self.bili_client = SimpleNamespace(get_user_info=self.get_user_info)
        self.subscription_service = SimpleNamespace(add_or_update=self.add_or_update)
        self.data_manager = SimpleNamespace(
            get_subscriptions_by_user=self.get_subscriptions_by_user,
            remove_subscription=self.remove_subscription,
        )

    async def get_user_info(self, uid):
        self.calls.append(("user", uid))
        return {"name": "example-up"}, None

    async def add_or_update(self, umo, uid, types, regex, live_atall):
        self.calls.append(("subscribe", umo, uid, types, regex, live_atall))
        return SimpleNamespace(updated=False, initialized=True)

    def get_subscriptions_by_user(self, umo):
        self.calls.append(("list", umo))
        return [SimpleNamespace(uid=123, filter_types=["video"], filter_regex=[], live_atall=False)]

    async def remove_subscription(self, umo, uid):
        self.calls.append(("remove", umo, uid))
        return True


class FakeTwitter:
    def __init__(self):
        self.subscriptions = SimpleNamespace(
            add=self.add,
            get_snapshot=self.get_snapshot,
            remove=self.remove,
        )
        self.calls = []

    async def add(self, umo, name):
        self.calls.append(("add", umo, name))
        return {"ok": True, "username": name, "secret": "must-not-escape"}

    async def get_snapshot(self):
        return {
            "writer": {
                "screen_name": "Writer",
                "api_token": "never-return",
                "subscribers": {"qq:Group:77": {"status": True, "media": False},
                                "qq:Group:88": {"status": False}},
            }
        }

    async def remove(self, umo, name):
        self.calls.append(("remove", umo, name))
        return {"ok": True, "username": name, "secret": "must-not-escape"}


def make_caps():
    event = SimpleNamespace(unified_msg_origin="qq:Group:77")
    bili, twitter, proxy = FakeBili(), FakeTwitter(), FakeProxy()
    context = FakeContext({}, proxy)
    context.plugins.update({
        "astrbot_plugin_bilibili": FakePlugin("astrbot_plugin_bilibili", bili),
        "astrbot_plugin_twitter": FakePlugin("astrbot_plugin_twitter", SimpleNamespace(
            subscription_service=twitter.subscriptions, _provider_ready=True)),
    })
    for plugin_id, metadata in context.plugins.items():
        metadata.name = "Bilibili" if plugin_id == "astrbot_plugin_bilibili" else "Twitter"
        metadata.root_dir_name = plugin_id
        metadata.module_path = plugin_id + ".main"
    return PluginCapabilities(context, event), context, bili, twitter, proxy


def permit_session_plugins(monkeypatch, *, disabled=()):
    from astrbot.core.star.session_plugin_manager import SessionPluginManager

    calls = []

    async def enabled(umo, plugin_name):
        calls.append((umo, plugin_name))
        return plugin_name not in disabled

    monkeypatch.setattr(SessionPluginManager, "is_plugin_enabled_for_session", staticmethod(enabled))
    return calls


def authorize_test_commands(monkeypatch, context, *, disabled=(), db_disabled=(), admin_only=()):
    permit_session_plugins(monkeypatch)
    names = {
        "astrbot_plugin_bilibili": ("dynamic_sub", "sub_list", "sub_del"),
        "astrbot_plugin_twitter": ("follow_twitter", "list_follows", "unfollow_twitter"),
    }
    handlers = {}
    for plugin_id, methods in names.items():
        metadata = context.plugins[plugin_id]
        for method in methods:
            full_name = f"test_{plugin_id}_{method}"
            metadata.star_handler_full_names.append(full_name)
            handlers[full_name] = SimpleNamespace(
                handler_full_name=full_name,
                handler_name=method,
                enabled=method not in disabled,
                event_filters=[],
            )
            if method in admin_only:
                from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
                handlers[full_name].event_filters = [PermissionTypeFilter(PermissionType.ADMIN)]

    from astrbot.core import db_helper
    import astrbot.core.star.star_handler as star_handler

    monkeypatch.setattr(star_handler, "star_handlers_registry", SimpleNamespace(
        get_handler_by_full_name=lambda name: handlers.get(name)
    ))

    async def command_config(full_name):
        if any(full_name.endswith("_" + method) for method in db_disabled):
            return SimpleNamespace(enabled=False)
        return None

    monkeypatch.setattr(db_helper, "get_command_config", command_config)


def test_bilibili_dynamics_uses_native_tool_proxy_and_current_event(monkeypatch):
    caps, context, _, _, proxy = make_caps()
    permit_session_plugins(monkeypatch)
    # Preserve the exact AstrBot wrapper call shape while allowing lightweight
    # stand-ins for its runtime-only Context and AstrMessageEvent classes.
    class Wrapper:
        def __init__(self, context):
            self.context = context

    class AgentContext:
        def __init__(self, *, context, event):
            self.context, self.event = context, event

    monkeypatch.setitem(sys.modules, "astrbot.core.astr_agent_context",
                        SimpleNamespace(AstrAgentContext=AgentContext))
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.run_context",
                        SimpleNamespace(ContextWrapper=Wrapper))
    assert asyncio.run(caps.bili_dynamics(123, 4)) == "native guarded dynamics result"
    assert context.manager_calls == 1
    assert proxy.calls[0][0].context.event is caps.event
    assert proxy.calls[0][1] == {"uid": 123, "limit": 4}


def test_bilibili_dynamics_respects_astrbot_permission_proxy(monkeypatch):
    from astrbot.core.provider.func_tool_manager import _PermissionGuardedTool

    caps, context, _, _, raw_tool = make_caps()
    permit_session_plugins(monkeypatch)

    async def deny_permission(_name, _context):
        return "error: Permission denied by AstrBot tool policy"

    guarded = _PermissionGuardedTool(raw_tool, SimpleNamespace(
        _check_tool_permission=deny_permission
    ))
    context.proxy = guarded

    class Wrapper:
        def __init__(self, context):
            self.context = context

    class AgentContext:
        def __init__(self, *, context, event):
            self.context, self.event = context, event

    monkeypatch.setitem(sys.modules, "astrbot.core.astr_agent_context",
                        SimpleNamespace(AstrAgentContext=AgentContext))
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.run_context",
                        SimpleNamespace(ContextWrapper=Wrapper, TContext=object))
    result = asyncio.run(caps.bili_dynamics(123))
    assert "Permission denied" in result
    assert raw_tool.calls == []


def test_bilibili_subscription_operations_are_scoped_to_current_umo(monkeypatch):
    caps, _, bili, _, _ = make_caps()
    authorize_test_commands(monkeypatch, caps.context)
    assert asyncio.run(caps.bili_subscribe(123))["ok"] is True
    assert asyncio.run(caps.bili_subscriptions())[0]["uid"] == 123
    assert asyncio.run(caps.bili_unsubscribe(123))["ok"] is True
    assert bili.calls == [
        ("user", 123),
        ("subscribe", "qq:Group:77", 123, [], [], False),
        ("list", "qq:Group:77"),
        ("remove", "qq:Group:77", 123),
    ]


def test_twitter_operations_filter_other_conversations_and_sensitive_fields(monkeypatch):
    caps, _, _, twitter, _ = make_caps()
    authorize_test_commands(monkeypatch, caps.context)
    assert asyncio.run(caps.twitter_subscribe("@writer")) == {"ok": True, "username": "writer"}
    rows = asyncio.run(caps.twitter_subscriptions())
    assert rows == [{"username": "writer", "screen_name": "Writer", "enabled": True, "media_only": False}]
    assert asyncio.run(caps.twitter_unsubscribe("writer")) == {"ok": True, "username": "writer"}
    assert twitter.calls == [("add", "qq:Group:77", "writer"), ("remove", "qq:Group:77", "writer")]


def test_no_foreign_target_or_arbitrary_subscription_options_are_accepted(monkeypatch):
    caps, _, _, _, _ = make_caps()
    authorize_test_commands(monkeypatch, caps.context)
    with pytest.raises(TypeError):
        asyncio.run(caps.twitter_subscribe("writer", "qq:Group:88"))
    with pytest.raises(PluginCapabilityError):
        asyncio.run(caps.bili_dynamics(True, 3))
    with pytest.raises(PluginCapabilityError):
        asyncio.run(caps.twitter_subscribe("writer other"))


def test_disabled_plugin_is_reported_and_rejected(monkeypatch):
    caps, context, _, _, _ = make_caps()
    permit_session_plugins(monkeypatch)
    context.plugins["astrbot_plugin_twitter"].activated = False
    assert asyncio.run(caps.status()) == {
        "bilibili_enabled": True,
        "bilibili_login_configured": False,
        "twitter_enabled": False,
        "twitter_provider_ready": False,
    }
    with pytest.raises(PluginCapabilityError):
        asyncio.run(caps.twitter_subscriptions())


def test_disabled_native_command_is_respected(monkeypatch):
    caps, _, _, _, _ = make_caps()
    authorize_test_commands(monkeypatch, caps.context, disabled=("follow_twitter",))
    with pytest.raises(PluginCapabilityError, match="停用"):
        asyncio.run(caps.twitter_subscribe("writer"))


@pytest.mark.parametrize("disable_mode", ["runtime", "webui"])
def test_native_command_disabled_in_runtime_or_webui_is_respected(monkeypatch, disable_mode):
    caps, _, _, _, _ = make_caps()
    authorize_test_commands(
        monkeypatch,
        caps.context,
        disabled=("follow_twitter",) if disable_mode == "runtime" else (),
        db_disabled=("follow_twitter",) if disable_mode == "webui" else (),
    )
    with pytest.raises(PluginCapabilityError, match="停用"):
        asyncio.run(caps.twitter_subscribe("writer"))


def test_native_command_permission_filter_is_checked(monkeypatch):
    caps, _, _, _, _ = make_caps()
    authorize_test_commands(monkeypatch, caps.context, admin_only=("follow_twitter",))
    caps.event.is_admin = lambda: False
    with pytest.raises(PluginCapabilityError, match="没有使用"):
        asyncio.run(caps.twitter_subscribe("writer"))


def test_event_plugin_allowlist_and_native_session_disable_use_metadata_name(monkeypatch):
    caps, _, _, _, _ = make_caps()
    event = caps.event
    event.plugins_name = ["Twitter"]
    calls = permit_session_plugins(monkeypatch)
    assert asyncio.run(caps.status()) == {
        "bilibili_enabled": False,
        "bilibili_login_configured": False,
        "twitter_enabled": True,
        "twitter_provider_ready": True,
    }
    assert calls == [("qq:Group:77", "Twitter")]

    event.plugins_name = None
    calls.clear()
    permit_session_plugins(monkeypatch, disabled=("Twitter",))
    with pytest.raises(PluginCapabilityError, match="未安装或未启用"):
        asyncio.run(caps.twitter_subscribe("writer"))


def test_native_metadata_lookup_does_not_assume_display_name_is_directory_id(monkeypatch):
    caps, context, _, _, _ = make_caps()
    authorize_test_commands(monkeypatch, context)
    context.get_registered_star = lambda _name: None
    assert asyncio.run(caps.twitter_subscriptions()) == [
        {"username": "writer", "screen_name": "Writer", "enabled": True, "media_only": False}
    ]
