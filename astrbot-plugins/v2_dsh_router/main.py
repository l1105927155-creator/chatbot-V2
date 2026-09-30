"""Route unhandled QQ text through the V2 journal and pinned DSH runtime."""

from __future__ import annotations

import os
import asyncio
from sys import maxsize
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .dsh_client import DshClient
from .routing import should_route
from .service import ChatService, PendingTurnError
from .state import StateStore


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
        self.dsh = DshClient(
            dsh_bin=runtime / "dsh-runtime/node_modules/.bin/dsh",
            dsh_home=runtime / "dsh-home",
            workspace=runtime / "dsh-workspace",
            patch=repo / "dsh/profile/v2-qq.patch.yml",
            instructions=repo / "dsh/profile/qq-agent.md",
        )
        self.service = ChatService(
            JournalStore(journal_dir / "journal.sqlite3"),
            StateStore(own_dir / "state.sqlite3"),
            self.dsh,
        )
        self.allowed_conversations = frozenset(
            part.strip() for part in os.environ.get("V2_QQ_ALLOWED_CONVERSATIONS", "").split(",")
            if part.strip()
        )
        if not self.allowed_conversations:
            self.logger.warning("V2 DSH router is idle: V2_QQ_ALLOWED_CONVERSATIONS is empty")

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

            async def send(reply: str) -> None:
                await event.send(MessageChain().message(reply))

            task = asyncio.current_task()
            self._active.add(task)
            try:
                await self.service.handle(message.conversation, message.message_id, send)
            finally:
                self._active.discard(task)
        except PendingTurnError as exc:
            self.logger.error("V2 DSH conversation paused: %s", exc)
        except Exception:
            self.logger.exception("V2 DSH turn failed for QQ message %s", raw.get("message_id"))

    async def terminate(self) -> None:
        self._closing = True
        if self._active:
            draining = asyncio.gather(*tuple(self._active), return_exceptions=True)
            while not draining.done():
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    continue
        if hasattr(self, "dsh"):
            await asyncio.to_thread(self.dsh.close)
