"""Route unhandled QQ text through the V2 journal and pinned DSH runtime."""

from __future__ import annotations

import os
import asyncio
import importlib.util
import hashlib
import sys
from sys import maxsize
from pathlib import Path

from astrbot.api import star
from astrbot.api.message_components import File
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.star.filter.command import GreedyStr

from .dsh_client import DshTurnCancelled, start_client
from .routing import should_route
from .service import ChatService, PendingTurnError, RecoveryError, ResetError, FinalReplyDeliveryError, ModelTurnError, AgentDisabledError
from .delivery_error import notify_delivery_error
from .turn_error import notify_turn_error
from .reply_safety import ReplyContentError
from .messaging import QQMessaging
from .meme_library import MemeLibrary
from .plugin_capabilities import PluginCapabilities
from .state import StateStore, migration_needed
from .mcp_server import McpServer
from .file_transfer import FileTransfer, resolve_onebot_file
from .vision import QQVision
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
        self._commands_ready = False
        self._command_surface = None
        self._active = set()
        self._pending_notice_keys = set()
        # The plugin is symlinked into AstrBot's data/plugins directory by V2.
        repo = Path(__file__).resolve().parents[2]
        runtime = (repo / ".runtime").resolve(strict=True)
        contract = os.environ.get("V2_EXECUTION_CONTRACT")
        if contract != "2":
            raise RuntimeError("V2_EXECUTION_CONTRACT=2 is required; refusing legacy execution configuration")
        self.repo = repo
        self.runtime_root = runtime
        from .console_settings import configured_store
        self.console_store = configured_store()
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
        self.mcp.configure_background(self._background_event)
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
                enable_authorization=True,
                background_callback_url=self.mcp.background_callback_url,
                background_callback_token=self.mcp.background_callback_token,
            )
            self.services["chat"] = ChatService(
                journal, chat_state, self.clients["chat"], self.mcp,
                audit=self.audit, domain="chat", execution_profile="safe-chat",
                trusted_sources=True,
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
                    background_callback_url=self.mcp.background_callback_url,
                    background_callback_token=self.mcp.background_callback_token,
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
            await self._initialize_console_settings()
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

    async def _initialize_console_settings(self):
        """Adopt existing model choices and homes without inferring editor roles."""
        store = self.console_store
        if store is None:
            return
        from data.plugins.v2_journal.journal import Conversation
        identities = {(owner.platform_id, owner.bot_id, "private:" + owner.user_id)
                      for owner in self.owners
                      if "private:" + owner.user_id in self.allowed_conversations}
        identities.update((runtime["platform_id"], runtime["bot_id"], "group:" + runtime["group_id"])
                          for runtime in self._group_runtimes.values())
        for platform, bot, key in sorted(identities):
            conversation = Conversation(platform, bot, key)
            service = (self.services["owner"] if key.startswith("private:") else
                       await self._ensure_group_service(platform, bot, key.removeprefix("group:")))
            previous = service.state.get_model_route(platform, bot, key)
            route = previous or (service.dsh.settings["provider"], service.dsh.settings["model"])
            store.import_session(platform, bot, key,
                                 {"model": {"provider": route[0], "model": route[1]},
                                  "enabled": True, "editors": []},
                                 expected_revision=store.read()["revision"])
            from .console_settings import session_settings
            settings = session_settings(store, conversation)
            await service.set_enabled(conversation, settings["enabled"])

    def _console_accepts(self, message):
        if getattr(self, "_closing", False):
            return False
        store = getattr(self, "console_store", None)
        if store is None:
            return True
        if message.conversation_key.startswith("private:") and Owner(
                message.platform_id, message.bot_id, message.actor_id) not in self.owners:
            return False
        from .console_settings import session_settings
        try:
            return session_settings(store, message.conversation)["enabled"] is True
        except PermissionError:
            return False

    @filter.on_astrbot_loaded()
    async def install_agent_commands(self):
        """Bind user commands after all native handlers have been registered."""
        from .command_surface import install_command_surface
        self._command_surface = await install_command_surface(previous=self._command_surface)
        self._commands_ready = True

    @filter.on_plugin_loaded()
    async def refresh_agent_commands(self, metadata):
        """Keep the same surface when the router or native commands reload."""
        if (getattr(self, "_initialized", False) and not self._closing and
                getattr(metadata, "name", None) in {"v2_dsh_router", "builtin_commands"}):
            await self.install_agent_commands()

    @filter.command("new")
    async def new_chat(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """开始干净的 DSH 对话；群聊仅 Bot Owner 可用。"""
        await self._agent_command(event, "new", args)

    @filter.command("reset")
    async def reset_chat(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """开始干净的 DSH 对话，与 /new 相同。"""
        await self._agent_command(event, "reset", args)

    @filter.command("stop")
    async def stop_chat(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """请求停止当前 DSH 执行；群聊仅 Bot Owner 可用。"""
        await self._agent_command(event, "stop", args)

    @filter.command("help")
    async def agent_help(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """查看当前 Agent-Bot 命令及作用范围。"""
        await self._agent_command(event, "help", args)

    @filter.command("provider")
    async def provider_notice(self, event: AstrMessageEvent, args: GreedyStr):
        """查看或切换当前会话的 DSH 模型。"""
        await self._agent_command(event, "provider", args)

    @filter.command("stats")
    async def stats_notice(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """说明 DSH 统计的实际作用范围。"""
        await self._agent_command(event, "stats", args)

    @filter.command("set")
    async def set_notice(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """说明 Agent 配置的实际管理入口。"""
        await self._agent_command(event, "set", args)

    @filter.command("unset")
    async def unset_notice(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """说明 Agent 配置的实际管理入口。"""
        await self._agent_command(event, "unset", args)

    async def _agent_command(self, event, action, args=""):
        event.set_extra("v2_deterministic_handled", True)
        task = asyncio.current_task()
        self._active.add(task)
        trace_token = None
        trace = None

        async def reply(text):
            async def native_send(value):
                self.onebot_audit.bind(getattr(event, "bot", None))
                await event.send(MessageChain().message(value))
            return await trace.send(native_send, text, "control") if trace else await native_send(text)

        try:
            if self._closing or not self._initialized or not self._commands_ready:
                await reply("Agent 命令尚未就绪，请稍后再试。")
                return
            if event.get_platform_name() != "aiocqhttp":
                await reply("这些 Agent 命令用于当前已启用的 QQ 会话。")
                return
            from data.plugins.v2_journal.journal import message_from_onebot
            raw = getattr(event.message_obj, "raw_message", None)
            message = message_from_onebot(event.get_platform_id(), raw)
            if not self._console_accepts(message):
                return
            if not message.message_id or message.direction != "inbound":
                raise ValueError("Agent command requires an inbound canonical message")
            domain = classify(message, self.owners)
            if message.conversation_key not in self.allowed_conversations:
                await reply("当前会话尚未启用 Agent。")
                return
            trace = self.audit.trace(message.conversation, message.message_id, domain)
            trace_token = CURRENT_TRACE.set(trace)
            trace.emit("control/request", control=action)
            if action == "help":
                from .command_surface import help_text
                await reply(help_text())
                return
            if action == 'provider':
                from .model_command import describe_model, select_route
                argument = args.strip()
                changing = argument not in {'', 'list'}
                if (changing and message.conversation_key.startswith('group:') and
                        Owner(message.platform_id, message.bot_id, message.actor_id) not in self.owners):
                    await reply('群聊共享一个 Agent 会话，仅 Bot Owner 可以切换模型；/provider 和 /provider list 可查看模型。')
                    return
                service = await self._service_for(message)
                view = await service.model_status(message.conversation, get_group=event.get_group)
                if not changing:
                    await reply(describe_model(view, listing=argument == 'list'))
                    return
                try:
                    route = select_route(argument, view['choices'])
                except ValueError as exc:
                    await reply(str(exc))
                    return
                if route is None:
                    view = await service.use_default_model(message.conversation, get_group=event.get_group)
                else:
                    view = await service.select_model(message.conversation, *route, get_group=event.get_group)
                current = view['current']
                await reply(f"已将本会话模型切换为 {current['provider']} / {current['model']}。后续轮次使用此模型，上下文、人格和场景保留。")
                return
            if action in {"stats", "set", "unset"}:
                notices = {
                    "stats": "/stats 尚未接通 DSH 的 Token 统计。Bot Owner 可用 /v2agent metrics 查看当前运行周期的请求与完成情况。",
                    "set": "/set 尚未接通 DSH 配置。Bot Owner 可用 /v2agent persona 或 scene 管理当前会话选项，指南文档支持热更新。",
                    "unset": "/unset 尚未接通 DSH 配置。Bot Owner 可用 /v2agent persona 或 scene 管理当前会话选项，指南文档支持热更新。",
                }
                await reply(notices[action])
                return
            if (message.conversation_key.startswith("group:") and
                    Owner(message.platform_id, message.bot_id, message.actor_id) not in self.owners):
                await reply("群聊共享一个 Agent 会话，仅 Bot Owner 可以使用 /new、/reset、/stop。")
                return
            if args.strip():
                await reply("用法：/" + action)
                return
            service = self._cached_service_for(message)
            barrier = None
            if service is not None and action in {"new", "reset"}:
                barrier = service.begin_reset(message.conversation, message.message_id)
            if service is None:
                service = await self._service_for(message)
            if action in {"new", "reset"}:
                await service.reset_conversation(message.conversation, message.message_id,
                                                 get_group=event.get_group, barrier=barrier)
                await reply("已开始新的 Agent 对话，历史检索从本次重置后开始。人格、场景和工作区文件保留。")
            elif action == "stop":
                accepted = await service.cancel(message.conversation)
                await reply("已请求停止当前 Agent 执行。已发生的操作会保留；Bot Owner 可用 /v2agent status 核对状态。"
                            if accepted else "当前没有可停止的模型执行。已有待核对结果仍需由 Bot Owner 核对恢复。")
        except PendingTurnError:
            await reply("当前会话有结果待核对，本次未切换模型。请由 Bot Owner 使用 /v2agent status 核对后再继续。"
                        if action == 'provider' else
                        "当前会话有结果待核对，尚未重置。请由 Bot Owner 使用 /v2agent status 核对，再按提示恢复。")
        except ResetError as exc:
            await reply(str(exc))
        except Exception:
            self.logger.exception("V2 Agent command %s failed", action)
            await reply("模型目录同步或切换尚未完成，请检查 DSH Web 模型设置或运行日志，再查询 /provider。"
                        if action == 'provider' else
                        "Agent 命令未完成，请由 Bot Owner 检查运行日志；未确认重置或停止成功。")
        finally:
            if trace_token is not None:
                CURRENT_TRACE.reset(trace_token)
            self._active.discard(task)
            event.stop_event()

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

    async def _background_event(self, event):
        """Route native task events only to the service owning the parent SID."""
        if getattr(self, "_closing", False):
            return False
        for service in tuple(getattr(self, "services", {}).values()):
            if await service.handle_background_event(event):
                return True
        return False

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
            from .console_settings import group_template, initialize_group_template
            selected_template = group_template(getattr(self, "console_store", None), platform_id, bot_id, group_id)
            options = {"seed_guides": False} if selected_template else {}
            await asyncio.to_thread(module.prepare_group, group_storage_root, platform_id, bot_id, group_id, **options)
            if selected_template:
                await asyncio.to_thread(initialize_group_template, self.console_store, selected_template, paths)
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
                background_callback_url=self.mcp.background_callback_url,
                background_callback_token=self.mcp.background_callback_token,
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

    def _cached_service_for(self, message):
        """Select a prepared service without yielding before a reset barrier."""
        services = getattr(self, "services", {})
        if message.conversation_key.startswith("group:"):
            group_id = message.conversation_key.removeprefix("group:")
            for name, runtime in getattr(self, "_group_runtimes", {}).items():
                if (runtime["platform_id"], runtime["bot_id"], runtime["group_id"]) == (
                        message.platform_id, message.bot_id, group_id):
                    return services.get(name)
            return None
        return services.get(classify(message, self.owners))

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
        journal_readable, capture_intact, collector_ready = False, False, False
        capture_failures = 0
        pending, quarantined = 0, 0
        try:
            journal_readable = await asyncio.to_thread(self.mcp.journal.check_health)
            capture = await asyncio.to_thread(self.mcp.journal.capture_status)
            capture_failures = capture["failure_count"]
            capture_intact = capture["available"] and not capture["degraded"]
            # The collector has an in-process latch even if SQLite cannot
            # persist its failure. A separate readable store is insufficient.
            metadata = self.context.get_registered_star("v2_journal")
            collector = metadata.star_cls if metadata and metadata.activated else None
            if collector is not None:
                status = collector.collector_health()
                collector_ready = bool(status["initialized"] and not status["closing"] and status["bindings_match"])
                capture_failures = max(capture_failures, status["capture_errors"])
                capture_intact = capture_intact and not status["capture_degraded"]
            journal_ready = bool(journal_readable and capture_intact and collector_ready)
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
        commands_ready = bool(getattr(self, "_commands_ready", True))
        core = bool(self._initialized and not self._closing and journal_ready and state_ready
                    and acp_ready and owner_ready and groups_ready and commands_ready)
        return {"pid": os.getpid(), "core_ready": core, "ready": core and connected,
                "closing": self._closing, "onebot_connected": connected, "journal_ready": journal_ready,
                "journal_readable": bool(journal_readable), "journal_capture_intact": bool(capture_intact),
                "journal_collector_ready": collector_ready, "journal_capture_failures": capture_failures,
                "state_ready": state_ready, "acp_ready": bool(acp_ready),
                "command_surface_ready": commands_ready,
                "owner_host_ready": owner_ready, "group_sandboxes_ready": groups_ready,
                "active_turns": len(self._active), "queued": sum(s._queued for s in self.services.values()),
                "pending_conversations": pending, "quarantined_sessions": quarantined,
                "degraded": bool(quarantined or self.audit.write_errors or not journal_ready)}

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
            if not self._console_accepts(message):
                return
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
                persona_label = ("本群 persona.md" if service.execution_profile == "group-sandbox"
                                 else status["persona"])
                fault_label = {"infrastructure": "运行通道故障", "unknown": "执行结果未知",
                               "cancelled": "操作已取消，结果待核对"}.get(
                                   status.get("pending_fault_kind"), "")
                fault_code = status.get("pending_fault_code")
                fault_detail = ("；原因：" + fault_label +
                                (("（" + fault_code + "）") if fault_code else "")
                                if fault_label else "")
                details = status.get("pending_fault_details") or {}
                confirmed_tools = details.get("confirmed_tools", [])
                unknown_tools = details.get("unknown_tools", [])
                if confirmed_tools:
                    fault_detail += "；已确认完成的工具：" + "、".join(confirmed_tools[:8])
                if unknown_tools:
                    fault_detail += "；结果未知的工具：" + "、".join(unknown_tools[:8])
                if fault_code in {"NO_TERMINAL_RECEIPT", "SESSION_DRAIN_FAILED"}:
                    fault_detail += "；未收到原生终态，工具执行和消息发送状态无法确认"
                await reply("Agent：" + labels[status["state"]]
                            + "；人格：" + persona_label + "；场景：" + status["scene"]
                            + "；模式：" + ("Owner 主机执行" if service.execution_profile == "owner-host"
                                           else "群聊隔离执行" if service.execution_profile == "group-sandbox"
                                           else "安全聊天")
                            + ("；待核对 Journal：" + str(status["pending_upper_cursor"])
                               + fault_detail + "。核对已发生的执行和发送结果后，可用 /v2agent recover "
                               + str(status["pending_upper_cursor"]) + " 跳过该轮并解除暂停，系统不会重试旧输入。"
                               if status["state"] == "pending" else "。"))
                return
            if kind == "persona" and service.execution_profile == "group-sandbox":
                await reply("本群人格由 /workspace/persona.md 定义。请在普通请求中说明想调整的风格并让我编辑这个文件；保存后下一轮自动生效。")
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

    def _file_transfer(self, event, message, service):
        profile = service.execution_profile
        workspace = Path(service.dsh.settings['workspace'])
        verify = None
        storage_root = workspace
        if profile == 'group-sandbox':
            module = _group_runtime_module(self.repo)
            verify = lambda: module.verified_group_workspace(workspace)
        else:
            identity = '\0'.join((message.platform_id, message.bot_id, message.conversation_key))
            # Existing stopped-writer backups already include plugin_data.
            # Owner's execution cwd stays unchanged; only received attachments
            # go to this private durable inbox, outside version-controlled code.
            storage_root = (Path(star.StarTools.get_data_dir('v2_dsh_router')) / 'attachments'
                            / hashlib.sha256(identity.encode()).hexdigest())
            storage_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            if profile == 'safe-chat':
                workspace = storage_root

        async def resolve(segment):
            return await resolve_onebot_file(event.bot, message.conversation, segment)

        async def send_file(path, name):
            self.onebot_audit.bind(event.bot)
            async def native_send(_):
                await event.send(MessageChain([File(name=name, file=str(path))]))
            trace = transfer.trace
            if trace:
                await trace.send(native_send, None, 'mcp')
            else:
                await native_send(None)

        transfer = FileTransfer(self.mcp.journal, message.conversation, workspace,
                                profile=profile, verify_root=verify, resolve=resolve, send=send_file,
                                spool_dir=Path(star.StarTools.get_data_dir('v2_dsh_router')) / 'file-spool',
                                storage_root=storage_root,
                                workspace_lock_path=(workspace.parent / '.workspace-file.lock'
                                                     if profile == 'group-sandbox' and
                                                     getattr(self, 'console_store', None) is not None else None))
        return transfer

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-maxsize)
    async def route_chat(self, event: AstrMessageEvent) -> None:
        raw = getattr(event.message_obj, "raw_message", None)
        if self._closing or not self._initialized or not should_route(event, raw):
            return
        from data.plugins.v2_journal.journal import message_from_onebot

        task = asyncio.current_task()
        turn_started = False
        message = None
        service = None
        try:
            message = message_from_onebot(event.get_platform_id(), raw)
            if not self._console_accepts(message):
                return
            if not message.message_id:
                raise ValueError("QQ inbound message has no platform message ID")
            if message.conversation_key not in self.allowed_conversations:
                return
            service = await self._service_for(message)
            domain = classify(message, self.owners)

            self.onebot_audit.bind(getattr(event, "bot", None))
            messaging = QQMessaging(event, self.mcp.journal, message.conversation)

            async def send(reply: str) -> None:
                await messaging.send_final(reply)

            self._active.add(task)
            turn_started = True
            async def resolve_image(segment):
                return await resolve_onebot_file(event.bot, message.conversation, segment)

            async def send_meme(path):
                from astrbot.api.message_components import Image
                async def native_send(_):
                    await event.send(MessageChain([Image.fromFileSystem(str(path))]))
                trace = CURRENT_TRACE.get()
                if trace:
                    await trace.send(native_send, None, 'mcp')
                else:
                    await native_send(None)

            await service.handle(
                message.conversation, message.message_id, send,
                get_group=event.get_group, files=self._file_transfer(event, message, service),
                vision=QQVision(resolve_image), messaging=messaging,
                memes=MemeLibrary(self.runtime_root.parent / 'meme', send_meme),
                plugins=PluginCapabilities(self.context, event),
            )
        except AgentDisabledError:
            self.logger.info("V2 Agent input stopped by console disable barrier")
        except ReplyContentError as exc:
            self.logger.warning("V2 withheld invalid model reply (code=%s)", exc.code)
            if turn_started and message is not None and self._console_accepts(message):
                await notify_turn_error(event, message.conversation, self.owners, "invalid_reply", self.logger)
        except FinalReplyDeliveryError as exc:
            self.logger.exception("V2 final reply delivery failed for QQ message %s; turn consumed, no replay",
                                  raw.get("message_id"))
            log_location = (str(self.audit.path) if self.audit.write_errors == 0
                            else "journalctl --user -u chatbot-v2-agent.service")
            if self._console_accepts(message):
                await notify_delivery_error(event, message.conversation, self.owners, exc, self.logger,
                                            log_location)
        except ModelTurnError as exc:
            self.logger.warning("V2 model turn terminated before any tool call for QQ message %s (code=%s)",
                                raw.get("message_id"), exc.code)
            if turn_started and message is not None and self._console_accepts(message):
                await notify_turn_error(event, message.conversation, self.owners, "model", self.logger,
                                        code=exc.code, tools_completed=exc.tools_completed)
        except DshTurnCancelled:
            self.logger.info("V2 DSH cancellation confirmed for QQ message %s; outcome pending review",
                             raw.get("message_id"))
            reset_cancel = (turn_started and message is not None and
                            callable(getattr(service, "is_reset_cancellation", None)) and
                            service.is_reset_cancellation(message.conversation, message.message_id))
            if turn_started and message is not None and not reset_cancel and self._console_accepts(message):
                await notify_turn_error(event, message.conversation, self.owners, "cancelled", self.logger)
        except PendingTurnError as exc:
            self.logger.error("V2 DSH conversation paused: %s", exc)
            if turn_started and message is not None and self._console_accepts(message):
                try:
                    status = await service.status(message.conversation)
                except Exception:
                    status = {}
                    self.logger.exception("V2 cannot read pending fault status for turn notice")
                pending_cursor = status.get("pending_upper_cursor")
                notice_key = (message.conversation.platform_id, message.conversation.bot_id,
                              message.conversation.key, pending_cursor)
                notices = getattr(self, "_pending_notice_keys", None)
                if notices is None:
                    notices = self._pending_notice_keys = set()
                if notice_key not in notices:
                    notices.difference_update({key for key in notices if key[:3] == notice_key[:3]})
                    notices.add(notice_key)
                    fault_kind = status.get("pending_fault_kind")
                    notice_kind = fault_kind if fault_kind in {"infrastructure", "unknown"} else "pending"
                    await notify_turn_error(event, message.conversation, self.owners, notice_kind, self.logger)
        except Exception:
            self.logger.exception("V2 DSH turn failed for QQ message %s", raw.get("message_id"))
            if (message is not None and message.conversation_key in self.allowed_conversations
                    and self._console_accepts(message)):
                kind = "infrastructure" if service is None else "failed"
                await notify_turn_error(event, message.conversation, self.owners, kind, self.logger,
                                        code="INITIALIZATION_FAILED" if service is None else None)
        finally:
            self._active.discard(task)

    async def _close_runtime(self) -> None:
        errors = []
        for service in tuple(getattr(self, "services", {}).values()):
            try:
                await service.close_background()
            except BaseException as error:
                errors.append(error)
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
        try:
            await self._close_runtime()
        finally:
            if getattr(self, "_command_surface", None) is not None:
                from .command_surface import restore_command_surface
                await restore_command_surface(self._command_surface)
                self._command_surface = None
                self._commands_ready = False
