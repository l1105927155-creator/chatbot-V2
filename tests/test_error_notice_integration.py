"""End-to-end custom notices through workspace storage and AstrBot chains."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / ".runtime/astrbot"),
    str(ROOT / "astrbot-plugins"),
    str(ROOT / "astrbot-plugins/v2_console"),
]

from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.message_components import Plain  # noqa: E402
from files import WorkspaceFiles  # noqa: E402
from v2_dsh_router import delivery_error, turn_error  # noqa: E402
from v2_dsh_router.main import V2DshRouter  # noqa: E402
from v2_dsh_router.trust_domain import Owner  # noqa: E402
from v2_dsh_router.error_notices import FILE_NAME  # noqa: E402
from v2_dsh_router.service import RecoveryError, ResetError  # noqa: E402


def _text(chain):
    assert isinstance(chain, MessageChain)
    return "".join(segment.text for segment in chain.chain if isinstance(segment, Plain))


def _router(workspace):
    router = object.__new__(V2DshRouter)
    router.owners = {Owner("qq", "bot", "42")}
    router.services = {"owner": NS(dsh=NS(settings={"workspace": str(workspace)}))}
    router.logger = logging.getLogger("test.error-notices")
    return router


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_atomic_workspace_edits_apply_to_next_turn_notice_and_delete_restores_default(workspace):
    async def run():
        router = _router(workspace)
        conversation = NS(platform_id="qq", bot_id="bot", key="private:42")
        sent = []

        async def send(chain):
            sent.append(chain)

        async def render(current, code, default):
            return await router.error_notice(current, code, default)

        event = NS(send=send)
        with WorkspaceFiles(workspace) as files:
            files.write(FILE_NAME, json.dumps({
                "MODEL_TIMEOUT": "已执行工具结果未知的完整自定义提示",
            }, ensure_ascii=False).encode(), None)
            await turn_error.notify_turn_error(
                event, conversation, (), "model", logging.getLogger(__name__),
                code=" timeout ", tools_completed=True, render=render,
            )
            assert _text(sent[-1]) == "已执行工具结果未知的完整自定义提示"
            assert "工具不会自动重跑" not in _text(sent[-1])

            # A later atomic write is read on the following notification.
            current = files.read(FILE_NAME)
            files.write(FILE_NAME, json.dumps({
                "TURN_FAILED": "第二次通知的新文案",
            }, ensure_ascii=False).encode(), current["version"])
            await turn_error.notify_turn_error(
                event, conversation, (), "failed", logging.getLogger(__name__), render=render,
            )
            assert _text(sent[-1]) == "第二次通知的新文案"

            # An unknown provider code normalizes to MODEL_ERROR, which has no
            # configured override and therefore retains the stock safe notice.
            await turn_error.notify_turn_error(
                event, conversation, (), "model", logging.getLogger(__name__),
                code="provider said secret", render=render,
            )
            assert "模型接口暂时未能完成请求" in _text(sent[-1])
            assert "secret" not in _text(sent[-1])

            # Removing the file takes effect immediately and restores TURN_FAILED.
            current = files.read(FILE_NAME)
            files.delete(FILE_NAME, current["version"])
            await turn_error.notify_turn_error(
                event, conversation, (), "failed", logging.getLogger(__name__), render=render,
            )
            assert _text(sent[-1]).startswith("这次处理没有完成")
        assert len(sent) == 4

    asyncio.run(run())


def test_delivery_error_uses_full_override_and_send_failure_is_once_and_swallowed(workspace):
    async def run():
        router = _router(workspace)
        conversation = NS(platform_id="qq", bot_id="bot", key="private:42")
        attempts = []

        async def render(current, code, default):
            return await router.error_notice(current, code, default)

        with WorkspaceFiles(workspace) as files:
            files.write(FILE_NAME, json.dumps({
                "DELIVERY_FAILED": "交付失败的完整自定义提示",
            }, ensure_ascii=False).encode(), None)

            async def good_send(chain):
                attempts.append("ok")
                assert _text(chain) == "交付失败的完整自定义提示"

            await delivery_error.notify_delivery_error(
                NS(send=good_send), conversation, (), RuntimeError("private secret"),
                logging.getLogger(__name__), render=render,
            )
            assert attempts == ["ok"]

            async def failing_send(_chain):
                attempts.append("failed")
                raise OSError("private transport detail")

            # A known default path also stays best-effort when AstrBot send fails.
            current = files.read(FILE_NAME)
            files.delete(FILE_NAME, current["version"])
            await delivery_error.notify_delivery_error(
                NS(send=failing_send), conversation, (), RuntimeError("private secret"),
                logging.getLogger(__name__), render=render,
            )
            assert attempts == ["ok", "failed"]

    asyncio.run(run())


@pytest.mark.parametrize(("action", "code"), [
    ("new", "RESET_FAILED"),
    ("provider", "MODEL_SELECTION_FAILED"),
    ("recover", "RECOVERY_FAILED"),
])
def test_command_failures_use_full_workspace_notice_and_release_activity(workspace, action, code):
    async def run():
        router = _router(workspace)
        router._closing = False
        router._initialized = router._commands_ready = True
        router._active = set()
        router.allowed_conversations = {"private:42"}
        router.onebot_audit = NS(bind=lambda *_: None)
        async def trace_send(sender, text, *_):
            return await sender(text)
        router.audit = NS(trace=lambda *_: NS(emit=lambda *a, **k: None, send=trace_send))
        service = router.services["owner"]
        def begin(*_):
            raise ResetError("原会话保持不变")
        async def model_status(*_, **__):
            raise OSError("SECRET_MODEL_CONFIG")
        async def resolve(*_, **__):
            raise RecoveryError("编号已变化，不能恢复")
        service.begin_reset = begin
        service.model_status = model_status
        service.resolve_pending = resolve
        sent, stopped = [], []
        async def send(chain): sent.append(_text(chain))
        event = NS(
            set_extra=lambda *_: None,
            message_obj=NS(raw_message={
                "post_type": "message", "message_type": "private", "self_id": "bot",
                "user_id": "42", "message_id": "command-1",
                "message": [{"type": "text", "data": {"text": "/" + action}}],
            }),
            get_platform_id=lambda: "qq", get_platform_name=lambda: "aiocqhttp",
            get_group=lambda: None, stop_event=lambda: stopped.append(True), send=send,
        )
        with WorkspaceFiles(workspace) as files:
            files.write(FILE_NAME, json.dumps({code: "完整命令错误文案"}).encode(), None)
        if action == "recover":
            await router.configure_agent(event, "recover", "1")
        else:
            await router._agent_command(event, action)
        assert sent == ["完整命令错误文案"]
        assert router._active == set()
        if action != "recover":
            assert stopped == [True]
    asyncio.run(run())

