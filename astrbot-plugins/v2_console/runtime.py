"""AstrBot/DSH runtime adapter for the V2 console controller."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from .runtime_control import atomic_json, private_json

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_NUMERIC_ID = re.compile(r"^[0-9]{1,32}$")


class PartialRuntimeOperation(RuntimeError):
    """A multi-system action failed after some durable steps completed."""

    def __init__(self, action: str, completed: list[str], cause: BaseException):
        self.action = action
        self.completed = tuple(completed)
        self.cause = cause
        super().__init__(f"{action}未完成；已完成步骤：{', '.join(completed) or '无'}；请刷新核对后重试。")


class RuntimeAdapter:
    def __init__(self, context, repo: str | Path):
        self.context = context
        self.repo = Path(repo).resolve()
        self.service_config_path = self.repo / ".runtime" / "agent-service.json"
        self._restart_disabled = []

    def _router(self):
        metadata = self.context.get_registered_star("v2_dsh_router")
        router = getattr(metadata, "star_cls", None) if metadata is not None else None
        if router is None:
            raise RuntimeError("V2 DSH Router 尚未启动，控制台运行时不可用。")
        return router

    def todos(self) -> list[str]:
        # Recovery-window calibration is program-managed engineering work,
        # not an unresolved administrator setting or a recurring UI warning.
        return []

    async def health(self):
        status = await self._router().health()
        return {key: status[key] for key in ("pid", "ready", "onebot_connected", "core_ready")}

    async def discover(self, store) -> None:
        """Import existing state only for currently admitted Owner/group identities."""
        router = self._router()
        allowed = getattr(router, "allowed_conversations", None)
        if not isinstance(allowed, (set, frozenset)):
            raise RuntimeError("Router 会话准入状态不可用。")
        admitted: dict[tuple[str, str, str], Any] = {}

        owners = getattr(router, "owners", ())
        owner_keys = set()
        for owner in owners:
            platform, bot, user = self._validate_identity(
                owner.platform_id, owner.bot_id, owner.user_id
            )
            key = f"private:{user}"
            if key in allowed:
                owner_keys.add((platform, bot, key))
        owner_service = getattr(router, "services", {}).get("owner")
        if owner_service is not None:
            for identity in owner_keys:
                admitted[identity] = owner_service

        for name, runtime in getattr(router, "_group_runtimes", {}).items():
            platform, bot, group = self._runtime_group_identity(runtime)
            key = f"group:{group}"
            if key not in allowed:
                continue
            service = getattr(router, "services", {}).get(name)
            if service is not None:
                admitted[(platform, bot, key)] = service

        for identity, service in admitted.items():
            state_store = getattr(service, "state", None)
            if state_store is None or not callable(getattr(state_store, "export_rows", None)):
                continue
            for row in state_store.export_rows():
                row_identity = self._validate_identity(
                    row.platform_id, row.bot_id, row.conversation_key
                )
                if row_identity != identity:
                    continue
                route = state_store.get_model_route(*identity)
                if route is None:
                    dsh_settings = getattr(getattr(service, "dsh", None), "settings", {})
                    route = (dsh_settings.get("provider"), dsh_settings.get("model"))
                if (not isinstance(route, (tuple, list)) or len(route) != 2
                        or any(not isinstance(part, str) or not part for part in route)):
                    raise RuntimeError("既有会话没有可确认的模型设置，未导入控制台。")
                enabled = identity not in getattr(service, "_disabled_identities", set())
                # Existing Owner.json entries do not grant editor permissions;
                # the Owner maintains each group list explicitly in the console.
                store.import_session(
                    *identity,
                    {"model": {"provider": route[0], "model": route[1]},
                     "enabled": enabled, "editors": []},
                    expected_revision=store.read()["revision"],
                )

    async def validate_model(self, model: Any) -> None:
        if (not isinstance(model, dict) or set(model) != {"provider", "model"}
                or any(not isinstance(model[key], str) or not model[key]
                       for key in ("provider", "model"))):
            raise ValueError("model must contain provider and model")
        router = self._router()
        clients = list(getattr(router, "clients", {}).values())
        clients.extend(getattr(service, "dsh", None)
                       for service in getattr(router, "services", {}).values())
        shared = next((getattr(getattr(client, "runtime", None), "shared_models", None)
                       for client in clients
                       if client is not None and getattr(
                           getattr(client, "runtime", None), "shared_models", None) is not None), None)
        if shared is None:
            raise RuntimeError("当前 DSH 共享模型目录不可用。")
        _fingerprint, catalog = await asyncio.to_thread(shared.snapshot)
        if not isinstance(catalog, list):
            raise RuntimeError("共享模型目录格式无效。")
        if not any(
            isinstance(item, dict) and item.get("provider") == model["provider"]
            and isinstance(item.get("models"), list) and model["model"] in item["models"]
            for item in catalog
        ):
            raise ValueError("所选模型不在当前 DSH 共享模型目录中。")

    async def update(self, row: dict, settings: dict, revision: int, store) -> dict:
        if getattr(self._router(), "_closing", False):
            raise RuntimeError("Bot 正在停止，请等待重启完成后修改设置。")
        identity = self._row_identity(row)
        validated = store._validate_settings(settings)
        before = store.read()
        before_key = store.session_key(dict(zip(
            ("platform_id", "bot_id", "conversation_key"), identity)))
        previous = before["sessions"].get(before_key)
        # A missing model must never prevent disabling an Agent or revoking an
        # editor. Only an actual selection change requires catalog validation.
        if previous is None or previous["settings"]["model"] != validated["model"]:
            await self.validate_model(validated["model"])
        state = store.read()
        if type(revision) is not int or state["revision"] != revision:
            from .store import RevisionConflict
            raise RevisionConflict(revision, state["revision"])
        key = store.session_key(dict(zip(
            ("platform_id", "bot_id", "conversation_key"), identity
        )))
        current = state["sessions"].get(key)
        if current is None:
            raise KeyError("conversation settings do not exist")
        service = self._service(identity)
        conversation = self._conversation(identity)
        enabling = current["settings"]["enabled"] is False and validated["enabled"] is True
        resume_through = None
        if enabling:
            journal = getattr(service, "journal", None)
            if journal is None or not callable(getattr(journal, "high_watermark", None)):
                raise RuntimeError("无法读取 Journal 高水位；启用设置未保存。")
            resume_through = journal.high_watermark(conversation)
            if type(resume_through) is not int or resume_through < 0:
                raise RuntimeError("Journal 高水位无效；启用设置未保存。")

        # No await between the revision check and durable commit. Concurrent
        # /provider writes use the same revision and therefore cannot be lost.
        options = {"resume_through_journal_id": resume_through} if enabling else {}
        updated = store.update_session(*identity, validated,
                                       expected_revision=revision, **options)
        try:
            if current["settings"]["enabled"] != validated["enabled"]:
                # set_enabled(False) must be free to cancel a running turn;
                # never wait for the service's turn lock before publishing it.
                await service.set_enabled(conversation, validated["enabled"])
        except BaseException as error:
            raise PartialRuntimeOperation("会话设置", ["设置已持久化"], error) from error
        return {"revision": updated["state"]["revision"], "session": updated["session"]}

    async def add_group(self, payload: dict, store, templates_root: Path) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("group payload must be an object")
        platform, bot, group = self._group_payload(payload)
        template_id = payload.get("template_id")
        if not isinstance(template_id, str) or not template_id:
            raise ValueError("template_id is required")
        revision = payload.get("expected_revision")
        if type(revision) is not int:
            raise ValueError("expected_revision must be an integer")

        router = self._router()
        owner_pairs = {(owner.platform_id, owner.bot_id) for owner in getattr(router, "owners", ())}
        if len(owner_pairs) != 1 or (platform, bot) not in owner_pairs:
            raise PermissionError("群身份必须匹配唯一已配置的 Bot Owner 平台与机器人。")
        state = store.read()
        if state["revision"] != revision:
            from .store import RevisionConflict
            raise RevisionConflict(revision, state["revision"])
        if not any(item["id"] == template_id for item in state["templates"]):
            raise KeyError("模板不存在。")
        defaults = store._validate_settings(state["defaults"], allow_unconfigured=True)
        await self.validate_model(defaults["model"])
        if defaults["enabled"] is None:
            raise ValueError("默认 enabled 必须先设置为 true 或 false。")

        conversation_key = f"group:{group}"
        identity = (platform, bot, conversation_key)
        session_key = store.session_key(dict(zip(
            ("platform_id", "bot_id", "conversation_key"), identity
        )))
        agent_config = private_json(self.service_config_path)
        allowed = agent_config.get("allowed_conversations")
        if (not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed)
                or len(set(allowed)) != len(allowed)):
            raise ValueError("Agent 服务准入配置格式无效。")
        existing_session = state["sessions"].get(session_key)
        agent_admitted = conversation_key in allowed
        router_admitted = conversation_key in getattr(router, "allowed_conversations", ())
        if agent_admitted:
            if existing_session is None:
                raise RuntimeError("该群已在 Agent 准入配置中，但缺少控制台迁移记录；请刷新并核对。")
            if router_admitted:
                raise ValueError("该群已经接入；选择模板不会覆盖或重置现有工作区。")
            # Recover the crash window after durable service-config save.
            self._group_runtime(router, identity)
            router.allowed_conversations = frozenset((*router.allowed_conversations, conversation_key))
            return {"added": False, "recovered": True, "completed": ["Router 会话准入已恢复"]}
        if router_admitted:
            raise RuntimeError("Router 与 Agent 服务准入状态不一致；请重启服务并核对配置。")
        if existing_session is not None and existing_session.get("template_id") != template_id:
            raise ValueError("该群存在未完成的接入记录，所选模板与记录不一致；请先核对状态。")
        if existing_session is not None and "template_id" not in existing_session:
            raise ValueError("该群已有会话设置；模板不会替换现有工作区。")

        completed: list[str] = []
        try:
            if existing_session is None:
                store.create_session(*identity, expected_revision=revision, template_id=template_id)
                completed.append("会话设置已持久化")
            await router._ensure_group_service(platform, bot, group)
            completed.append("群工作区与运行时已准备")
            _name, group_runtime = self._group_runtime(router, identity)

            from .templates import TemplateService
            # Group provisioning seeds the stock guides before template selection.
            # Replace only unchanged stock files; never erase edited group content.
            stock_root = self.repo / "dsh/templates/group"
            stock_guides = {name: (stock_root / name).read_bytes()
                            for name in ("AGENTS.md", "persona.md")
                            if (stock_root / name).is_file()}
            copied = TemplateService(templates_root).copy_to(
                template_id, group_runtime["workspace"], replace_pristine_guides=stock_guides)
            completed.append("工作区模板已安全初始化")

            config = self.context.get_config()
            platform_settings = config.get("platform_settings")
            if not isinstance(platform_settings, dict):
                raise ValueError("AstrBot platform_settings is unavailable")
            whitelist = platform_settings.get("id_whitelist")
            if not isinstance(whitelist, list) or any(not isinstance(item, str) for item in whitelist):
                raise ValueError("AstrBot id_whitelist is invalid")
            whitelist_entry = f"{platform}:GroupMessage:{group}"
            if whitelist_entry not in whitelist:
                previous = list(whitelist)
                whitelist.append(whitelist_entry)
                try:
                    config.save_config()
                except BaseException:
                    whitelist[:] = previous
                    raise
            completed.append("AstrBot 平台会话白名单已同步")

            if conversation_key not in allowed:
                updated_config = dict(agent_config)
                updated_config["allowed_conversations"] = [*allowed, conversation_key]
                atomic_json(self.service_config_path, updated_config)
            completed.append("Agent 服务准入已持久化")

            router.allowed_conversations = frozenset((*router.allowed_conversations, conversation_key))
            completed.append("Router 会话准入已启用")
            return {"added": True,
                    "identity": {"platform_id": platform, "bot_id": bot,
                                 "conversation_key": conversation_key},
                    "template_id": template_id, "workspace": str(group_runtime["workspace"]),
                    "template_result": copied, "completed": completed}
        except BaseException as error:
            raise PartialRuntimeOperation("添加群", completed, error) from error

    async def models(self, row: dict) -> dict:
        identity = self._row_identity(row)
        service = self._service(identity)
        shared = getattr(getattr(getattr(service, "dsh", None), "runtime", None),
                         "shared_models", None)
        if shared is not None:
            _fingerprint, catalog = await asyncio.to_thread(shared.snapshot)
            choices = self._catalog_choices(catalog)
            stored = self._stored_settings(identity, service)
            return {"available": True, "current": stored["model"], "choices": choices}
        if service.state.get(*identity) is None:
            return {"available": False,
                    "todo": "TODO(待确认)：当前 DSH 未提供无需创建 Session 的共享模型目录。"}
        return {"available": True, **(await service.model_status(self._conversation(identity)))}

    async def workspace(self, row: dict) -> Path:
        identity = self._row_identity(row)
        router = self._router()
        if identity[2].startswith("private:"):
            self._service(identity)
            return Path(router.services["owner"].dsh.settings["workspace"]).resolve(strict=True)
        _name, runtime = self._group_runtime(router, identity)
        return Path(runtime["workspace"]).resolve(strict=True)

    async def file_lock(self, row: dict) -> Path:
        identity = self._row_identity(row)
        service = self._service(identity)
        if identity[2].startswith("private:"):
            return Path(service.dsh.settings["dsh_home"]) / "workspace-file.lock"
        _name, runtime = self._group_runtime(self._router(), identity)
        return Path(runtime["workspace"]).parent / ".workspace-file.lock"

    async def quiesce(self) -> None:
        router = self._router()
        router._closing = True
        failures = []
        for service in tuple(getattr(router, "services", {}).values()):
            identities = set()
            state = getattr(service, "state", None)
            if state is not None and callable(getattr(state, "export_rows", None)):
                identities.update((row.platform_id, row.bot_id, row.conversation_key)
                                  for row in state.export_rows())
            identities.update(getattr(service, "_active_runs", {}).keys())
            identities.update(getattr(service, "_locks", {}).keys())
            identities.update(tuple(task.identity)
                              for task in getattr(service, "_background_tasks", {}).values())
            for identity in sorted(identities):
                try:
                    was_disabled = identity in getattr(service, "_disabled_identities", set())
                    if not was_disabled:
                        self._restart_disabled.append((service, identity))
                    await service.set_enabled(self._conversation(identity), False)
                    active = getattr(service, "_active_runs", {}).get(identity)
                    if active is not None and not active.done.is_set():
                        failures.append(identity)
                except BaseException:
                    failures.append(identity)
        current = asyncio.current_task()
        pending = [task for task in tuple(getattr(router, "_active", set()))
                   if task is not current and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if failures:
            raise RuntimeError("停止中的 Agent 任务未能全部确认取消；未提交重启请求。")

    async def restart_failed(self):
        for service, identity in self._restart_disabled:
            await service.set_enabled(self._conversation(identity), True)
        self._restart_disabled.clear()
        self._router()._closing = False

    async def context_action(self, action: str, row: dict) -> dict:
        identity = self._row_identity(row)
        service = self._service(identity)
        conversation = self._conversation(identity)
        if action == "new":
            state = await service.reset_from_console(conversation)
            return {"available": True, "reset": True, "session_id": state.session_id,
                    "history_after_journal_id": state.history_after_journal_id}
        if action == "compact":
            return {"available": True, **(await service.compact_from_console(conversation))}
        raise ValueError("unknown context action")

    @staticmethod
    def _catalog_choices(catalog):
        if not isinstance(catalog, list):
            raise RuntimeError("共享模型目录格式无效。")
        choices = []
        for item in catalog:
            if (not isinstance(item, dict) or not isinstance(item.get("provider"), str)
                    or not isinstance(item.get("models"), list)
                    or not isinstance(item.get("names"), list)
                    or len(item["models"]) != len(item["names"])
                    or any(not isinstance(model, str) for model in item["models"])
                    or any(not isinstance(name, str) for name in item["names"])):
                raise RuntimeError("共享模型目录格式无效。")
            choices.extend({"provider": item["provider"], "model": model, "name": name}
                           for model, name in zip(item["models"], item["names"]))
        return choices

    def _stored_settings(self, identity, service):
        store = getattr(service, "console_store", None)
        if store is not None:
            key = store.session_key(dict(zip(
                ("platform_id", "bot_id", "conversation_key"), identity
            )))
            row = store.read()["sessions"].get(key)
            if row is not None:
                return row["settings"]
        settings = getattr(getattr(service, "dsh", None), "settings", {})
        provider, model = settings.get("provider"), settings.get("model")
        return {"model": ({"provider": provider, "model": model}
                           if isinstance(provider, str) and isinstance(model, str) else None)}

    def _service(self, identity: tuple[str, str, str]):
        router = self._router()
        platform, bot, key = self._validate_identity(*identity)
        if key.startswith("private:"):
            user = key.removeprefix("private:")
            owners = {(owner.platform_id, owner.bot_id, owner.user_id)
                      for owner in getattr(router, "owners", ())}
            if (platform, bot, user) not in owners:
                raise PermissionError("仅配置的 Owner 私聊工作区可由控制台访问。")
            service = getattr(router, "services", {}).get("owner")
            if service is None:
                raise RuntimeError("Owner DSH 服务不可用。")
            return service
        if key.startswith("group:"):
            return self._group_runtime(router, (platform, bot, key))[1]["service"]
        raise ValueError("unsupported conversation key")

    def _group_runtime(self, router, identity):
        platform, bot, key = self._validate_identity(*identity)
        if not key.startswith("group:") or not _NUMERIC_ID.fullmatch(key.removeprefix("group:")):
            raise ValueError("invalid group conversation identity")
        group = key.removeprefix("group:")
        matches = []
        for name, runtime in getattr(router, "_group_runtimes", {}).items():
            if self._runtime_group_identity(runtime) == (platform, bot, group):
                entry = dict(runtime)
                entry["service"] = getattr(router, "services", {}).get(name)
                matches.append((name, entry))
        if len(matches) != 1 or matches[0][1]["service"] is None:
            raise PermissionError("找不到与完整会话身份唯一匹配的群运行时。")
        return matches[0]

    @staticmethod
    def _runtime_group_identity(runtime):
        if not isinstance(runtime, dict):
            raise ValueError("invalid group runtime identity")
        platform, bot, group = RuntimeAdapter._validate_identity(
            runtime.get("platform_id"), runtime.get("bot_id"), runtime.get("group_id")
        )
        if not _NUMERIC_ID.fullmatch(group):
            raise ValueError("invalid group ID")
        return platform, bot, group

    @staticmethod
    def _group_payload(payload: dict):
        platform, bot, group = RuntimeAdapter._validate_identity(
            payload.get("platform_id"), payload.get("bot_id"), payload.get("group_id")
        )
        if not _NUMERIC_ID.fullmatch(group):
            raise ValueError("group_id must contain decimal digits")
        return platform, bot, group

    @staticmethod
    def _validate_identity(platform: Any, bot: Any, key: Any) -> tuple[str, str, str]:
        values = (platform, bot, key)
        if any(not isinstance(value, str) or not _SAFE_ID.fullmatch(value)
               or value != value.strip() for value in values):
            raise ValueError("platform_id, bot_id and conversation_key must be complete safe strings")
        return values

    @classmethod
    def _row_identity(cls, row: Any) -> tuple[str, str, str]:
        if not isinstance(row, dict) or not isinstance(row.get("identity"), dict):
            raise ValueError("conversation identity is required")
        identity = row["identity"]
        if set(identity) != {"platform_id", "bot_id", "conversation_key"}:
            raise ValueError("conversation identity must be complete")
        platform, bot, key = cls._validate_identity(
            identity["platform_id"], identity["bot_id"], identity["conversation_key"]
        )
        if key.startswith("group:"):
            valid = _NUMERIC_ID.fullmatch(key.removeprefix("group:"))
        elif key.startswith("private:"):
            valid = _NUMERIC_ID.fullmatch(key.removeprefix("private:"))
        else:
            valid = None
        if not valid:
            raise ValueError("unsupported or invalid conversation key")
        return platform, bot, key

    def _conversation(self, identity: tuple[str, str, str]):
        platform, bot, key = self._validate_identity(*identity)
        return self._router()._conversation_type(platform, bot, key)
