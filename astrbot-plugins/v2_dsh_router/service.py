"""Serialize one QQ conversation's journal delta, DSH turn, and reply."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

try:
    from .runtime_audit import CURRENT_TRACE, trace_scope
except ImportError:
    from runtime_audit import CURRENT_TRACE, trace_scope


class PendingTurnError(RuntimeError):
    """A prior DSH call may have been accepted; automatic replay is unsafe."""


class RecoveryError(RuntimeError):
    """The requested reviewed pending turn cannot currently be resolved."""


@dataclass
class _ActiveRun:
    session_id: str
    finished: bool = False
    lock: Any = field(default_factory=threading.Lock)


class ChatService:
    MAX_DELTA_ROWS = 100
    MAX_DELTA_BYTES = 32 * 1024
    MAX_ROW_BYTES = 8 * 1024

    def __init__(self, journal: Any, state: Any, dsh: Any, mcp: Any = None,
                 trusted_sources: bool = False, audit=None, domain: str | None = None,
                 execution_profile: str | None = None) -> None:
        self.journal = journal
        self.state = state
        self.dsh = dsh
        self.mcp = mcp
        self.trusted_sources = trusted_sources
        self.audit = audit
        self.domain = domain or "chat"
        if self.domain not in {"owner", "chat"}:
            raise ValueError("unknown router trust domain")
        self.execution_profile = execution_profile or ("owner-host" if self.domain == "owner" else "safe-chat")
        if self.execution_profile not in {"owner-host", "group-sandbox", "safe-chat"}:
            raise ValueError("unknown execution profile")
        self._queued = 0
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._active_runs: dict[tuple[str, str, str], _ActiveRun] = {}

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

    def metrics(self):
        return {"queued": self._queued, "active_chats": sum(lock.locked() for lock in self._locks.values()),
                "model_runs": len(self._active_runs),
                **(self.audit.snapshot(self.domain) if self.audit else {})}

    async def status(self, conversation) -> dict:
        """Read host state without waiting for a turn or creating a Session."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        state = await asyncio.to_thread(self.state.get, *identity)
        active = self._lock(conversation).locked()
        if state is None:
            return {"state": "running" if active else "not-started", "session_id": None,
                    "persona": "default", "scene": "casual-chat",
                    "last_seen_journal_id": 0, "pending_upper_cursor": None}
        trace = CURRENT_TRACE.get()
        if trace:
            trace.session_id = state.session_id
        persona, scene = await asyncio.gather(
            asyncio.to_thread(self.dsh.personas.current, state.session_id),
            asyncio.to_thread(self.dsh.scenes.current, state.session_id),
        )
        return {"state": "running" if active else (
                    "pending" if state.pending_upper_cursor is not None else "idle"),
                "session_id": state.session_id,
                "persona": persona["persona"], "scene": scene["scene"],
                "last_seen_journal_id": state.last_seen_journal_id,
                "pending_upper_cursor": state.pending_upper_cursor}

    async def cancel(self, conversation) -> bool:
        """Request native cancellation of this exact run without its chat lock."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        active = self._active_runs.get(identity)
        if active is None:
            return False

        def request():
            # A delayed worker must not cancel the next turn of the same SID.
            # The run's finally block marks this token before the chat lock
            # can be released and a later turn admitted.
            with active.lock:
                if active.finished:
                    return False
                return self.dsh.cancel_session(active.session_id)

        return await self._dsh_call(request)

    async def resolve_pending(self, conversation, expected_pending: int, get_group=None):
        """Drain and consume the Owner-reviewed uncertain delta without replay."""
        if (isinstance(expected_pending, bool) or not isinstance(expected_pending, int)
                or expected_pending <= 0):
            raise RecoveryError("待核对 Journal 编号必须为正整数。")
        lock = self._lock(conversation)
        if lock.locked():
            raise RecoveryError("当前会话仍在执行或发送，请先取消并等待结束后再核对恢复。")
        async with lock:
            identity = (conversation.platform_id, conversation.bot_id, conversation.key)
            state = await asyncio.to_thread(self.state.get, *identity)
            if state is None or state.pending_upper_cursor is None:
                raise RecoveryError("当前会话没有待核对的执行结果。")
            if state.pending_upper_cursor != expected_pending:
                raise RecoveryError("待核对 Journal 编号已变化，请重新查看 /v2agent status。")
            trace = CURRENT_TRACE.get()
            if trace:
                trace.session_id = state.session_id

            async def drain(servers=None):
                args = () if servers is None else (servers,)
                await self._dsh_call(self.dsh.drain_session, state.session_id, *args)

            if self.mcp is not None:
                async def no_tool_send(_reply):
                    raise RuntimeError("recovery cannot send through an Agent tool")
                async with self.mcp.turn(conversation, no_tool_send, get_group) as servers:
                    await drain(servers)
            else:
                await drain()
            # Only this exact reviewed state may be consumed. It is a skipped
            # input, not proof of a successful model turn or QQ delivery.
            try:
                resolved = await self._dsh_call(
                    self.state.resolve_pending, *identity, state.session_id,
                    state.last_seen_journal_id, expected_pending,
                )
                if trace:
                    trace.emit("recovery/resolved", previous_cursor=state.last_seen_journal_id,
                               pending_cursor=expected_pending)
                return resolved
            except ValueError as error:
                raise RecoveryError("恢复期间会话状态已变化，请重新查看 /v2agent status 后核对。") from error

    async def _delta(self, conversation, after: int, upper: int) -> list[Any]:
        # One reverse-indexed query bounds bootstrap/backlog work. The extra
        # row detects a gap without scanning the full canonical history.
        page = await asyncio.to_thread(
            self.journal.before, conversation, upper + 1, self.MAX_DELTA_ROWS + 1,
        )
        return [row for row in page if after < row.journal_id <= upper]

    @classmethod
    def _project_row(cls, row) -> tuple[str, bool]:
        message = row.message
        payload = {
            "journal_id": row.journal_id,
            "direction": message.direction,
            "actor_id": message.actor_id,
            "actor_name": message.actor_name,
            "message_id": message.message_id,
            "reply_to": message.reply_to,
            "content": message.content,
        }
        def encode():
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = encode()
        if len(encoded.encode("utf-8")) <= cls.MAX_ROW_BYTES:
            return encoded, False
        # This is a projection, not a journal mutation. A typed lookup by the
        # journal ID can retrieve the full original content and media metadata.
        for key, value in tuple(payload.items()):
            if isinstance(value, str):
                payload[key] = value[:128]
        text = message.content.get("text", "")
        payload["content"] = {
            "text": text[:1024] if isinstance(text, str) else "",
            "projection_truncated": True,
            "original_journal_id": row.journal_id,
        }
        encoded = encode()
        while len(encoded.encode("utf-8")) > cls.MAX_ROW_BYTES:
            payload["content"]["text"] = payload["content"]["text"][:len(payload["content"]["text"]) // 2]
            encoded = encode()
        return encoded, True

    @classmethod
    def _prompt(cls, conversation, rows: list[Any], wake_id: int, after: int = 0) -> str:
        selected = [(row, *cls._project_row(row)) for row in rows[-cls.MAX_DELTA_ROWS:]]
        while True:
            header = {
                "conversation": conversation.key[:256],
                "latest_user_journal_id": wake_id,
                "instruction": "根据按顺序提供的 QQ Journal 增量，回应最新的普通用户消息。"
                               "仅在可用上下文缺少所需事实时，查询当前会话的历史。",
                "history_window": {
                    "after_journal_id": after,
                    "through_journal_id": wake_id,
                    "complete": len(selected) == len(rows) and not any(truncated for _, _, truncated in selected),
                    "before_journal_id": selected[0][0].journal_id,
                    "truncated_journal_ids": [row.journal_id for row, _, truncated in selected if truncated],
                },
            }
            prompt = "\n".join([json.dumps(header, ensure_ascii=False, separators=(",", ":"))]
                               + [encoded for _, encoded, _ in selected])
            if len(prompt.encode("utf-8")) <= cls.MAX_DELTA_BYTES:
                return prompt
            if len(selected) == 1:
                raise ValueError("canonical wake projection exceeds the input budget")
            selected.pop(0)

    async def handle(
        self,
        conversation,
        inbound_message_id: str,
        send: Callable[[str], Awaitable[None]],
        get_group=None,
    ) -> str | None:
        """Run a turn once, then send through AstrBot while holding this chat's lock."""
        trace = self.audit.trace(conversation, inbound_message_id, self.domain) if self.audit else None
        started = time.monotonic()
        if trace:
            trace.emit("turn/queued")
        self._queued += 1
        waiting = True
        with trace_scope(trace):
            try:
                async with self._lock(conversation):
                    self._queued -= 1
                    waiting = False
                    if trace:
                        trace.emit("turn/start", queue_ms=(time.monotonic() - started) * 1000)

                    async def final_send(text):
                        return await trace.send(send, text, "final") if trace else await send(text)

                    async def tool_send(text):
                        return await trace.send(send, text, "mcp") if trace else await send(text)

                    if self.mcp is not None:
                        async with self.mcp.turn(conversation, tool_send, get_group) as servers:
                            result = await self._handle_locked(conversation, inbound_message_id, final_send, servers)
                    else:
                        result = await self._handle_locked(conversation, inbound_message_id, final_send)
                    if trace:
                        trace.emit("turn/duplicate" if result is None else "turn/completed",
                                   elapsed_ms=(time.monotonic() - started) * 1000)
                    return result
            except BaseException as error:
                if trace:
                    trace.emit("turn/failed", error_type=type(error).__name__,
                               elapsed_ms=(time.monotonic() - started) * 1000)
                raise
            finally:
                if waiting:
                    self._queued -= 1

    async def select_profile(self, conversation, kind: str, value: str, send,
                             get_group=None) -> dict:
        """Change trusted runtime state while serializing with ordinary turns."""
        selector = {"persona": self.dsh.select_persona, "scene": self.dsh.select_scene}.get(kind)
        if selector is None:
            raise ValueError("unknown profile selection")
        async with self._lock(conversation):
            identity = (conversation.platform_id, conversation.bot_id, conversation.key)
            state = await asyncio.to_thread(self.state.get, *identity)
            if state is not None and state.pending_upper_cursor is not None:
                raise PendingTurnError("profile selection requires recovery of the pending turn")

            async def select(servers=None):
                nonlocal state
                if state is None:
                    args = () if servers is None else (servers,)
                    session_id = await self._dsh_call(self.dsh.create_session, *args)
                    state = await asyncio.to_thread(self.state.get_or_create, *identity, session_id)
                trace = CURRENT_TRACE.get()
                if trace:
                    trace.session_id = state.session_id
                selected = await self._dsh_call(selector, state.session_id, value)
                await send(selected)
                return selected

            if self.mcp is not None:
                async def no_tool_send(_reply):
                    raise RuntimeError("profile selection cannot send through an Agent tool")
                async with self.mcp.turn(conversation, no_tool_send, get_group) as servers:
                    # Session creation discovers MCP tools but performs no model
                    # turn. The inactive bearer cannot execute tools afterward.
                    return await select(servers)
            return await select()

    async def _handle_locked(self, conversation, inbound_message_id, send, servers=None):
        mcp_args = () if servers is None else (servers,)
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        state = await asyncio.to_thread(self.state.get, *identity)
        if state is None:
            session_id = await self._dsh_call(self.dsh.create_session, *mcp_args)
            state = await asyncio.to_thread(self.state.get_or_create, *identity, session_id)
        trace = CURRENT_TRACE.get()
        if trace:
            trace.session_id = state.session_id
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
        if (wake.message.platform_id != conversation.platform_id or
                wake.message.bot_id != conversation.bot_id or
                wake.message.conversation_key != conversation.key or
                wake.message.direction != "inbound" or
                wake.message.message_id != inbound_message_id):
            raise RuntimeError("canonical wake message identity is inconsistent")
        if wake.journal_id <= state.last_seen_journal_id:
            return None
        # End this turn at its own wake row. A later ordinary message may
        # already be queued while we wait for the lock; consuming it here
        # would silently swallow that message's separate wake attempt.
        upper = wake.journal_id
        rows = await self._delta(conversation, state.last_seen_journal_id, upper)
        if not any(row.journal_id == wake.journal_id for row in rows):
            raise RuntimeError("inbound wake message is absent from journal delta")
        prompt = self._prompt(conversation, rows, wake.journal_id, state.last_seen_journal_id)
        trusted_source = None
        if self.trusted_sources:
            message = wake.message
            trusted_source = {
                "platform_id": message.platform_id,
                "bot_id": message.bot_id,
                "conversation_key": message.conversation_key,
                "requester_id": message.actor_id,
                "inbound_message_id": message.message_id,
            }
            if any(not isinstance(value, str) or not value for value in trusted_source.values()):
                raise RuntimeError("canonical wake message has incomplete trusted source facts")
        await asyncio.to_thread(self.state.begin_turn, *identity, state.session_id, upper)
        if trace:
            trace.emit("dsh/start", journal_id=upper)
        # Any error after begin_turn leaves a durable pending marker. The
        # SDK stdio protocol cannot prove whether a lost call was accepted.
        run_args = (state.session_id, prompt, *mcp_args)
        if self.trusted_sources:
            if not mcp_args:
                run_args += (None,)
            run_args += (trusted_source,)
        active = _ActiveRun(state.session_id)
        self._active_runs[identity] = active

        def run():
            try:
                return self.dsh.run(*run_args)
            finally:
                with active.lock:
                    active.finished = True

        try:
            reply = await self._dsh_call(run)
        finally:
            if self._active_runs.get(identity) is active:
                del self._active_runs[identity]
        await send(reply)
        await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
        return reply
