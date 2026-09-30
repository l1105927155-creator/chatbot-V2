"""Serialize one QQ conversation's journal delta, DSH turn, and reply."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any


class PendingTurnError(RuntimeError):
    """A prior DSH call may have been accepted; automatic replay is unsafe."""


class ChatService:
    def __init__(self, journal: Any, state: Any, dsh: Any) -> None:
        self.journal = journal
        self.state = state
        self.dsh = dsh
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    @staticmethod
    async def _dsh_call(function, *args):
        running = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            # Cancelling the waiter cannot stop the synchronous ACP thread.
            # Keep ownership until it ends, including during session creation.
            while not running.done():
                try:
                    await asyncio.shield(running)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not running.cancelled():
                running.exception()
            raise

    def _lock(self, conversation) -> asyncio.Lock:
        key = (conversation.platform_id, conversation.bot_id, conversation.key)
        return self._locks.setdefault(key, asyncio.Lock())

    async def _delta(self, conversation, after: int, upper: int) -> list[Any]:
        rows = []
        cursor = after
        while cursor < upper:
            page = await asyncio.to_thread(self.journal.after, conversation, cursor, 500)
            selected = [row for row in page if row.journal_id <= upper]
            rows.extend(selected)
            if not page or page[-1].journal_id >= upper:
                break
            cursor = page[-1].journal_id
        return rows

    @staticmethod
    def _prompt(conversation, rows: list[Any], wake_id: int) -> str:
        header = {
            "conversation": conversation.key,
            "latest_user_journal_id": wake_id,
            "instruction": "Reply to the latest ordinary user message using this ordered QQ journal delta.",
        }
        lines = [json.dumps(header, ensure_ascii=False)]
        for row in rows:
            message = row.message
            lines.append(json.dumps({
                "journal_id": row.journal_id,
                "direction": message.direction,
                "actor_id": message.actor_id,
                "actor_name": message.actor_name,
                "message_id": message.message_id,
                "reply_to": message.reply_to,
                "content": message.content,
            }, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines)

    async def handle(
        self,
        conversation,
        inbound_message_id: str,
        send: Callable[[str], Awaitable[None]],
    ) -> str | None:
        """Run a turn once, then send through AstrBot while holding this chat's lock."""
        async with self._lock(conversation):
            identity = (conversation.platform_id, conversation.bot_id, conversation.key)
            state = await asyncio.to_thread(self.state.get, *identity)
            if state is None:
                session_id = await self._dsh_call(self.dsh.create_session)
                state = await asyncio.to_thread(self.state.get_or_create, *identity, session_id)
            if state.pending_upper_cursor is not None:
                raise PendingTurnError(
                    f"DSH turn outcome is uncertain for {conversation.key}; manual recovery required"
                )
            matches = await asyncio.to_thread(
                self.journal.by_message_id,
                conversation.platform_id, conversation.bot_id, inbound_message_id,
            )
            wake = next((row for row in matches if
                         row.message.conversation_key == conversation.key and
                         row.message.direction == "inbound"), None)
            if wake is None:
                raise RuntimeError("inbound wake message is missing from the canonical journal")
            if wake.journal_id <= state.last_seen_journal_id:
                return None
            # End this turn at its own wake row. A later ordinary message may
            # already be queued while we wait for the lock; consuming it here
            # would silently swallow that message's separate wake attempt.
            upper = wake.journal_id
            rows = await self._delta(conversation, state.last_seen_journal_id, upper)
            if not any(row.journal_id == wake.journal_id for row in rows):
                raise RuntimeError("inbound wake message is absent from journal delta")
            prompt = self._prompt(conversation, rows, wake.journal_id)
            await asyncio.to_thread(self.state.begin_turn, *identity, state.session_id, upper)
            # Any error after begin_turn leaves a durable pending marker. The
            # SDK stdio protocol cannot prove whether a lost call was accepted.
            reply = await self._dsh_call(self.dsh.run, state.session_id, prompt)
            await send(reply)
            await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
            return reply
