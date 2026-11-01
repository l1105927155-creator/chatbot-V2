"""Routing must silence ignored group members before a DSH turn can start."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT = ROOT / ".runtime/astrbot"
for path in (ASTRBOT, ASTRBOT / "data", ASTRBOT / "data/plugins", ROOT / "astrbot-plugins"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
# Import the repository package directly. ``data.plugins.v2_dsh_router`` may be
# the deployed release snapshot, which is a different set of files.
main = pytest.importorskip("v2_dsh_router.main")

GROUP = "1055300305"
PLATFORM = "qq"
BOT = "900001"
MEMBER = "1105927155"
GUEST = "2205927155"


class Volume:
    """Stand-in for the real group volume module's verified workspace open."""

    @staticmethod
    def verified_group_workspace(workspace):
        return os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


class Service:
    def __init__(self):
        self.handled = []

    async def handle(self, *args, **kwargs):
        self.handled.append(args)


class Event:
    def __init__(self, raw, *, components=()):
        self.message_obj = SimpleNamespace(raw_message=raw, message=components)
        self.bot = None

    @staticmethod
    def get_platform_id():
        return PLATFORM

    @staticmethod
    def get_platform_name():
        return "aiocqhttp"

    @staticmethod
    def is_stopped():
        return False

    @staticmethod
    def get_extra(key, default=None):
        return default

    @staticmethod
    def get_group():
        return None


def raw_message(actor=MEMBER, *, text="你好", mention=True, message_id=77):
    return {
        "post_type": "message", "message_type": "group", "self_id": int(BOT),
        "user_id": int(actor), "group_id": int(GROUP), "message_id": message_id,
        "message": ([{"type": "at", "data": {"qq": BOT}}] if mention else [])
                   + [{"type": "text", "data": {"text": text}}],
    }


def workspace(tmp_path, entries):
    root = tmp_path / "workspace"
    directory = root / "apps/blacklist/data"
    directory.mkdir(parents=True)
    (directory / "members.json").write_text(json.dumps(
        {"version": 1, "entries": {qq: {"qq": qq, "scope": scope}
                                   for qq, scope in entries.items()}}), encoding="utf-8")
    return root


def router_for(root):
    """Build only the attributes route_chat touches up to service selection."""
    router = object.__new__(main.V2DshRouter)
    router._initialized = True
    router._closing = False
    router._active = set()
    router.logger = SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None,
                                    warning=lambda *a, **k: None)
    router.owners = frozenset()
    router.console_store = None
    router.allowed_conversations = {"group:" + GROUP}
    router.services = {}
    router._group_runtimes = {}
    name = "group:" + GROUP
    # A prepared runtime, as after the group's first admitted message.
    router.services[name] = SimpleNamespace()
    router._group_runtimes[name] = {"platform_id": PLATFORM, "bot_id": BOT, "group_id": GROUP,
                                    "workspace": root, "volume_module": Volume,
                                    "volume_key": GROUP}
    calls = []

    async def service_for(message):
        # Reaching selection is the whole question here; stop the turn there so
        # this test needs no DSH client, journal, or QQ sender.
        calls.append(message.conversation_key)
        raise main.AgentDisabledError("test stops at service selection")

    router._service_for = service_for
    return router, calls


def route(router, raw=None, *, components=()):
    return asyncio.run(router.route_chat(Event(raw or raw_message(), components=components)))


def test_listed_member_never_reaches_service_selection(tmp_path):
    router, calls = router_for(workspace(tmp_path, {MEMBER: "all"}))
    route(router)
    assert calls == []


def test_unlisted_member_still_routes(tmp_path):
    router, calls = router_for(workspace(tmp_path, {MEMBER: "all"}))
    route(router, raw_message(GUEST))
    assert calls == ["group:" + GROUP]


def test_mention_only_member_routes_only_when_addressing_the_bot(tmp_path):
    router, calls = router_for(workspace(tmp_path, {MEMBER: "mention_only"}))
    # No real mention: the gate keeps the message silent.
    route(router, raw_message(MEMBER, mention=False))
    assert calls == []
    route(router, raw_message(MEMBER, mention=True, message_id=78))
    assert calls == ["group:" + GROUP]


def test_broken_list_keeps_ordinary_routing(tmp_path):
    root = workspace(tmp_path, {MEMBER: "all"})
    (root / "apps/blacklist/data/members.json").write_text("{not json", encoding="utf-8")
    router, calls = router_for(root)
    route(router)
    assert calls == ["group:" + GROUP]


def test_missing_group_runtime_keeps_ordinary_routing(tmp_path):
    router, calls = router_for(workspace(tmp_path, {MEMBER: "all"}))
    router._group_runtimes.clear()
    route(router)
    assert calls == ["group:" + GROUP]


def test_gate_failure_keeps_ordinary_routing(tmp_path, monkeypatch):
    router, calls = router_for(workspace(tmp_path, {MEMBER: "all"}))

    def broken(*_args, **_kwargs):
        raise RuntimeError("ignore list reader unavailable")

    monkeypatch.setattr(main.SilenceGate, "decide", broken)
    route(router)
    assert calls == ["group:" + GROUP]
