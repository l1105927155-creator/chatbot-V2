"""Route unhandled QQ text through the V2 journal and pinned DSH runtime."""

from __future__ import annotations

import os
import asyncio
import importlib.util
from sys import maxsize
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .dsh_client import DshTurnCancelled, start_client
from .routing import should_route
from .service import ChatService, PendingTurnError, RecoveryError
from .state import StateStore
from .mcp_server import McpServer
from .trust_domain import Owner, classify, parse_owners
from .persona_state import PersonaStateStore
from .scene_state import SceneStateStore
from .runtime_audit import AuditStore, OneBotAudit, CURRENT_TRACE


@star.register(
    "v2_dsh_router",
    "chatbot-V2",
    "Route unhandled QQ text to the V2 DSH conversation session.",
    "0.1.0",
)
class V2DshRouter(star.Star):
    async def initialize(self) -> None:
        self._closing = False
        self._initialized = False
        self._active = set()
        # The plugin is symlinked into AstrBot's data/plugins directory by V2.
        repo = Path(__file__).resolve().parents[2]
        runtime = repo / ".runtime"
        from data.plugins.v2_journal.journal import JournalStore

        journal_dir = Path(star.StarTools.get_data_dir("v2_journal"))
        own_dir = Path(star.StarTools.get_data_dir("v2_dsh_router"))
        self.audit = AuditStore(own_dir / "audit", self.logger)
        self.onebot_audit = OneBotAudit()
        self.owners = parse_owners(os.environ.get("V2_QQ_OWNERS", "[]"))
        self._verify_workspace = None
        if self.owners:
            spec = importlib.util.spec_from_file_location(
                "v2_health_owner_volume", repo / "dsh/plugins/agent-profile/owner_volume.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._verify_workspace = module.verified_workspace
        # Validate both state stores before starting either ACP process.
        journal = JournalStore(journal_dir / "journal.sqlite3")
        chat_state = StateStore(own_dir / "state-chat.sqlite3")
        owner_state = StateStore(own_dir / "state-owner.sqlite3") if self.owners else None
        self.mcp = McpServer(journal, logger=self.logger, health=self.health)
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
                audit=self.audit,
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
                    audit=self.audit,
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
        self._initialized = True

    def _onebot_connected(self):
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return False
        for platform in manager.get_insts():
            meta = platform.meta()
            if meta.name != "aiocqhttp":
                continue
            client = platform.get_client()
            api = getattr(client, "_wsr_api_clients", {})
            events = getattr(client, "_wsr_event_clients", set())
            expected = {owner.bot_id for owner in self.owners if owner.platform_id == meta.id}
            if (isinstance(api, dict) and isinstance(events, set) and events and
                    (bool(set(api) & expected) if expected else bool(api))):
                return True
        return False

    async def health(self):
        """Operational readiness only; no Session creation or model request."""
        journal_ready, state_ready, volume_ready = False, True, not self.owners
        pending, quarantined = 0, 0
        try:
            journal_ready = await asyncio.to_thread(self.mcp.journal.check_health)
        except Exception:
            pass
        for service in self.services.values():
            try:
                counts = await asyncio.to_thread(service.state.health_counts)
                pending += counts["pending"]
            except Exception:
                state_ready = False
        expected = {"chat", "owner"} if self.owners else {"chat"}
        acp_ready = set(self.clients) == expected
        for client in self.clients.values():
            try:
                status = client.health()
                acp_ready = acp_ready and status["ready"]
                quarantined += status["quarantined_sessions"]
            except Exception:
                acp_ready = False
        if self.owners and "owner" in self.clients:
            try:
                fd = await asyncio.to_thread(self._verify_workspace, self.clients["owner"].settings["workspace"])
                os.close(fd)
                volume_ready = True
            except Exception:
                pass
        connected = self._onebot_connected()
        core = bool(self._initialized and not self._closing and journal_ready and state_ready
                    and acp_ready and volume_ready)
        return {"pid": os.getpid(), "core_ready": core, "ready": core and connected,
                "closing": self._closing, "onebot_connected": connected, "journal_ready": journal_ready,
                "state_ready": state_ready, "acp_ready": bool(acp_ready), "owner_volume_ready": volume_ready,
                "active_turns": len(self._active), "queued": sum(s._queued for s in self.services.values()),
                "pending_conversations": pending, "quarantined_sessions": quarantined,
                "degraded": bool(quarantined or self.audit.write_errors)}

    @filter.command("v2agent")
    async def configure_agent(self, event: AstrMessageEvent, kind: str = "", value: str = ""):
        """Select this conversation's persona or skill scene from trusted host state."""
        event.set_extra("v2_deterministic_handled", True)
        if self._closing:
            return
        from data.plugins.v2_journal.journal import message_from_onebot
        trace = None
        trace_token = None
        task = asyncio.current_task()
        self._active.add(task)

        async def reply(text):
            async def send(value):
                self.onebot_audit.bind(getattr(event, "bot", None))
                await event.send(MessageChain().message(value))
            if trace:
                await trace.send(send, text, "control")
            else:
                await send(text)

        try:
            raw = getattr(event.message_obj, "raw_message", None)
            message = message_from_onebot(event.get_platform_id(), raw)
            domain = classify(message, self.owners)
            if not message.message_id:
                raise ValueError("control message requires canonical platform message ID")
            if message.direction != "inbound" or Owner(
                    message.platform_id, message.bot_id, message.actor_id) not in self.owners:
                await reply("仅 Bot 所有者可以使用 Agent 控制命令。")
                return
            if message.conversation_key not in self.allowed_conversations:
                await reply("当前会话尚未启用 Agent。")
                return
            trace = self.audit.trace(message.conversation, message.message_id, domain)
            trace_token = CURRENT_TRACE.set(trace)
            trace.emit("control/request", control=kind)
            choices = {"persona": PersonaStateStore.CHOICES, "scene": SceneStateStore.CHOICES}
            if kind == "metrics" and not value:
                metrics = self.services[domain].metrics()
                counts = metrics["events"]
                await reply("当前运行周期：排队 " + str(metrics["queued"])
                            + "；运行会话 " + str(metrics["active_chats"])
                            + "；DSH请求 " + str(counts["dsh/dispatched"])
                            + "；完成 " + str(counts["turn/completed"])
                            + "；失败/暂停 " + str(counts["turn/failed"])
                            + "；平均耗时 " + (str(metrics["mean_turn_ms"]) + "ms"
                                               if metrics["mean_turn_ms"] is not None else "暂无")
                            + "；审计写入错误 " + str(metrics["audit_write_errors"]) + "。")
                return
            if kind == "recover":
                if not value.isascii() or not value.isdecimal() or len(value) > 19 or int(value) <= 0:
                    await reply("核对上一轮执行和发送结果后，使用 /v2agent recover <待核对 Journal 编号>；该操作跳过旧输入，不重试。")
                    return
                resolved = await self.services[domain].resolve_pending(
                    message.conversation, int(value), get_group=event.get_group,
                )
                await reply("已解除暂停并跳过截至 Journal " + str(resolved.last_seen_journal_id)
                            + " 的待核对输入；未重试旧操作，也未确认旧回复发送成功。可以发送新的请求。")
                return
            if kind == "cancel" and not value:
                accepted = await self.services[domain].cancel(message.conversation)
                await reply("已请求取消当前执行；请用 /v2agent status 核对状态。已发生的操作不会撤销。"
                            if accepted else "当前没有可取消的模型执行；请用 /v2agent status 查看会话状态。")
                return
            if kind == "status" and not value:
                status = await self.services[domain].status(message.conversation)
                labels = {"not-started": "尚未开始", "running": "执行中", "pending": "结果待核对", "idle": "空闲"}
                await reply("Agent：" + labels[status["state"]]
                            + "；人格：" + status["persona"] + "；场景：" + status["scene"]
                            + "；模式：" + ("Owner 受限操作" if domain == "owner" else "安全聊天")
                            + ("；待核对 Journal：" + str(status["pending_upper_cursor"])
                               + "。核对执行和发送结果后，可用 /v2agent recover "
                               + str(status["pending_upper_cursor"]) + " 跳过该轮并解除暂停，系统不会重试旧输入。"
                               if status["state"] == "pending" else "。"))
                return
            if kind in choices and value == "list":
                await reply(("可用人格：" if kind == "persona" else "可用场景：")
                            + "、".join(sorted(choices[kind])))
                return
            if kind not in choices or value not in choices[kind]:
                await reply("用法：/v2agent persona <" + "|".join(sorted(PersonaStateStore.CHOICES))
                            + "|list>；/v2agent scene <" + "|".join(sorted(SceneStateStore.CHOICES))
                            + "|list>；/v2agent status；/v2agent metrics；/v2agent cancel；/v2agent recover <待核对 Journal 编号>")
                return
            async def selected(_state):
                label = "人格" if kind == "persona" else "场景"
                await reply(label + "已切换为 " + value + "，下轮生效。")

            await self.services[domain].select_profile(
                message.conversation, kind, value, selected, get_group=event.get_group,
            )
        except RecoveryError as exc:
            await reply(str(exc))
        except PendingTurnError:
            await reply("当前会话有未确认的执行结果，请先恢复会话后再切换。")
        except Exception:
            self.logger.exception("V2 Agent control command failed")
            await reply("Agent 命令未完成，请检查 Bot 运行日志。")
        finally:
            if trace_token is not None:
                CURRENT_TRACE.reset(trace_token)
            self._active.discard(task)

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
                self.onebot_audit.bind(getattr(event, "bot", None))
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
        except DshTurnCancelled:
            self.logger.info("V2 DSH cancellation confirmed for QQ message %s; outcome pending review",
                             raw.get("message_id"))
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
        if hasattr(self, "onebot_audit"):
            self.onebot_audit.close()
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
