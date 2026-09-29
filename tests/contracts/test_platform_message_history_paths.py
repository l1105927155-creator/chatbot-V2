"""Isolated coverage harness for AstrBot v4.28.2 PlatformMessageHistory.

Run against the pinned checkout, for example:

    ASTRBOT_SOURCE=.runtime/astrbot ASTRBOT_ROOT=.runtime/astrbot \
      uv run --with pytest --with pytest-asyncio \
      --with-requirements .runtime/astrbot/requirements.txt \
      pytest -q \
      tests/contracts/test_platform_message_history_paths.py

The harness deliberately uses fake transports.  A passing test demonstrates
AstrBot's local persistence behavior; it does not demonstrate a QQ delivery.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


SOURCE = Path(
    os.environ.get("ASTRBOT_SOURCE", Path(__file__).parents[2] / ".runtime/astrbot")
).resolve()
if not (SOURCE / "astrbot").is_dir():
    pytest.skip(
        "Set ASTRBOT_SOURCE to the pinned AstrBot checkout (or bootstrap .runtime/astrbot).",
        allow_module_level=True,
    )
# AstrBot imports create data files.  Keep them with the ignored pinned runtime,
# never under the V2 checkout from which pytest was invoked.
os.environ["ASTRBOT_ROOT"] = str(SOURCE)
sys.path.insert(0, str(SOURCE))

from astrbot.api.message_components import At, Plain  # noqa: E402
from astrbot.api.provider import LLMResponse  # noqa: E402
from astrbot.builtin_stars.astrbot import main as builtin_main_module  # noqa: E402
from astrbot.builtin_stars.astrbot.main import Main  # noqa: E402
from astrbot.core.db.sqlite import SQLiteDatabase  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform_message_history_mgr import PlatformMessageHistoryManager  # noqa: E402
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (  # noqa: E402
    AiocqhttpMessageEvent,
)
# Import the parent stage first: v4.28.2's substage module and parent module
# import one another, and the production import order starts at the parent.
from astrbot.core.pipeline.process_stage.stage import ProcessStage  # noqa: F401, E402
from astrbot.core.pipeline.process_stage.method import star_request as star_request_module  # noqa: E402
from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402


GROUP_UMO = "aiocqhttp:GroupMessage:member_100_group_200"
PRIVATE_UMO = "aiocqhttp:FriendMessage:member_100"


class Session:
    def __init__(self, umo: str, message_type: MessageType, platform_name: str = "aiocqhttp"):
        self.umo = umo
        self.message_type = message_type
        self.platform_name = platform_name
        self.platform_id = "aiocqhttp"

    def __str__(self) -> str:
        return self.umo


class FakePlatform:
    def __init__(self):
        self.send_by_session = AsyncMock()

    def meta(self):
        return SimpleNamespace(id="aiocqhttp", name="aiocqhttp")


def settings(enabled: bool) -> dict:
    return {
        "provider_ltm_settings": {
            "group_message_history_enable": enabled,
            "group_message_history_max_cnt": 20,
        }
    }


def event(umo: str, message_type: MessageType) -> MagicMock:
    result = MagicMock()
    result.unified_msg_origin = umo
    result.get_message_type.return_value = message_type
    result.get_platform_name.return_value = "aiocqhttp"
    result.get_platform_id.return_value = "aiocqhttp"
    result.get_sender_id.return_value = "100"
    result.get_sender_name.return_value = "Alice"
    result.get_self_id.return_value = "999"
    result.get_messages.return_value = [Plain("payload")]
    result.set_extra = MagicMock()
    return result


async def rows(manager: PlatformMessageHistoryManager, umo: str):
    return await manager.get("aiocqhttp", umo)


@pytest.mark.asyncio
async def test_group_inbound_records_sender_role_and_umo(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    main = Main.__new__(Main)
    main.context = SimpleNamespace(
        get_config=lambda umo: settings(True), message_history_manager=manager
    )
    incoming = event(GROUP_UMO, MessageType.GROUP_MESSAGE)
    try:
        await main.persist_group_message(incoming)
        [row] = await rows(manager, GROUP_UMO)
        assert row.content == {"type": "user", "message": [{"type": "plain", "text": "payload"}]}
        assert (row.platform_id, row.user_id, row.sender_id, row.sender_name) == (
            "aiocqhttp", GROUP_UMO, "100", "Alice"
        )
        assert incoming.set_extra.called
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_group_llm_response_records_bot_role_without_platform_message_id(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    main = Main.__new__(Main)
    main.context = SimpleNamespace(
        get_config=lambda umo: settings(True), message_history_manager=manager
    )
    try:
        await main.persist_llm_response(
            event(GROUP_UMO, MessageType.GROUP_MESSAGE),
            LLMResponse(role="assistant", result_chain=MessageChain([Plain("llm output")])),
        )
        [row] = await rows(manager, GROUP_UMO)
        assert row.content["type"] == "bot"
        assert row.content["message"] == [{"type": "plain", "text": "llm output"}]
        assert (row.sender_id, row.sender_name) == ("999", "bot")
        # PlatformMessageHistory has an internal primary key only, no QQ/OneBot message id.
        assert not hasattr(row, "message_id")
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_context_send_records_only_enabled_group_messages(tmp_path, enabled):
    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    platform = FakePlatform()
    context = Context.__new__(Context)
    context.platform_manager = SimpleNamespace(platform_insts=[platform])
    context.get_config = lambda umo: settings(enabled)
    context.message_history_manager = manager
    try:
        ok = await context.send_message(
            Session(GROUP_UMO, MessageType.GROUP_MESSAGE), MessageChain([Plain("proactive")])
        )
        assert ok is True
        platform.send_by_session.assert_awaited_once()
        stored = await rows(manager, GROUP_UMO)
        assert len(stored) == int(enabled)
        if enabled:
            assert stored[0].content["type"] == "bot"
            assert (stored[0].sender_id, stored[0].sender_name) == ("bot", "bot")
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_private_inbound_and_proactive_send_do_not_record(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    main = Main.__new__(Main)
    main.context = SimpleNamespace(
        get_config=lambda umo: settings(True), message_history_manager=manager
    )
    platform = FakePlatform()
    context = Context.__new__(Context)
    context.platform_manager = SimpleNamespace(platform_insts=[platform])
    context.get_config = lambda umo: settings(True)
    context.message_history_manager = manager
    try:
        await main.persist_group_message(event(PRIVATE_UMO, MessageType.FRIEND_MESSAGE))
        await context.send_message(
            Session(PRIVATE_UMO, MessageType.FRIEND_MESSAGE), MessageChain([Plain("private proactive")])
        )
        assert await rows(manager, PRIVATE_UMO) == []
        platform.send_by_session.assert_awaited_once()
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_empty_mention_stop_skips_lower_priority_group_inbound_persistence(
    tmp_path, monkeypatch
):
    """Reproduce the dispatch consequence of the builtin empty-mention order.

    The actual builtin registrations establish the priorities.  The actual
    builtin handler runs through AstrBot's real StarRequestSubStage before the
    actual ``persist_group_message`` method, with SQLite proving that the
    latter was skipped. A test-only waiter avoids its 60-second pause and LLM.
    """
    empty = star_handlers_registry.get_handler_by_full_name(
        "astrbot.builtin_stars.astrbot.main_handle_empty_mention"
    )
    persist = star_handlers_registry.get_handler_by_full_name(
        "astrbot.builtin_stars.astrbot.main_persist_group_message"
    )
    assert empty is not None and persist is not None
    assert empty.extras_configs["priority"] > persist.extras_configs["priority"]

    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    main = Main.__new__(Main)
    config = settings(True)
    config["platform_settings"] = {
        "empty_mention_waiting": True,
        "empty_mention_waiting_need_reply": False,
    }
    main.context = SimpleNamespace(
        get_config=lambda umo: config, message_history_manager=manager
    )

    def immediate_session_waiter(_timeout):
        def decorate(waiter):
            async def invoke(event):
                return await waiter(SimpleNamespace(stop=lambda: None), event)

            return invoke

        return decorate

    monkeypatch.setattr(builtin_main_module, "session_waiter", immediate_session_waiter)

    class PipelineEvent:
        def __init__(self):
            self.stopped = False
            self.unified_msg_origin = GROUP_UMO
            self._extras = {}
            self.message_str = ""
            self.message_obj = SimpleNamespace(message=[At(qq="999", name="999")])

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

        def is_stopped(self):
            return self.stopped

        def stop_event(self):
            self.stopped = True

        def clear_result(self):
            return None

        def get_message_type(self):
            return MessageType.GROUP_MESSAGE

        def get_platform_name(self):
            return "aiocqhttp"

        def get_platform_id(self):
            return "aiocqhttp"

        def get_sender_id(self):
            return "100"

        def get_sender_name(self):
            return "Alice"

        def get_messages(self):
            return self.message_obj.message

        def get_self_id(self):
            return "999"

    event = PipelineEvent()
    module_path = "phase1.empty-mention"
    event.set_extra(
        "activated_handlers",
        [
            SimpleNamespace(
                handler_full_name="empty", handler_module_path=module_path,
                handler_name="handle_empty_mention",
                handler=main.handle_empty_mention,
            ),
            SimpleNamespace(
                handler_full_name="persist", handler_module_path=module_path,
                handler_name="persist_group_message", handler=main.persist_group_message,
            ),
        ],
    )
    monkeypatch.setattr(
        star_request_module, "star_map", {module_path: SimpleNamespace(name="phase1")}
    )
    try:
        async for _ in StarRequestSubStage().process(event):
            pass
        assert event.is_stopped()
        assert await rows(manager, GROUP_UMO) == []
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_disabling_empty_mention_waiting_allows_group_inbound_persistence(tmp_path):
    """The special handler returns without stopping when its setting is false."""
    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    main = Main.__new__(Main)
    config = settings(True)
    config["platform_settings"] = {"empty_mention_waiting": False}
    main.context = SimpleNamespace(
        get_config=lambda umo: config, message_history_manager=manager
    )
    incoming = event(GROUP_UMO, MessageType.GROUP_MESSAGE)
    incoming.get_messages.return_value = [At(qq="999", name="999")]
    incoming.get_self_id.return_value = "999"
    incoming.message_obj = SimpleNamespace(message=list(incoming.get_messages()))
    incoming.stop_event = MagicMock()
    try:
        assert [item async for item in main.handle_empty_mention(incoming)] == []
        incoming.stop_event.assert_not_called()
        await main.persist_group_message(incoming)
        [row] = await rows(manager, GROUP_UMO)
        assert row.content["type"] == "user"
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_wake_prefix_only_still_stops_when_empty_mention_waiting_is_disabled():
    """The wake-prefix branch is independent of empty_mention_waiting."""
    main = Main.__new__(Main)
    config = settings(True)
    config["platform_settings"] = {
        "empty_mention_waiting": False,
        "empty_mention_waiting_need_reply": False,
    }
    config["wake_prefix"] = ["/"]
    main.context = SimpleNamespace(get_config=lambda umo: config)

    def immediate_session_waiter(_timeout):
        def decorate(waiter):
            async def invoke(event):
                return await waiter(SimpleNamespace(stop=lambda: None), event)

            return invoke

        return decorate

    incoming = event(GROUP_UMO, MessageType.GROUP_MESSAGE)
    incoming.get_messages.return_value = [Plain("/")]
    incoming.message_str = ""
    incoming.message_obj = SimpleNamespace(message=list(incoming.get_messages()))
    incoming.stop_event = MagicMock()

    from unittest.mock import patch

    with patch.object(builtin_main_module, "session_waiter", immediate_session_waiter):
        assert [item async for item in main.handle_empty_mention(incoming)] == []
    incoming.stop_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_group", "umo", "send_method"),
    [
        (True, GROUP_UMO, "send_group_msg"),
        (False, PRIVATE_UMO, "send_private_msg"),
    ],
)
async def test_plugin_style_aiocqhttp_response_sends_but_has_no_history_write(
    tmp_path, is_group, umo, send_method
):
    """Exercise the response transport used by a plugin result without QQ.

    Plugin results are delivered by ``event.send``; the aiocqhttp event's
    transport has no history-manager dependency.  This isolates that concrete
    output path and verifies that its output alone leaves the history DB empty.
    """
    db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
    manager = PlatformMessageHistoryManager(db)
    bot = SimpleNamespace(send_group_msg=AsyncMock(), send_private_msg=AsyncMock())
    try:
        await AiocqhttpMessageEvent.send_message(
            bot=bot,
            message_chain=MessageChain([Plain("plugin response")]),
            is_group=is_group,
            session_id="200" if is_group else "100",
        )
        getattr(bot, send_method).assert_awaited_once()
        assert await rows(manager, umo) == []
    finally:
        await db.engine.dispose()


def test_message_chain_normalization_loses_media_payloads(tmp_path):
    """Runs the real serializer through SQLite to make loss explicit."""
    from astrbot.core.message.components import Image

    async def check():
        db = SQLiteDatabase(str(tmp_path / "history.sqlite"))
        manager = PlatformMessageHistoryManager(db)
        try:
            await manager.insert_message_chain(
                "aiocqhttp", GROUP_UMO,
                MessageChain([Plain("kept"), Image(file="file:///private/image.png")]),
                "user", "100", "Alice", 20,
            )
            [row] = await rows(manager, GROUP_UMO)
            assert row.content["message"] == [
                {"type": "plain", "text": "kept"},
                {"type": "plain", "text": "[Image]"},
            ]
        finally:
            await db.engine.dispose()

    asyncio.run(check())
