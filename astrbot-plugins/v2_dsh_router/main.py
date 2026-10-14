"""Route unhandled QQ text through the V2 journal and pinned DSH runtime."""

from __future__ import annotations

import os
import asyncio
from sys import maxsize
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .dsh_client import start_client
from .routing import should_route
from .service import ChatService, PendingTurnError
from .state import StateStore
from .mcp_server import McpServer
from .trust_domain import Owner, classify, parse_owners
from .persona_state import PersonaStateStore
from .scene_state import SceneStateStore


@star.register(
    "v2_dsh_router",
    "chatbot-V2",
    "Route unhandled QQ text to the V2 DSH conversation session.",
    "0.1.0",
)
class V2DshRouter(star.Star):
    async def initialize(self) -> None:
        self._closing = False
        self._active = set()
        # The plugin is symlinked into AstrBot's data/plugins directory by V2.
        repo = Path(__file__).resolve().parents[2]
        runtime = repo / ".runtime"
        from data.plugins.v2_journal.journal import JournalStore

        journal_dir = Path(star.StarTools.get_data_dir("v2_journal"))
        own_dir = Path(star.StarTools.get_data_dir("v2_dsh_router"))
        self.owners = parse_owners(os.environ.get("V2_QQ_OWNERS", "[]"))
        # Validate both state stores before starting either ACP process.
        journal = JournalStore(journal_dir / "journal.sqlite3")
        chat_state = StateStore(own_dir / "state-chat.sqlite3")
        owner_state = StateStore(own_dir / "state-owner.sqlite3") if self.owners else None
        self.mcp = McpServer(journal, logger=self.logger)
        self.clients = {}
        self.services = {}
        try:
            await self.mcp.start(port=int(os.environ.get("V2_MCP_PORT", "6210")))
            self.clients["chat"] = await start_client(
                dsh_bin=runtime / "dsh-runtime/node_modules/.bin/dsh",
                dsh_home=runtime / "dsh-home-chat",
                workspace=runtime / "dsh-workspace-chat",
                patch=repo / "dsh/profile/v2-agent-group.patch.yml",
                instructions=repo / "dsh/profile/agent-group.md",
            )
            self.services["chat"] = ChatService(
                journal, chat_state, self.clients["chat"], self.mcp,
            )
            if self.owners:
                self.clients["owner"] = await start_client(
                    dsh_bin=runtime / "dsh-runtime/node_modules/.bin/dsh",
                    dsh_home=runtime / "dsh-home-owner",
                    workspace=runtime / "dsh-workspace-owner",
                    patch=repo / "dsh/profile/v2-agent-owner.patch.yml",
                    instructions=repo / "dsh/profile/agent-owner.md",
                    enable_authorization=True,
                )
                self.services["owner"] = ChatService(
                    journal, owner_state, self.clients["owner"], self.mcp,
                    trusted_sources=True,
                )
        except BaseException as startup_error:
            try:
                await self._close_runtime()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "V2 DSH startup and cleanup both failed",
                    [startup_error, cleanup_error],
                ) from startup_error
            raise
        self.allowed_conversations = frozenset(
            part.strip() for part in os.environ.get("V2_QQ_ALLOWED_CONVERSATIONS", "").split(",")
            if part.strip()
        )
        if not self.allowed_conversations:
            self.logger.warning("V2 DSH router is idle: V2_QQ_ALLOWED_CONVERSATIONS is empty")

    @filter.command("v2agent")
    async def configure_agent(self, event: AstrMessageEvent, kind: str = "", value: str = ""):
        """Select this conversation's persona or skill scene from trusted host state."""
        event.set_extra("v2_deterministic_handled", True)
        if self._closing:
            return
        from data.plugins.v2_journal.journal import message_from_onebot

        async def reply(text):
            await event.send(MessageChain().message(text))

        try:
            raw = getattr(event.message_obj, "raw_message", None)
            message = message_from_onebot(event.get_platform_id(), raw)
            domain = classify(message, self.owners)
            if not message.message_id:
                raise ValueError("control message requires canonical platform message ID")
            if message.direction != "inbound" or Owner(
                    message.platform_id, message.bot_id, message.actor_id) not in self.owners:
                await reply("仅 Bot 所有者可以切换人格和场景。")
                return
            if message.conversation_key not in self.allowed_conversations:
                await reply("当前会话尚未启用 Agent。")
                return
            choices = {"persona": PersonaStateStore.CHOICES, "scene": SceneStateStore.CHOICES}
            if kind not in choices or value not in choices[kind]:
                await reply("用法：/v2agent persona <" + "|".join(sorted(PersonaStateStore.CHOICES))
                            + ">；/v2agent scene <" + "|".join(sorted(SceneStateStore.CHOICES)) + ">")
                return
            async def selected(_state):
                label = "人格" if kind == "persona" else "场景"
                await reply(label + "已切换为 " + value + "，下轮生效。")

            task = asyncio.current_task()
            self._active.add(task)
            try:
                await self.services[domain].select_profile(
                    message.conversation, kind, value, selected, get_group=event.get_group,
                )
            finally:
                self._active.discard(task)
        except PendingTurnError:
            await reply("当前会话有未确认的执行结果，请先恢复会话后再切换。")
        except Exception:
            self.logger.exception("V2 Agent profile selection failed")
            await reply("切换未完成，请检查 Bot 运行日志。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-maxsize)
    async def route_chat(self, event: AstrMessageEvent) -> None:
        raw = getattr(event.message_obj, "raw_message", None)
        if self._closing or not should_route(event, raw):
            return
        from data.plugins.v2_journal.journal import message_from_onebot

        try:
            message = message_from_onebot(event.get_platform_id(), raw)
            if not message.message_id:
                raise ValueError("QQ inbound message has no platform message ID")
            if message.conversation_key not in self.allowed_conversations:
                return
            domain = classify(message, self.owners)
            service = self.services[domain]

            async def send(reply: str) -> None:
                await event.send(MessageChain().message(reply))

            task = asyncio.current_task()
            self._active.add(task)
            try:
                await service.handle(
                    message.conversation, message.message_id, send,
                    get_group=event.get_group,
                )
            finally:
                self._active.discard(task)
        except PendingTurnError as exc:
            self.logger.error("V2 DSH conversation paused: %s", exc)
        except Exception:
            self.logger.exception("V2 DSH turn failed for QQ message %s", raw.get("message_id"))

    async def _close_runtime(self) -> None:
        errors = []
        for client in reversed(tuple(getattr(self, "clients", {}).values())):
            try:
                await asyncio.to_thread(client.close)
            except BaseException as error:
                errors.append(error)
        if hasattr(self, "mcp"):
            try:
                await self.mcp.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("V2 DSH runtime cleanup failed", errors)

    async def terminate(self) -> None:
        self._closing = True
        if self._active:
            draining = asyncio.gather(*tuple(self._active), return_exceptions=True)
            while not draining.done():
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    continue
        await self._close_runtime()
