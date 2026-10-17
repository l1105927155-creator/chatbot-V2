"""Route unhandled QQ text through the V2 journal and pinned DSH runtime."""

from __future__ import annotations

import os
import asyncio
import importlib.util
import sys
from sys import maxsize
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .dsh_client import DshTurnCancelled, start_client
from .routing import should_route
from .service import ChatService, PendingTurnError, RecoveryError, FinalReplyDeliveryError
from .delivery_error import notify_delivery_error
from .state import StateStore, migration_needed
from .mcp_server import McpServer
from .trust_domain import Owner, classify, parse_owners
from .persona_state import PersonaStateStore
from .scene_state import SceneStateStore
from .runtime_audit import AuditStore, OneBotAudit, CURRENT_TRACE


def _private_shell_environment() -> dict[str, str]:
    """Capture only non-secret host routing needed by Owner's Bash."""
    names = ("PATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy", "NODE_USE_ENV_PROXY")
    return {name: os.environ[name] for name in names if name in os.environ}


def _group_runtime_module(repo: Path):
    path = repo / "dsh/plugins/agent-profile/group_volume.py"
    if not path.is_file():
        raise RuntimeError("group sandbox volume runtime is unavailable")
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("v2_group_volume_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        contract = os.environ.get("V2_EXECUTION_CONTRACT")
        if contract != "2":
            raise RuntimeError("V2_EXECUTION_CONTRACT=2 is required; refusing legacy execution configuration")
        self.repo = repo
        self.runtime_root = runtime
        self.migrate_enabled = os.environ.get("V2_EXECUTION_MIGRATE") == "1"
        self.allowed_conversations = frozenset(
            part.strip() for part in os.environ.get("V2_QQ_ALLOWED_CONVERSATIONS", "").split(",")
            if part.strip()
        )
        from data.plugins.v2_journal.journal import JournalStore

        journal_dir = Path(star.StarTools.get_data_dir("v2_journal"))
        own_dir = Path(star.StarTools.get_data_dir("v2_dsh_router"))
        self.audit = AuditStore(own_dir / "audit", self.logger)
        self.onebot_audit = OneBotAudit()
        self.owners = parse_owners(os.environ.get("V2_QQ_OWNERS", "[]"))
        journal = JournalStore(journal_dir / "journal.sqlite3")
        # Keep the old safe-chat mapping intact. Owner-host and each group get
        # fresh generation-specific state files and native Session IDs.
        chat_state = StateStore(own_dir / "state-chat.sqlite3")
        owner_state = StateStore(own_dir / "state-owner-host.sqlite3") if self.owners else None
        self.mcp = McpServer(journal, logger=self.logger, health=self.health)
        self.clients = {}
        self.services = {}
        self._group_runtimes = {}
        self._group_init_locks = {}
        self.legacy_chat_state = chat_state
        self.legacy_owner_state = StateStore(own_dir / "state-owner.sqlite3") if self.owners else None
        self.chat_state = chat_state
        self.owner_state = owner_state
        try:
            await self.mcp.start(port=int(os.environ.get("V2_MCP_PORT", "6210")))
            self.clients["chat"] = await start_client(
                dsh_bin=runtime / "dsh-runtime/node_modules/.bin/dsh",
                dsh_home=runtime / "dsh-home-chat",
                workspace=runtime / "dsh-workspace-chat",
                patch=repo / "dsh/profile/v2-agent-group.patch.yml",
                instructions=repo / "dsh/profile/agent-group.md",
                execution_profile="safe-chat",
            )
            self.services["chat"] = ChatService(
                journal, chat_state, self.clients["chat"], self.mcp,
                audit=self.audit, domain="chat", execution_profile="safe-chat",
            )
            if self.owners:
                self.clients["owner"] = await start_client(
                    dsh_bin=runtime / "dsh-runtime/node_modules/.bin/dsh",
                    dsh_home=runtime / "dsh-home-owner-host",
                    workspace=Path(os.environ.get("V2_OWNER_WORKDIR", str(repo))).resolve(strict=True),
                    patch=repo / "dsh/profile/v2-agent-owner.patch.yml",
                    instructions=repo / "dsh/profile/agent-owner.md",
                    enable_authorization=True,
                    execution_profile="owner-host",
                    owner_shell_env=_private_shell_environment(),
                )
                self.services["owner"] = ChatService(
                    journal, owner_state, self.clients["owner"], self.mcp,
                    trusted_sources=True, audit=self.audit, domain="owner",
                    execution_profile="owner-host",
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
        if not self.allowed_conversations:
            self.logger.warning("V2 DSH router is idle: V2_QQ_ALLOWED_CONVERSATIONS is empty")
        # Materialize admitted groups at startup so readiness includes every
        # configured execution volume. Incoming identities are still checked
        # against their canonical platform and Bot before using these clients.
        try:
            if self.owners:
                owner_pairs = {(owner.platform_id, owner.bot_id) for owner in self.owners}
                for key in sorted(self.allowed_conversations):
                    if not key.startswith("group:"):
                        continue
                    if len(owner_pairs) != 1:
                        raise RuntimeError("cannot bind an admitted group to one platform/Bot identity")
                    platform_id, bot_id = next(iter(owner_pairs))
                    await self._ensure_group_service(platform_id, bot_id, key.removeprefix("group:"))
            elif any(key.startswith("group:") for key in self.allowed_conversations):
                raise RuntimeError("admitted group execution requires a configured platform/Bot identity")
            await self._migrate_legacy_mappings()
        except BaseException as startup_error:
            try:
                await self._close_runtime()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "V2 execution-domain startup and cleanup both failed",
                    [startup_error, cleanup_error],
                ) from startup_error
            raise
        self._initialized = True

    def _migration_marker(self) -> Path:
        return self.runtime_root / "execution-migration-approved.json"

    def _migration_is_authorized(self) -> bool:
        marker = self._migration_marker()
        if os.environ.get("V2_EXECUTION_MIGRATE") != "1" or marker.is_symlink():
            return False
        try:
            info = marker.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or not marker.is_file():
                return False
            import json, re
            data = json.loads(marker.read_text(encoding="utf-8"))
            return (isinstance(data, dict) and data.get("version") == 1
                    and type(data.get("created_at_unix")) is int
                    and isinstance(data.get("backup_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", data["backup_sha256"]) is not None)
        except (OSError, ValueError, TypeError):
            return False

    async def _migrate_legacy_mappings(self) -> None:
        """Create fresh native sessions and copy only clean cursors/settings."""
        candidates = []
        for owner in self.owners:
            key = "private:" + owner.user_id
            old = self.legacy_owner_state.get(owner.platform_id, owner.bot_id, key)
            if old is not None:
                candidates.append((old, self.owner_state, self.services["owner"],
                                   self.runtime_root / "dsh-home-owner"))
        for name, service in self.services.items():
            if name.startswith("group:"):
                state = service.state.get
                group_id = self._group_runtimes[name]["group_id"]
                identity = self._group_runtimes[name]
                old = self.legacy_chat_state.get(identity["platform_id"], identity["bot_id"], "group:" + group_id)
                if old is not None:
                    candidates.append((old, service.state, service, self.runtime_root / "dsh-home-chat"))
        authorized = self._migration_is_authorized()
        needed = [(row, target, service, old_home) for row, target, service, old_home in candidates
                  if migration_needed(row, target.get(row.platform_id, row.bot_id, row.conversation_key),
                                      authorized=authorized)]
        for row, target, service, old_home in needed:
            await self._migrate_one_mapping(row, target, service, old_home)
        if self._migration_is_authorized():
            self._migration_marker().unlink(missing_ok=True)

    async def _migrate_one_mapping(self, row, target, service, old_home: Path) -> None:
        if row.pending_upper_cursor is not None:
            raise RuntimeError("pending legacy conversation blocks execution migration; review recovery first")
        conversation = self._conversation_type(row.platform_id, row.bot_id, row.conversation_key)
        async def no_tool_send(_text):
            raise RuntimeError("migration cannot send chat messages")
        async with self.mcp.turn(conversation, no_tool_send) as servers:
            new_sid = await asyncio.to_thread(service.dsh.create_session, servers)
        persona_root = old_home / "agent-bot-personas"
        scene_root = old_home / "agent-bot-scenes"
        old_personas = (PersonaStateStore(persona_root).current(row.session_id)
                        if persona_root.is_dir() else {"persona": "default"})
        old_scenes = (SceneStateStore(scene_root).current(row.session_id)
                      if scene_root.is_dir() else {"scene": "casual-chat"})
        if old_personas["persona"] != "default":
            await asyncio.to_thread(service.dsh.select_persona, new_sid, old_personas["persona"])
        if old_scenes["scene"] != "casual-chat":
            await asyncio.to_thread(service.dsh.select_scene, new_sid, old_scenes["scene"])
        await asyncio.to_thread(target.import_mapping, row, new_sid)

    @staticmethod
    def _conversation_type(platform_id, bot_id, key):
        from data.plugins.v2_journal.journal import Conversation
        return Conversation(platform_id, bot_id, key)

    async def _ensure_group_service(self, platform_id: str, bot_id: str, group_id: str):
        if not all(isinstance(value, str) and value for value in (platform_id, bot_id, group_id)):
            raise ValueError("group execution identity is incomplete")
        module = _group_runtime_module(self.repo)
        group_storage_root = self.runtime_root / "group-storage"
        paths = module.paths(group_storage_root, platform_id, bot_id, group_id)
        name = "group:" + paths["key"]
        existing = self.services.get(name)
        if existing is not None:
            runtime = self._group_runtimes[name]
            if (runtime["platform_id"], runtime["bot_id"], runtime["group_id"]) != (platform_id, bot_id, group_id):
                raise RuntimeError("group workspace hash collision")
            return existing
        lock = self._group_init_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if name in self.services:
                return self.services[name]
            state_path = Path(star.StarTools.get_data_dir("v2_dsh_router")) / ("state-group-" + paths["key"] + ".sqlite3")
            state = StateStore(state_path)
            legacy = self.legacy_chat_state.get(platform_id, bot_id, "group:" + group_id)
            target = state.get(platform_id, bot_id, "group:" + group_id)
            dynamic_migration = self._initialized and migration_needed(
                legacy, target, authorized=self._migration_is_authorized())
            await asyncio.to_thread(module.prepare_group, group_storage_root, platform_id, bot_id, group_id)
            workspace = Path(paths["workspace"]).resolve(strict=True)
            self._group_runtimes[name] = {"platform_id": platform_id, "bot_id": bot_id,
                                          "group_id": group_id, "workspace": workspace,
                                          "volume_module": module, "volume_key": paths["key"]}
            dsh_home = self.runtime_root / ("dsh-home-group-" + paths["key"])
            group_runtime = {"workspaceRoot": str(workspace), "platformId": platform_id,
                             "botId": bot_id, "groupId": group_id}
            client = await start_client(
                dsh_bin=self.runtime_root / "dsh-runtime/node_modules/.bin/dsh",
                dsh_home=dsh_home, workspace=workspace,
                patch=self.repo / "dsh/profile/v2-agent-group-sandbox.patch.yml",
                instructions=self.repo / "dsh/profile/agent-group-sandbox.md",
                enable_authorization=True, execution_profile="group-sandbox",
                group_runtime=group_runtime,
            )
            service = ChatService(self.mcp.journal, state, client, self.mcp,
                                  trusted_sources=True, audit=self.audit,
                                  domain="chat", execution_profile="group-sandbox")
            self.clients[name] = client
            self.services[name] = service
            if dynamic_migration:
                try:
                    await self._migrate_one_mapping(legacy, state, service,
                                                    self.runtime_root / "dsh-home-chat")
                except BaseException:
                    self.services.pop(name, None)
                    self.clients.pop(name, None)
                    await asyncio.to_thread(client.close)
                    await asyncio.to_thread(module.unmount_group, workspace)
                    self._group_runtimes.pop(name, None)
                    raise
                self._migration_marker().unlink(missing_ok=True)
            return service

    async def _service_for(self, message):
        if message.conversation_key.startswith("group:"):
            return await self._ensure_group_service(message.platform_id, message.bot_id,
                                                    message.conversation_key.removeprefix("group:"))
        domain = classify(message, self.owners)
        return self.services[domain]

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
        journal_ready, state_ready, owner_ready, groups_ready = False, True, not self.owners, True
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
        expected.update(name for name in self.services if name.startswith("group:"))
        acp_ready = set(self.clients) == expected
        for client in self.clients.values():
            try:
                status = client.health()
                acp_ready = acp_ready and status["ready"]
                quarantined += status["quarantined_sessions"]
            except Exception:
                acp_ready = False
        owner_ready = not self.owners
        if self.owners and "owner" in self.clients:
            try:
                owner_ready = self.clients["owner"].health()["ready"]
            except Exception:
                pass
        for name, runtime in self._group_runtimes.items():
            try:
                fd = await asyncio.to_thread(runtime["volume_module"].verified_group_workspace,
                                             runtime["workspace"])
                os.close(fd)
            except Exception:
                groups_ready = False
        admitted_group_ids = {key.removeprefix("group:") for key in self.allowed_conversations
                              if key.startswith("group:")}
        owner_pairs = {(owner.platform_id, owner.bot_id) for owner in self.owners}
        if admitted_group_ids:
            if len(owner_pairs) != 1:
                groups_ready = False
            else:
                platform_id, bot_id = next(iter(owner_pairs))
                materialized = {(item["platform_id"], item["bot_id"], item["group_id"])
                                for item in self._group_runtimes.values()}
                groups_ready = groups_ready and all(
                    (platform_id, bot_id, group_id) in materialized
                    for group_id in admitted_group_ids
                )
        connected = self._onebot_connected()
        core = bool(self._initialized and not self._closing and journal_ready and state_ready
                    and acp_ready and owner_ready and groups_ready)
        return {"pid": os.getpid(), "core_ready": core, "ready": core and connected,
                "closing": self._closing, "onebot_connected": connected, "journal_ready": journal_ready,
                "state_ready": state_ready, "acp_ready": bool(acp_ready),
                "owner_host_ready": owner_ready, "group_sandboxes_ready": groups_ready,
                "active_turns": len(self._active), "queued": sum(s._queued for s in self.services.values()),
                "pending_conversations": pending, "quarantined_sessions": quarantined,
                "degraded": bool(quarantined or self.audit.write_errors)}

    @filter.command("v2agent")
    async def configure_agent(self, event: AstrMessageEvent, kind: str = "", value: str = ""):
        """Select this conversation's persona or skill scene from trusted host state."""
        event.set_extra("v2_deterministic_handled", True)
        if self._closing or not self._initialized:
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
            service = await self._service_for(message)
            trace = self.audit.trace(message.conversation, message.message_id, domain)
            trace_token = CURRENT_TRACE.set(trace)
            trace.emit("control/request", control=kind)
            choices = {"persona": PersonaStateStore.CHOICES, "scene": SceneStateStore.CHOICES}
            if kind == "metrics" and not value:
                metrics = service.metrics()
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
                resolved = await service.resolve_pending(
                    message.conversation, int(value), get_group=event.get_group,
                )
                await reply("已解除暂停并跳过截至 Journal " + str(resolved.last_seen_journal_id)
                            + " 的待核对输入；未重试旧操作，也未确认旧回复发送成功。可以发送新的请求。")
                return
            if kind == "cancel" and not value:
                accepted = await service.cancel(message.conversation)
                await reply("已请求取消当前执行；请用 /v2agent status 核对状态。已发生的操作不会撤销。"
                            if accepted else "当前没有可取消的模型执行；请用 /v2agent status 查看会话状态。")
                return
            if kind == "status" and not value:
                status = await service.status(message.conversation)
                labels = {"not-started": "尚未开始", "running": "执行中", "pending": "结果待核对", "idle": "空闲"}
                await reply("Agent：" + labels[status["state"]]
                            + "；人格：" + status["persona"] + "；场景：" + status["scene"]
                            + "；模式：" + ("Owner 主机执行" if service.execution_profile == "owner-host"
                                           else "群聊隔离执行" if service.execution_profile == "group-sandbox"
                                           else "安全聊天")
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

            await service.select_profile(
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
        if self._closing or not self._initialized or not should_route(event, raw):
            return
        from data.plugins.v2_journal.journal import message_from_onebot

        task = asyncio.current_task()
        try:
            message = message_from_onebot(event.get_platform_id(), raw)
            if not message.message_id:
                raise ValueError("QQ inbound message has no platform message ID")
            if message.conversation_key not in self.allowed_conversations:
                return
            service = await self._service_for(message)
            domain = classify(message, self.owners)

            async def send(reply: str) -> None:
                self.onebot_audit.bind(getattr(event, "bot", None))
                await event.send(MessageChain().message(reply))

            self._active.add(task)
            await service.handle(
                message.conversation, message.message_id, send,
                get_group=event.get_group,
            )
        except FinalReplyDeliveryError as exc:
            self.logger.exception("V2 final reply delivery failed for QQ message %s; turn consumed, no replay",
                                  raw.get("message_id"))
            log_location = (str(self.audit.path) if self.audit.write_errors == 0
                            else "journalctl --user -u chatbot-v2-agent.service")
            await notify_delivery_error(event, message.conversation, self.owners, exc, self.logger,
                                        log_location)
        except DshTurnCancelled:
            self.logger.info("V2 DSH cancellation confirmed for QQ message %s; outcome pending review",
                             raw.get("message_id"))
        except PendingTurnError as exc:
            self.logger.error("V2 DSH conversation paused: %s", exc)
        except Exception:
            self.logger.exception("V2 DSH turn failed for QQ message %s", raw.get("message_id"))
        finally:
            self._active.discard(task)

    async def _close_runtime(self) -> None:
        errors = []
        for client in reversed(tuple(getattr(self, "clients", {}).values())):
            try:
                await asyncio.to_thread(client.close)
            except BaseException as error:
                errors.append(error)
        for runtime in reversed(tuple(getattr(self, "_group_runtimes", {}).values())):
            try:
                await asyncio.to_thread(runtime["volume_module"].unmount_group, runtime["workspace"])
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
