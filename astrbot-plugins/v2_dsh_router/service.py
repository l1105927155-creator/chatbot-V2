"""Serialize one QQ conversation's journal delta, DSH turn, and reply."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

try:
    from .reply_safety import validate_reply, ReplyContentError
    from .runtime_audit import CURRENT_TRACE, trace_scope
    from .dsh_client import DshTurnCancelled, DshModelTurnFailure, DshTurnFailure
    from .background import BackgroundTask, validate_event, task_key
    from .console_settings import configured_store, session_settings
except ImportError:
    from reply_safety import validate_reply, ReplyContentError
    from runtime_audit import CURRENT_TRACE, trace_scope
    from dsh_client import DshTurnCancelled, DshModelTurnFailure, DshTurnFailure
    from background import BackgroundTask, validate_event, task_key
    from console_settings import configured_store, session_settings


class PendingTurnError(RuntimeError):
    """A prior DSH call may have been accepted; automatic replay is unsafe."""


class ModelTurnError(RuntimeError):
    """A terminal model failure whose turn had no tool calls or side effects."""

    def __init__(self, code="UNKNOWN", *, tools_completed=False):
        self.tools_completed = tools_completed
        self.code = code if isinstance(code, str) else "UNKNOWN"
        super().__init__("model request failed after a confirmed terminal response")


class RecoveryError(RuntimeError):
    """The requested reviewed pending turn cannot currently be resolved."""


class ResetError(RuntimeError):
    """A clean conversation rotation could not be safely committed."""


class _ResetPreempted(Exception):
    """A turn still preparing its first DSH prompt yielded to /new."""

    def __init__(self, barrier):
        super().__init__("conversation reset is pending")
        self.barrier = barrier


class FinalReplyDeliveryError(RuntimeError):
    """DSH completed a turn, but AstrBot could not deliver its final reply."""

    def __init__(self, cause: Exception) -> None:
        super().__init__("DSH turn completed but final reply delivery failed")
        self.cause = cause


class AgentDisabledError(RuntimeError):
    """The conversation Agent is disabled and cannot accept this turn."""


@dataclass
class _ActiveRun:
    session_id: str
    inbound_message_id: str | None = None
    conversation: Any = None
    requester_id: str | None = None
    messaging: Any = None
    finished: bool = False
    error: BaseException | None = None
    pending_cursor: int | None = None
    reset_command_id: str | None = None
    succeeded: bool = False
    cancel_token: Any = None
    done: threading.Event = field(default_factory=threading.Event)
    lock: Any = field(default_factory=threading.Lock)


@dataclass
class _ResetBarrier:
    command_journal_id: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    trace: Any = None
    succeeded: bool = False


@dataclass
class _SendLifetime:
    active: bool = True


class ChatService:
    MAX_DELTA_ROWS = 100
    MAX_DELTA_BYTES = 32 * 1024
    MAX_ROW_BYTES = 8 * 1024
    BACKGROUND_ACTIVE_LIMIT = 256
    BACKGROUND_TERMINAL_LIMIT = 256
    BACKGROUND_TOTAL_LIMIT = 512
    BUSY_NOTICE_LIMIT = 256
    BUSY_NOTICE_TIMEOUT_SECONDS = 5
    BUSY_NOTICE = "已收到你的消息。上一项任务仍在前台处理中，这条消息会随后处理。"

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
        self._busy_notices: OrderedDict[tuple, None] = OrderedDict()
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._active_runs: dict[tuple[str, str, str], _ActiveRun] = {}
        self._reset_barriers: dict[tuple[str, str, str], _ResetBarrier] = {}
        self._delivery_counts: dict[tuple[str, str, str], int] = {}
        self._reset_cancelled_wakes: set[tuple[tuple[str, str, str], str]] = set()
        self._disabled_identities: set[tuple[str, str, str]] = set()
        self._background_tasks: dict[tuple[str, str], BackgroundTask] = {}
        self._background_terminal_lru: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._background_delivery_tasks: set[asyncio.Task] = set()
        self._background_closing = False
        self.console_store = configured_store()

    async def handle_background_event(self, raw_event: dict) -> bool:
        """Accept a native DSH lifecycle callback without waiting for chat delivery."""
        if self._background_closing:
            return False
        try:
            event = validate_event(raw_event)
        except (TypeError, ValueError):
            return False
        key = task_key(event)
        task = self._background_tasks.get(key)
        if event["event"] == "started":
            if task is not None:
                return task.requester_id == event["requester_id"]
            self._prune_background_tasks()
            active_count = sum(item.status == "running" for item in self._background_tasks.values())
            if (active_count >= self.BACKGROUND_ACTIVE_LIMIT or
                    len(self._background_tasks) >= self.BACKGROUND_TOTAL_LIMIT):
                return False
            active = next((run for run in self._active_runs.values()
                           if run.session_id == event["parent_session_id"]), None)
            if (active is None or active.finished or active.messaging is None or
                    active.requester_id != event["requester_id"] or
                    active.conversation is None or not active.inbound_message_id):
                return False
            identity = (str(active.conversation.platform_id), str(active.conversation.bot_id),
                        str(active.conversation.key))
            self._background_tasks[key] = BackgroundTask(
                parent_session_id=event["parent_session_id"], task_id=event["task_id"],
                requester_id=active.requester_id, identity=identity,
                conversation=active.conversation, messaging=active.messaging,
                inbound_message_id=active.inbound_message_id,
            )
            return True
        if task is None or task.requester_id != event["requester_id"]:
            return False
        if task.terminal_event is not None:
            return task.terminal_event == event
        task.status = event["status"]
        task.terminal_event = event
        delivery = asyncio.create_task(self._deliver_background(task, event),
                                       name="v2-background-delivery")
        task.delivery_task = delivery
        self._background_delivery_tasks.add(delivery)
        self._background_terminal_lru[key] = None
        self._background_terminal_lru.move_to_end(key)
        self._prune_background_tasks()
        delivery.add_done_callback(lambda done, item_key=key:
                                   self._background_delivery_done(done, item_key))
        return True

    def _background_delivery_done(self, delivery, key):
        self._background_delivery_tasks.discard(delivery)
        self._prune_background_tasks()

    def _prune_background_tasks(self):
        """Retain a bounded terminal idempotency window and all live tasks."""
        attempts = len(self._background_terminal_lru)
        while attempts:
            over_terminal_limit = len(self._background_terminal_lru) > self.BACKGROUND_TERMINAL_LIMIT
            over_total_limit = len(self._background_tasks) >= self.BACKGROUND_TOTAL_LIMIT
            if not over_terminal_limit and not over_total_limit:
                break
            key, _ = self._background_terminal_lru.popitem(last=False)
            attempts -= 1
            task = self._background_tasks.get(key)
            if task is None:
                continue
            delivery = task.delivery_task
            if delivery is not None and not delivery.done():
                self._background_terminal_lru[key] = None
                continue
            self._background_tasks.pop(key, None)

    def active_background(self, conversation) -> bool:
        identity = (str(conversation.platform_id), str(conversation.bot_id), str(conversation.key))
        return any(task.identity == identity and task.status == "running"
                   for task in self._background_tasks.values())

    def _discard_background_session(self, identity, session_id):
        for key, task in tuple(self._background_tasks.items()):
            if task.identity == identity and task.parent_session_id == session_id:
                self._background_tasks.pop(key, None)
                self._background_terminal_lru.pop(key, None)

    async def _deliver_background(self, task: BackgroundTask, event: dict[str, str]) -> None:
        identity = task.identity
        lock = self._locks.setdefault(identity, asyncio.Lock())
        trace = self.audit.trace(task.conversation, task.inbound_message_id, self.domain) if self.audit else None
        try:
            async with lock:
                current = await asyncio.to_thread(self.state.get, *identity)
                if (self._background_closing or identity in getattr(self, "_disabled_identities", set()) or
                        identity in self._reset_barriers or current is None or
                        current.session_id != task.parent_session_id):
                    if trace:
                        trace.emit("background/delivery_suppressed", task_id=task.task_id,
                                   status=event["status"])
                    return
                status = event["status"]
                text = event.get("text", "").strip()
                if status == "completed":
                    message = text
                elif status == "cancelled":
                    message = text or "后台检索已停止。"
                else:
                    message = text or "后台检索遇到错误，未能完成。"
                if event.get("label") and not message.startswith(event["label"]):
                    message = f"{event['label']}：{message}"
                mentions = [task.requester_id] if task.conversation.key.startswith("group:") else []
                sender = lambda value: task.messaging.send_origin(
                    value, mention_user_ids=mentions, reply_to_journal_id=None)
                # Keep reset admission and actual AstrBot send atomic on the
                # event loop; no await may occur between this lease and send.
                self._delivery_enter(identity)
                try:
                    if trace:
                        await trace.send(sender, message, "mcp")
                    else:
                        await sender(message)
                finally:
                    self._delivery_exit(identity)
        except Exception as exc:
            # Delivery may have reached QQ before the platform raised. Never
            # retry an ambiguous send; retain only a body-free audit marker.
            if trace:
                trace.emit("background/delivery_failed", task_id=task.task_id,
                           error_type=type(exc).__name__)

    async def close_background(self) -> None:
        """Stop callback acceptance and cancel queued sends before DSH shutdown."""
        self._background_closing = True
        pending = tuple(self._background_delivery_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_delivery_tasks.clear()

    def begin_reset(self, conversation, inbound_message_id):
        """Synchronously publish /new before its command handler first awaits."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        if self._delivery_counts.get(identity, 0):
            raise ResetError("当前会话的旧轮次正在发送消息，请发送 /new 重试；本次尚未重置。")
        if identity in self._reset_barriers:
            raise ResetError("已有会话重置正在进行，请稍后重试。")
        trace = self.audit.trace(conversation, inbound_message_id, self.domain) if self.audit else None
        if trace:
            trace.emit("session/reset_started")
        barrier = _ResetBarrier()
        barrier.trace = trace
        self._reset_barriers[identity] = barrier
        return barrier

    def _delivery_enter(self, identity, lifetime=None):
        # Synchronous check and increment close the race with begin_reset.
        if identity in getattr(self, "_disabled_identities", set()):
            raise AgentDisabledError("此会话的 Agent 已禁用。")
        if lifetime is not None and not lifetime.active:
            raise ResetError("旧轮次已结束，不能再发送消息。")
        if identity in self._reset_barriers:
            raise ResetError("会话正在重置，旧轮次的发送已停止。")
        self._delivery_counts[identity] = self._delivery_counts.get(identity, 0) + 1

    def _delivery_exit(self, identity):
        count = self._delivery_counts.get(identity, 0) - 1
        if count > 0:
            self._delivery_counts[identity] = count
        else:
            self._delivery_counts.pop(identity, None)

    def _remember_reset_cancel(self, identity, inbound_message_id):
        if inbound_message_id:
            self._reset_cancelled_wakes.add((identity, str(inbound_message_id)))
            if len(self._reset_cancelled_wakes) > 256:
                self._reset_cancelled_wakes.pop()

    def is_reset_cancellation(self, conversation, inbound_message_id) -> bool:
        """Whether this cancelled wake was stopped specifically for /new."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        key = (identity, str(inbound_message_id))
        if key in self._reset_cancelled_wakes:
            self._reset_cancelled_wakes.discard(key)
            return True
        active = self._active_runs.get(identity)
        matches_active = (active is not None and active.reset_command_id is not None
                          and active.inbound_message_id == str(inbound_message_id))
        if matches_active:
            self._reset_cancelled_wakes.discard(key)
        return bool(matches_active)

    async def _outbound_after(self, conversation, journal_id):
        """Find completed bot sends after /new without skipping later user rows."""
        cursor = journal_id
        for _ in range(100):
            rows = await asyncio.to_thread(self.journal.after, conversation, cursor, 500)
            if any(row.message.direction == "outbound" for row in rows):
                return True
            if len(rows) < 500:
                return False
            cursor = rows[-1].journal_id
        raise ResetError("命令之后的会话记录过多，无法安全确认重置范围；请稍后重新发送 /new。")

    async def _has_unjournaled_receipt(self, conversation):
        if self.audit is None:
            return False

        def inspect():
            if self.audit.write_errors:
                raise ResetError("发送回执审计暂不可用，无法安全确认重置范围。")
            pending = any(not row["journal_confirmed"] for row in
                          self.audit.receipt_evidence(self.journal, conversation=conversation, strict=True))
            if self.audit.write_errors:
                raise ResetError("发送回执审计暂不可用，无法安全确认重置范围。")
            return pending

        return await asyncio.to_thread(inspect)

    @staticmethod
    async def _dsh_call(function, *args, **kwargs):
        running = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
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

    def _ensure_enabled(self, identity) -> None:
        store = getattr(self, "console_store", None)
        if store is not None:
            row = store.read()["sessions"].get(store.session_key(dict(
                zip(("platform_id", "bot_id", "conversation_key"), identity))))
            if row is None or row["settings"]["enabled"] is not True:
                raise AgentDisabledError("当前会话 Agent 已禁用或未接入。")
        if identity in getattr(self, "_disabled_identities", set()):
            raise AgentDisabledError("此会话的 Agent 已禁用。")

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
            fault = await asyncio.to_thread(self.state.get_pre_session_fault, *identity)
            if fault is not None:
                return {"state": "pending", "session_id": None, "persona": "未知", "scene": "未知",
                        "last_seen_journal_id": 0, "pending_upper_cursor": fault["failed_upper_cursor"],
                        "pending_fault_kind": fault["fault_kind"], "pending_fault_code": fault["fault_code"],
                        "pending_fault_details": None, "background_running": 0}
            return {"state": "running" if active else "not-started", "session_id": None,
                    "persona": "default", "scene": "casual-chat",
                    "last_seen_journal_id": 0, "pending_upper_cursor": None,
                    "background_running": 0, "pending_fault_kind": None,
                    "pending_fault_code": None}
        background_running = sum(
            1 for task in self._background_tasks.values()
            if task.identity == tuple(map(str, identity)) and
            task.parent_session_id == state.session_id and task.status == "running"
        )
        trace = CURRENT_TRACE.get()
        if trace:
            trace.session_id = state.session_id
        # These stores read local selection files; status never asks the model
        # or ACP runtime. A missing store must not hide the durable fault.
        async def local_selection(name):
            try:
                store = getattr(self.dsh, name)
                return await asyncio.wait_for(asyncio.to_thread(store.current, state.session_id), 1)
            except Exception:
                return None
        persona, scene = await asyncio.gather(local_selection("personas"), local_selection("scenes"))
        persona_name = persona.get("persona", "未知") if isinstance(persona, dict) else "未知"
        scene_name = scene.get("scene", "未知") if isinstance(scene, dict) else "未知"
        return {"state": "running" if active else (
                    "pending" if state.pending_upper_cursor is not None else
                    "running" if background_running else "idle"),
                "session_id": state.session_id,
                "persona": persona_name, "scene": scene_name,
                "last_seen_journal_id": state.last_seen_journal_id,
                "pending_upper_cursor": state.pending_upper_cursor,
                "pending_fault_kind": state.pending_fault_kind,
                "pending_fault_code": state.pending_fault_code,
                "pending_fault_details": state.pending_fault_details,
                "background_running": background_running}

    async def cancel(self, conversation) -> bool:
        """Stop a prompt or drain background work owned by this conversation."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        active = self._active_runs.get(identity)
        if active is not None:
            def request():
                # A delayed worker must not cancel the next turn of the same SID.
                with active.lock:
                    if active.finished:
                        return False
                    options = ({"cancel_token": active.cancel_token}
                               if active.cancel_token is not None else {})
                    return self.dsh.cancel_session(active.session_id, **options)

            accepted = await self._dsh_call(request)
            if accepted:
                # Confirmed cancellation closes the native Session and drains
                # its children before DshClient releases trusted turn state.
                await asyncio.to_thread(active.done.wait)
                return True
            if not active.done.is_set():
                return False
            if self._active_runs.get(identity) is not active:
                # This cancellation request belonged to an older run; never
                # let its delayed continuation drain a subsequent run.
                return False

        # An idle native Session can still own background jobs. Serialize with
        # new turns, then close that same SID; ACP close drains child work and
        # leaves the durable Session available for a later resume.
        async with self._lock(conversation):
            state = await asyncio.to_thread(self.state.get, *identity)
            if state is None:
                return False
            has_background = any(
                item.identity == tuple(map(str, identity)) and
                item.parent_session_id == state.session_id and item.status == "running"
                for item in self._background_tasks.values()
            )
            if not has_background:
                return False
            await self._dsh_call(self.dsh.close_session, state.session_id)
            self._discard_background_session(identity, state.session_id)
            return True

    async def set_enabled(self, conversation, enabled: bool) -> bool:
        """Enable or disable this conversation's Agent without rotating state."""
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        disabled = getattr(self, "_disabled_identities", None)
        if disabled is None:  # Legacy test doubles and restored service objects.
            disabled = self._disabled_identities = set()
        if enabled:
            disabled.discard(identity)
            # Enabling itself intentionally performs no DSH call and does not move the cursor.
            return True
        disabled.add(identity)
        # Publish the barrier before cancellation; queued turns and late send
        # callbacks observe it synchronously while cancel drains the active run.
        active = self._active_runs.get(identity)
        cancelled = await self.cancel(conversation)
        if active is not None and not cancelled and not active.done.is_set():
            raise RuntimeError("Agent 准入已停止，但当前任务尚未确认取消，请检查运行状态。")
        return False

    async def reset_conversation(self, conversation, inbound_message_id, get_group=None, *, barrier=None):
        """Rotate to a fresh native Session at the canonical /new command row.

        A command handler may call begin_reset synchronously before awaiting
        service discovery. Every queued turn waits outside the chat lock until
        this operation commits or fails, then decides from canonical cursors.
        """
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        if barrier is None:
            barrier = self.begin_reset(conversation, inbound_message_id)
        elif self._reset_barriers.get(identity) is not barrier:
            raise ResetError("会话重置屏障已失效，请重新发送 /new。")
        trace = barrier.trace
        try:
            matches = await asyncio.to_thread(
                self.journal.by_message_id,
                conversation.platform_id, conversation.bot_id, inbound_message_id,
            )
            command = next((row for row in matches if
                            row.message.conversation_key == conversation.key and
                            row.message.direction == "inbound" and
                            row.message.message_id == inbound_message_id), None)
            if command is None or command.message.platform_id != conversation.platform_id \
                    or command.message.bot_id != conversation.bot_id:
                raise ResetError("找不到这条 /new 命令的规范会话记录，未重置上下文。")
            return await self._reset_at_floor(
                conversation, inbound_message_id, get_group, barrier, command.journal_id,
            )
        except PendingTurnError:
            if trace:
                trace.emit("session/reset_failed")
            raise
        except ResetError:
            if trace:
                trace.emit("session/reset_failed")
            raise
        except asyncio.CancelledError:
            if trace:
                trace.emit("session/reset_failed")
            raise
        except BaseException as error:
            if trace:
                trace.emit("session/reset_failed")
            raise ResetError("会话重置未完成，原会话映射保持不变。") from error
        finally:
            barrier.done.set()
            if self._reset_barriers.get(identity) is barrier:
                del self._reset_barriers[identity]

    async def reset_from_console(self, conversation, get_group=None):
        """Start a fresh DSH session at the canonical history high-watermark.

        The console operation is identified only in the runtime audit trace;
        it never creates a platform message or journal row.
        """
        operation_id = f"console:{time.time_ns()}"
        barrier = self.begin_reset(conversation, operation_id)
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        try:
            # The synchronous high-watermark query runs after the barrier is
            # published and before this coroutine yields, fixing the reset cut.
            floor_cursor = self.journal.high_watermark(conversation)
            barrier.command_journal_id = floor_cursor
            return await self._reset_at_floor(
                conversation, operation_id, get_group, barrier, floor_cursor,
            )
        except PendingTurnError:
            if barrier.trace:
                barrier.trace.emit("session/reset_failed")
            raise
        except ResetError:
            if barrier.trace:
                barrier.trace.emit("session/reset_failed")
            raise
        except asyncio.CancelledError:
            if barrier.trace:
                barrier.trace.emit("session/reset_failed")
            raise
        except BaseException as error:
            if barrier.trace:
                barrier.trace.emit("session/reset_failed")
            raise ResetError("会话重置未完成，原会话映射保持不变。") from error
        finally:
            barrier.done.set()
            if self._reset_barriers.get(identity) is barrier:
                del self._reset_barriers[identity]

    async def _reset_at_floor(self, conversation, reset_id, get_group, barrier, floor_cursor):
        """Shared reset transaction after its command row or console cut is fixed."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        trace = barrier.trace
        barrier.command_journal_id = floor_cursor

        # Only a run active at reset admission may have its exact pending
        # row skipped. Unknown persisted pending state is never inferred
        # to be safe merely because this command arrived later.
        active = self._active_runs.get(identity)
        expected_pending = None
        if active is not None:
            active.reset_command_id = str(reset_id)
            if not active.done.is_set():
                self._remember_reset_cancel(identity, active.inbound_message_id)
                try:
                    cancellation_requested = await self.cancel(conversation)
                except BaseException:
                    if active.inbound_message_id:
                        self._reset_cancelled_wakes.discard((identity, str(active.inbound_message_id)))
                    raise
                if (not cancellation_requested and not active.done.is_set()
                        and not active.succeeded):
                    if active.inbound_message_id:
                        self._reset_cancelled_wakes.discard((identity, str(active.inbound_message_id)))
                    raise ResetError("当前模型轮次未能确认取消，旧会话保持不变。")
            try:
                await asyncio.to_thread(active.done.wait)
            except BaseException:
                if active.inbound_message_id:
                    self._reset_cancelled_wakes.discard((identity, str(active.inbound_message_id)))
                raise
            if isinstance(active.error, DshTurnCancelled):
                expected_pending = active.pending_cursor
            elif active.error is not None:
                if active.inbound_message_id:
                    self._reset_cancelled_wakes.discard((identity, str(active.inbound_message_id)))
                raise ResetError("当前轮次没有收到原生取消确认，旧会话保持不变。") from active.error
            # error=None means the native run returned normally. The
            # handle suppresses delivery under this barrier and consumes
            # that known-complete wake before releasing the chat lock.

        lock = self._lock(conversation)
        async with lock:
            current = await asyncio.to_thread(self.state.get, *identity)
            if await self._has_unjournaled_receipt(conversation):
                raise ResetError("有一条此前已获平台回执的消息尚未进入会话记录，请稍后重新发送 /new；本次尚未重置。")
            if await self._outbound_after(conversation, floor_cursor):
                raise ResetError("命令之后已有旧轮次回复送达，请在该回复完成后重新发送 /new；本次尚未重置。")
            if current is not None and current.pending_upper_cursor is not None:
                if expected_pending is None or current.pending_upper_cursor != expected_pending:
                    raise PendingTurnError("存在未核对的 DSH 轮次，不能安全重置会话。")

            old_persona = None
            old_scene = None
            if current is not None:
                if getattr(self.dsh, "personas", None) is not None:
                    old_persona = await asyncio.to_thread(self.dsh.personas.current, current.session_id)
                if getattr(self.dsh, "scenes", None) is not None:
                    old_scene = await asyncio.to_thread(self.dsh.scenes.current, current.session_id)

            async def rotate(servers=None):
                args = () if servers is None else (servers,)
                created = {"session_id": None}
                try:
                    def create_and_record():
                        session_id = self.dsh.create_session(*args)
                        created["session_id"] = session_id
                        return session_id

                    created_session = await self._dsh_call(create_and_record)
                    saved_model = (await asyncio.to_thread(self.state.get_model_route, *identity)
                                   if hasattr(self.state, 'get_model_route') else None)
                    if saved_model is not None:
                        await self._dsh_call(lambda: self.dsh.model_status(
                            created_session, *args, model_route=saved_model))
                    if old_persona is not None and getattr(self.dsh, "personas", None) is not None:
                        value = old_persona.get("persona")
                        if value is not None:
                            await self._dsh_call(self.dsh.select_persona, created_session, value)
                    if old_scene is not None and getattr(self.dsh, "scenes", None) is not None:
                        value = old_scene.get("scene")
                        if value is not None:
                            await self._dsh_call(self.dsh.select_scene, created_session, value)
                    if current is not None:
                        drain_args = () if servers is None else (servers,)
                        await self._dsh_call(self.dsh.drain_session, current.session_id, *drain_args)
                    new_state = await self._dsh_call(
                        self.state.rotate_session,
                        *identity,
                        current.session_id if current is not None else None,
                        current.last_seen_journal_id if current is not None else None,
                        created_session,
                        floor_cursor,
                        expected_pending=expected_pending,
                    )
                    if created_session == new_state.session_id:
                        created["session_id"] = None  # The state store now owns this mapping.
                    if trace:
                        trace.session_id = new_state.session_id
                        fields = {
                            "previous_cursor": current.last_seen_journal_id if current else 0,
                            "journal_id": floor_cursor,
                            "history_after": new_state.history_after_journal_id,
                        }
                        if current is not None:
                            fields["previous_session_id"] = current.session_id
                        if expected_pending is not None:
                            fields["discarded_pending"] = expected_pending
                        trace.emit("session/reset_completed", **fields)
                    if current is not None:
                        self._discard_background_session(identity, current.session_id)
                    return new_state
                finally:
                    sid = created["session_id"]
                    if sid is not None:
                        try:
                            committed = await asyncio.to_thread(self.state.get, *identity)
                        except BaseException:
                            # An unreadable CAS result is ambiguous. Keep
                            # the native session alive rather than risk
                            # closing a SID that may now be authoritative.
                            committed = None
                            ambiguous = True
                        else:
                            ambiguous = committed is not None and committed.session_id == sid
                        if not ambiguous:
                            try:
                                await self._dsh_call(self.dsh.close_session, sid)
                            except BaseException:
                                # DshClient quarantines an undrained native
                                # session; the durable old mapping stays intact.
                                pass

            if self.mcp is None:
                new_state = await rotate()
                barrier.succeeded = True
                return new_state

            async def no_tool_send(_reply):
                raise RuntimeError("conversation reset cannot send through an Agent tool")

            options = {}
            if current is not None:
                floor = getattr(current, "history_after_journal_id", 0)
                if floor:
                    options["history_after"] = floor
            async with self.mcp.turn(conversation, no_tool_send, get_group, **options) as servers:
                new_state = await rotate(servers)
                barrier.succeeded = True
                return new_state

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
            if state is None:
                fault = await asyncio.to_thread(self.state.get_pre_session_fault, *identity)
                if fault is None:
                    raise RecoveryError("当前会话没有待核对的执行结果。")
                if fault["failed_upper_cursor"] != expected_pending:
                    raise RecoveryError("待核对 Journal 编号已变化，请重新查看 /v2agent status。")

                async def create_clean(servers=None):
                    args = () if servers is None else (servers,)
                    session_id = await self._dsh_call(self.dsh.create_session, *args)
                    try:
                        return await self._dsh_call(
                            self.state.recover_pre_session_fault, *identity,
                            expected_pending, session_id,
                        )
                    except BaseException:
                        try:
                            await self._dsh_call(self.dsh.close_session, session_id)
                        except BaseException:
                            pass
                        raise

                try:
                    if self.mcp is not None:
                        async def no_tool_send(_reply):
                            raise RuntimeError("recovery cannot send through an Agent tool")
                        async with self.mcp.turn(conversation, no_tool_send, get_group) as servers:
                            resolved = await create_clean(servers)
                    else:
                        resolved = await create_clean()
                except Exception as error:
                    raise RecoveryError(
                        "Agent 运行通道仍不可用，尚未解除暂停；修复后重新执行 recover。"
                    ) from error
                return resolved
            if state.pending_upper_cursor is None:
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
    def _prompt(cls, conversation, rows: list[Any], wake_id: int, after: int = 0, *,
                visual_input=None, visible_ids=None, history_floor: int = 0,
                resume_through_journal_id: int = 0) -> str:
        selected = [(row, *cls._project_row(row)) for row in rows[-cls.MAX_DELTA_ROWS:]
                    if visible_ids is None or row.journal_id in visible_ids]
        if resume_through_journal_id > history_floor:
            projected = []
            for row, encoded, truncated in selected:
                if row.journal_id <= resume_through_journal_id:
                    payload = json.loads(encoded)
                    payload["input_role"] = "background_only"
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                projected.append((row, encoded, truncated))
            selected = projected
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
            if history_floor > 0:
                header["history_access"] = {
                    "after_journal_id": history_floor,
                    "instruction": f"本会话可查询的历史从 Journal {history_floor} 之后开始，依据可用范围和查询结果作答。",
                }
            if resume_through_journal_id > history_floor:
                header["resume_background"] = {
                    "through_journal_id": resume_through_journal_id,
                    "instruction": "会话曾暂停接收 Agent 请求。标为 background_only 的旧消息仅用于了解背景，"
                                   "不补执行其中的请求。回应本次最新消息，缺少事实时按需查询历史。",
                }
            if visual_input is not None:
                header['visual_input'] = visual_input
            prompt = "\n".join([json.dumps(header, ensure_ascii=False, separators=(",", ":"))]
                               + [encoded for _, encoded, _ in selected])
            if len(prompt.encode("utf-8")) <= cls.MAX_DELTA_BYTES:
                return prompt
            if len(selected) == 1:
                raise ValueError("canonical wake projection exceeds the input budget")
            selected.pop(0)

    async def model_status(self, conversation, get_group=None):
        return await self._model_control(conversation, get_group=get_group)

    async def compact_from_console(self, conversation, get_group=None):
        """Run native maintenance without injecting a QQ/chat prompt."""
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        if self._lock(conversation).locked() or identity in self._reset_barriers:
            raise RuntimeError("会话忙碌，请在本轮结束后压缩。")
        async with self._lock(conversation):
            state = self.state.get(*identity)
            if state is None:
                return {"compacted": False, "reason": "no_session"}
            if state.pending_upper_cursor is not None:
                raise PendingTurnError("存在待核对轮次，请先核对再压缩。")
            route = self.state.get_model_route(*identity)
            if getattr(self, "console_store", None) is not None:
                model = session_settings(self.console_store, conversation)["model"]
                route = (model["provider"], model["model"])

            async def perform(servers=None):
                return await self._dsh_call(lambda: self.dsh.compact_session(
                    state.session_id, servers, model_route=route))

            if self.mcp is None:
                return await perform()

            async def no_send(_text, **_kwargs):
                raise RuntimeError("压缩不能发送 QQ 消息。")

            async with self.mcp.turn(conversation, no_send, get_group,
                                     history_after=state.history_after_journal_id) as servers:
                return await perform(servers)

    async def select_model(self, conversation, provider, model, get_group=None):
        return await self._model_control(conversation, selection=(provider, model), get_group=get_group)

    async def use_default_model(self, conversation, get_group=None):
        return await self._model_control(conversation, default=True, get_group=get_group)

    async def _model_control(self, conversation, *, selection=None, default=False, get_group=None):
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        if identity in self._reset_barriers:
            raise ResetError('会话正在重置，请稍后查看或切换模型。')
        async with self._lock(conversation):
            if identity in self._reset_barriers:
                raise ResetError('会话正在重置，请稍后查看或切换模型。')
            state = await asyncio.to_thread(self.state.get, *identity)
            if state is not None and state.pending_upper_cursor is not None:
                raise PendingTurnError('存在待核对的DSH轮次，请先核对再管理模型。')
            previous = await asyncio.to_thread(self.state.get_model_route, *identity)
            console = getattr(self, "console_store", None)
            if console is not None:
                settings = session_settings(console, conversation)
                previous = (settings["model"]["provider"], settings["model"]["model"])
            async def perform(servers=None):
                nonlocal state
                if state is None:
                    sid = await self._dsh_call(self.dsh.create_session, *(() if servers is None else (servers,)))
                    try:
                        state = await asyncio.to_thread(self.state.get_or_create, *identity, sid)
                    except BaseException:
                        await self._dsh_call(self.dsh.close_session, sid)
                        raise
                args = (state.session_id,) if servers is None else (state.session_id, servers)
                if selection is None and not default:
                    view = await self._dsh_call(lambda: self.dsh.model_status(*args, model_route=previous))
                else:
                    target = ((self.dsh.settings['provider'], self.dsh.settings['model'])
                              if default else selection)
                    if default and console is not None:
                        model = console.read()["defaults"]["model"]
                        if model is None:
                            raise ValueError("请先配置默认模型。")
                        target = (model["provider"], model["model"])
                    try:
                        view = await self._dsh_call(lambda: self.dsh.select_model(state.session_id, *target,
                                                    *(() if servers is None else (servers,)), previous_route=previous))
                        if console is not None:
                            snapshot = console.read()
                            key = console.session_key(dict(zip(
                                ("platform_id", "bot_id", "conversation_key"), identity)))
                            configured = dict(snapshot["sessions"][key]["settings"])
                            configured["model"] = {"provider": target[0], "model": target[1]}
                            console.update_session(*identity, configured, expected_revision=snapshot["revision"])
                        # Console mode has one durable authority. A second write
                        # to the legacy route table could fail after commit and
                        # leave the native route rolled back against new settings.
                        if console is None:
                            await asyncio.to_thread(self.state.set_model_route, *identity,
                                                    None if default else target)
                    except BaseException:
                        # A lost confirmation or failed durable publication must
                        # not silently change the model used for the next turn.
                        await self._dsh_call(lambda: self.dsh.model_status(*args, model_route=previous))
                        raise
                return {**view, 'snapshot': console is not None,
                        'overridden': (previous is not None if selection is None and not default else not default)}
            if self.mcp is None:
                return await perform()
            async def no_tool_send(_reply, **_metadata):
                raise RuntimeError('model configuration cannot send Agent tool messages')
            options = {}
            floor = getattr(state, 'history_after_journal_id', 0) if state is not None else 0
            if floor:
                options['history_after'] = floor
            async with self.mcp.turn(conversation, no_tool_send, get_group, **options) as servers:
                return await perform(servers)

    async def handle(
        self,
        conversation,
        inbound_message_id: str,
        send: Callable[[str], Awaitable[None]],
        get_group=None,
        files=None,
        vision=None,
        messaging=None,
        memes=None,
        plugins=None,
    ) -> str | None:
        """Run a turn once, then send through AstrBot while holding this chat's lock."""
        trace = self.audit.trace(conversation, inbound_message_id, self.domain) if self.audit else None
        started = time.monotonic()
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        self._ensure_enabled(identity)
        if trace:
            trace.emit("turn/queued")
        self._queued += 1
        waiting = True
        lifetime = _SendLifetime()
        notice_task = None
        if files is not None:
            original_file_send = files.send

            async def guarded_file_send(*args, _send=original_file_send, **kwargs):
                self._delivery_enter(identity, lifetime)
                try:
                    return await _send(*args, **kwargs)
                finally:
                    self._delivery_exit(identity)

            files.send = guarded_file_send
        with trace_scope(trace):
            try:
                lock = self._lock(conversation)
                active = self._active_runs.get(identity)
                notice_key = (identity, str(inbound_message_id))
                if (lock.locked() and active is not None and not active.finished
                        and inbound_message_id and active.inbound_message_id != inbound_message_id
                        and notice_key not in self._busy_notices
                        and identity not in self._reset_barriers):
                    # A receipt is independent of the queued model turn. Reserve
                    # before awaiting so redelivery cannot duplicate the notice;
                    # an uncertain send is never retried automatically.
                    self._busy_notices[notice_key] = None
                    while len(self._busy_notices) > self.BUSY_NOTICE_LIMIT:
                        self._busy_notices.popitem(last=False)
                    async def busy_notice():
                        entered = False
                        try:
                            current, matches = await asyncio.gather(
                                asyncio.to_thread(self.state.get, *identity),
                                asyncio.to_thread(self.journal.by_message_id,
                                                  conversation.platform_id,
                                                  conversation.bot_id, inbound_message_id),
                            )
                            wake = next((row for row in matches
                                         if row.message.conversation_key == conversation.key
                                         and row.message.direction == "inbound"), None)
                            floor = max(getattr(current, "last_seen_journal_id", 0),
                                        getattr(current, "history_after_journal_id", 0))
                            if (wake is None or wake.journal_id <= floor
                                    or self._active_runs.get(identity) is not active
                                    or active.finished or not lock.locked()):
                                return
                            self._delivery_enter(identity, lifetime)
                            entered = True
                            async def receipt_send(text):
                                if messaging is not None and conversation.key.startswith("group:"):
                                    return await messaging.send_origin(
                                        text, mention_user_ids=[wake.message.actor_id],
                                        reply_to_journal_id=None)
                                return await send(text)
                            operation = (trace.send(receipt_send, self.BUSY_NOTICE, "control")
                                         if trace else receipt_send(self.BUSY_NOTICE))
                            await asyncio.wait_for(operation, self.BUSY_NOTICE_TIMEOUT_SECONDS)
                        except Exception:
                            # A receipt failure must not discard the real request.
                            pass
                        finally:
                            if entered:
                                self._delivery_exit(identity)

                    notice_task = asyncio.create_task(busy_notice())
                while True:
                    while True:
                        await lock.acquire()
                        try:
                            if notice_task is not None:
                                await notice_task
                                notice_task = None
                            self._ensure_enabled(identity)
                        except BaseException:
                            lock.release()
                            raise
                        barrier = self._reset_barriers.get(identity)
                        if barrier is None:
                            break
                        lock.release()
                        await barrier.done.wait()
                    if waiting:
                        self._queued -= 1
                        waiting = False
                    if trace:
                        trace.emit("turn/start", queue_ms=(time.monotonic() - started) * 1000)

                    async def final_send(text):
                        return await trace.send(send, text, "final") if trace else await send(text)

                    async def tool_send(text, **metadata):
                        validate_reply(text)
                        self._delivery_enter(identity, lifetime)
                        try:
                            async def sender(value):
                                if messaging is not None:
                                    return await messaging.send_origin(value, **metadata)
                                return await send(value)
                            return await trace.send(sender, text, "mcp") if trace else await sender(text)
                        finally:
                            self._delivery_exit(identity)

                    preempted = None
                    try:
                        if self.mcp is not None:
                            if files is not None:
                                files.trace = trace
                            options = {'files': files} if files is not None else {}
                            if vision is not None:
                                options['vision'] = vision
                            if messaging is not None:
                                options['messaging'] = messaging
                            if memes is not None or plugins is not None:
                                async def invoke_capability(operation):
                                    self._delivery_enter(identity, lifetime)
                                    try:
                                        with trace_scope(trace):
                                            return await operation()
                                    finally:
                                        self._delivery_exit(identity)
                                options.update(memes=memes, plugins=plugins, invoke=invoke_capability)
                            current = await asyncio.to_thread(self.state.get, *identity)
                            history_after = max(0, getattr(current, "history_after_journal_id", 0))
                            if history_after:
                                options['history_after'] = history_after
                            async with self.mcp.turn(conversation, tool_send, get_group, **options) as servers:
                                visual_options = {'vision': vision} if vision is not None else {}
                                if messaging is not None:
                                    visual_options['messaging'] = messaging
                                result = await self._handle_locked(
                                    conversation, inbound_message_id, final_send, servers,
                                    send_lifetime=lifetime, **visual_options)
                        else:
                            visual_options = {'vision': vision} if vision is not None else {}
                            if messaging is not None:
                                visual_options['messaging'] = messaging
                            result = await self._handle_locked(
                                conversation, inbound_message_id, final_send,
                                send_lifetime=lifetime, **visual_options)
                    except _ResetPreempted as error:
                        preempted = error.barrier
                    finally:
                        lock.release()
                    if preempted is not None:
                        self._queued += 1
                        waiting = True
                        await preempted.done.wait()
                        continue
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
                lifetime.active = False
                if notice_task is not None:
                    notice_task.cancel()
                    await asyncio.gather(notice_task, return_exceptions=True)
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

    async def _handle_locked(self, conversation, inbound_message_id, send, servers=None, *, vision=None,
                             send_lifetime=None, messaging=None):
        mcp_args = () if servers is None else (servers,)
        identity = (conversation.platform_id, conversation.bot_id, conversation.key)
        self._ensure_enabled(identity)
        barrier = self._reset_barriers.get(identity)
        if barrier is not None:
            raise _ResetPreempted(barrier)
        state = await asyncio.to_thread(self.state.get, *identity)
        if state is None:
            fault = await asyncio.to_thread(self.state.get_pre_session_fault, *identity)
            if fault is not None:
                raise PendingTurnError(
                    f"DSH session creation remains paused at Journal {fault['failed_upper_cursor']}"
                )
            try:
                session_id = await self._dsh_call(self.dsh.create_session, *mcp_args)
            except Exception as error:
                matches = await asyncio.to_thread(
                    self.journal.by_message_id, conversation.platform_id, conversation.bot_id,
                    inbound_message_id,
                )
                wake_row = next((row for row in matches if
                                 row.message.conversation_key == conversation.key and
                                 row.message.direction == "inbound" and
                                 row.message.message_id == inbound_message_id), None)
                if wake_row is not None:
                    await asyncio.to_thread(self.state.record_pre_session_fault, *identity,
                                            wake_row.journal_id, "infrastructure", "SESSION_CREATE_FAILED")
                    raise PendingTurnError(
                        f"DSH could not create a session at Journal {wake_row.journal_id}"
                    ) from error
                raise
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
        history_floor = max(0, getattr(state, "history_after_journal_id", 0))
        # End this turn at its own wake row. A later ordinary message may
        # already be queued while we wait for the lock; consuming it here
        # would silently swallow that message's separate wake attempt.
        upper = wake.journal_id
        if self.mcp is not None:
            self.mcp.set_history_through(conversation, upper)
        if messaging is not None:
            messaging.history_through = upper
        rows = await self._delta(conversation, state.last_seen_journal_id, upper)
        if not any(row.journal_id == wake.journal_id for row in rows):
            raise RuntimeError("inbound wake message is absent from journal delta")
        resume_through = 0
        if getattr(self, "console_store", None) is not None:
            key = self.console_store.session_key(dict(zip(
                ("platform_id", "bot_id", "conversation_key"), identity)))
            resume_through = self.console_store.read()["sessions"][key].get("resume_through_journal_id", 0)
        # Program-managed recovery uses the existing row/UTF-8 byte bounds.
        # These bound the new Journal input, not the retained native context;
        # byte size must not be described as an exact model token count.
        prompt = self._prompt(conversation, rows, wake.journal_id, state.last_seen_journal_id,
                              history_floor=history_floor, resume_through_journal_id=resume_through)
        image_blocks = []
        if vision is not None:
            # Automatically load only images attached to this wake and the
            # canonical message it explicitly replies to. Other history stays
            # searchable and is viewed through history_read_image on demand.
            visible_ids = {json.loads(line)['journal_id'] for line in prompt.splitlines()[1:]}
            visual_rows = []
            reply_row = None
            reply_to = getattr(wake.message, 'reply_to', None)
            if isinstance(reply_to, str) and reply_to:
                referenced = await asyncio.to_thread(
                    self.journal.by_message_id, conversation.platform_id,
                    conversation.bot_id, reply_to,
                )
                reply_row = next((row for row in reversed(referenced)
                                  if row.message.conversation_key == conversation.key
                                  and history_floor < row.journal_id <= upper), None)
                if reply_row is not None:
                    visual_rows.append(reply_row)
            visual_rows.append(wake)
            auto_visual_ids = {row.journal_id for row in visual_rows}
            visual = await vision.prepare(visual_rows, auto_visual_ids)
            while visual['images'] or visual['errors'] or visual.get('omitted'):
                # The binary budget is independent of the text budget. Keep
                # per-image outcomes bounded even for attachment-heavy rows.
                visual_header = {}
                for source, target in (('references', 'images'), ('errors', 'unavailable'), ('omitted', 'omitted')):
                    entries = visual.get(source, [])
                    visual_header[target] = [
                        {key: item[key] for key in ('journal_id', 'segment_index', 'direction', 'status', 'reason')
                         if key in item}
                        for item in entries[-20:]
                    ]
                    visual_header[target + '_count'] = len(entries)
                prompt = self._prompt(conversation, rows, wake.journal_id, state.last_seen_journal_id,
                                      visual_input=visual_header, visible_ids=visible_ids,
                                      history_floor=history_floor, resume_through_journal_id=resume_through)
                final_ids = {json.loads(line)['journal_id'] for line in prompt.splitlines()[1:]}
                if final_ids == visible_ids:
                    break
                # Shrink monotonically without downloading images again. The
                # final text and binary blocks refer to the same canonical rows.
                visible_ids = final_ids
                retained_visual_ids = visible_ids | ({reply_row.journal_id} if reply_row is not None else set())
                pairs = [(block, ref) for block, ref in zip(visual['images'], visual['references'])
                         if ref['journal_id'] in retained_visual_ids]
                visual['images'] = [block for block, _ in pairs]
                visual['references'] = [ref for _, ref in pairs]
                for key in ('errors', 'omitted'):
                    visual[key] = [item for item in visual.get(key, []) if item['journal_id'] in retained_visual_ids]
            # A dropped row may have held the only image. Rebuild that final
            # text window too, retaining the original incomplete-history flag.
            if not (visual['images'] or visual['errors'] or visual.get('omitted')):
                prompt = self._prompt(conversation, rows, wake.journal_id, state.last_seen_journal_id,
                                      visible_ids=visible_ids, history_floor=history_floor,
                                      resume_through_journal_id=resume_through)
            image_blocks = visual['images']
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
        barrier = self._reset_barriers.get(identity)
        if barrier is not None:
            raise _ResetPreempted(barrier)
        self._ensure_enabled(identity)
        await asyncio.to_thread(self.state.begin_turn, *identity, state.session_id, upper)
        # set_enabled may run while begin_turn is persisted in its worker thread.
        # Preserve that pending marker, but do not start the model after disable.
        self._ensure_enabled(identity)
        if trace:
            trace.emit("dsh/start", journal_id=upper)
        # Errors before a confirmed DSH result leave a durable pending marker:
        # the stdio protocol cannot prove whether a lost call was accepted.
        run_args = (state.session_id, prompt, *mcp_args)
        if self.trusted_sources:
            if not mcp_args:
                run_args += (None,)
            run_args += (trusted_source,)
        selected_route = (await asyncio.to_thread(self.state.get_model_route, *identity)
                          if hasattr(self.state, 'get_model_route') else None)
        if getattr(self, "console_store", None) is not None:
            settings = session_settings(self.console_store, conversation)
            selected_route = (settings["model"]["provider"], settings["model"]["model"])
        run_options = {'images': image_blocks} if image_blocks else {}
        if selected_route is not None:
            run_options['model_route'] = selected_route
        # No await between this check and active-run registration. A disable
        # operation on this event loop either sees the registered run to cancel,
        # or has already published its admission barrier.
        self._ensure_enabled(identity)
        reserve = getattr(self.dsh, "reserve_turn", None)
        cancel_token = reserve(state.session_id) if callable(reserve) else None
        if cancel_token is not None:
            run_options["cancel_token"] = cancel_token
        active = _ActiveRun(state.session_id, cancel_token=cancel_token,
                            inbound_message_id=str(inbound_message_id),
                            conversation=conversation, requester_id=str(wake.message.actor_id),
                            messaging=messaging, pending_cursor=upper)
        self._active_runs[identity] = active

        def run():
            try:
                reply = self.dsh.run(*run_args, **run_options)
                active.succeeded = True
                return reply
            except BaseException as error:
                active.error = error
                raise
            finally:
                with active.lock:
                    active.finished = True
                active.done.set()

        try:
            reply = await self._dsh_call(run)
        except DshModelTurnFailure as error:
            # The trusted native terminal receipt proves the model failed
            # before any tool was called. Consume only this wake so later
            # requests remain available; never replay it automatically.
            await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
            raise ModelTurnError(error.code, tools_completed=bool(error.tools_finished)) from error
        except DshTurnFailure as error:
            kind = "infrastructure" if error.kind == "infrastructure" else "unknown"
            await asyncio.to_thread(self.state.mark_pending_fault, *identity, state.session_id,
                                    upper, kind, error.code, error.fault_details)
            raise PendingTurnError(
                f"DSH {kind} failure at Journal {upper}; review before recovery"
            ) from error
        except DshTurnCancelled:
            await asyncio.to_thread(self.state.mark_pending_fault, *identity, state.session_id,
                                    upper, "cancelled", "CANCELLED")
            raise
        except Exception as error:
            # No structured receipt means the remote execution state cannot
            # safely be inferred from a missing reply or tool event.
            await asyncio.to_thread(self.state.mark_pending_fault, *identity, state.session_id,
                                    upper, "unknown", "NO_TERMINAL_RECEIPT",
                                    {"confirmed_tools": [], "unknown_tools": []})
            raise PendingTurnError(
                f"DSH outcome is unknown at Journal {upper}; review before recovery"
            ) from error
        finally:
            if self._active_runs.get(identity) is active:
                del self._active_runs[identity]
        # Validate the complete final message before delivery or legacy splitting.
        # Never select a guessed "last sentence" from contaminated model text.
        try:
            validate_reply(reply)
        except ReplyContentError as error:
            await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
            if trace:
                trace.emit("delivery/content_rejected", code=error.code, reply_chars=len(reply))
            raise
        try:
            self._delivery_enter(identity, send_lifetime)
        except ResetError:
            if identity in self._reset_barriers:
                # Native execution succeeded, but reset owns the conversation
                # now. Consume only this known-complete wake and suppress its
                # stale reply so reset can rotate without leaving unknown work.
                await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
                if trace:
                    trace.emit("delivery/reset_suppressed", journal_id=upper)
                return None
            raise
        try:
            try:
                # The Agent chooses message boundaries: explicit MCP sends are
                # earlier utterances, and final text is exactly one last message.
                await send(reply)
            except Exception as exc:
                # The model turn is known to have completed. Consume only this
                # wake row so a failed QQ delivery does not block later requests.
                # Do not retry: the platform may have accepted the message before
                # its receipt was lost. Cancellation and DSH failures stay pending.
                await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
                if trace:
                    trace.emit("delivery/final_failed", journal_id=upper, error_type=type(exc).__name__)
                raise FinalReplyDeliveryError(exc) from exc
            await asyncio.to_thread(self.state.complete_turn, *identity, state.session_id, upper)
            return reply
        finally:
            self._delivery_exit(identity)
