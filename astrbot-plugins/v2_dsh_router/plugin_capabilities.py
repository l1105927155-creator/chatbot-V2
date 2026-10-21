"""Narrow, current-conversation adapters for installed AstrBot business plugins.

The adapters deliberately call the installed plugins' services. They do not
replay command handlers, accept a target conversation, or expose account/admin
controls. User-facing authorization remains the author plugin's policy.
"""

from __future__ import annotations

from typing import Any


BILIBILI_ID = "astrbot_plugin_bilibili"
TWITTER_ID = "astrbot_plugin_twitter"
BILIBILI_DYNAMICS_TOOL = "bili_get_user_dynamics"


class PluginCapabilityError(RuntimeError):
    """A requested optional plugin capability is unavailable or invalid."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class PluginCapabilities:
    """Author-plugin service access bound to one real AstrBot event."""

    def __init__(self, context: Any, event: Any) -> None:
        self.context = context
        self.event = event
        self._umo = self._current_umo(event)

    @staticmethod
    def _current_umo(event: Any) -> str:
        value = getattr(event, "unified_msg_origin", None)
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise PluginCapabilityError("当前会话身份不可用")
        return value

    def _metadata(self, plugin_id: str) -> Any | None:
        getter = getattr(self.context, "get_registered_star", None)
        metadata = getter(plugin_id) if callable(getter) else None
        # AstrBot's handler registry is keyed by the module's plugin name, while
        # metadata.name may differ from the plugin directory ID. Resolve all
        # metadata through its native registry and match the loaded module/root.
        if metadata is None or not self._matches_plugin(metadata, plugin_id):
            all_getter = getattr(self.context, "get_all_stars", None)
            if callable(all_getter):
                metadata = next((item for item in all_getter()
                                 if self._matches_plugin(item, plugin_id)), None)
        if metadata is None:
            return None
        return metadata

    @staticmethod
    def _matches_plugin(metadata: Any, plugin_id: str) -> bool:
        if plugin_id in (getattr(metadata, "name", None),
                         getattr(metadata, "root_dir_name", None)):
            return True
        module_path = str(getattr(metadata, "module_path", "") or "")
        if module_path == plugin_id or module_path.startswith(plugin_id + "."):
            return True
        instance = getattr(metadata, "star_cls", None)
        class_module = str(getattr(type(instance), "__module__", "") or "")
        return class_module == plugin_id or class_module.startswith(plugin_id + ".")

    async def _current_session_metadata(self, plugin_id: str) -> Any | None:
        metadata = self._metadata(plugin_id)
        if metadata is None:
            return None
        if not bool(getattr(metadata, "activated", False)):
            return None
        plugin_name = getattr(metadata, "name", None)
        if not isinstance(plugin_name, str) or not plugin_name:
            return None
        enabled_plugins = getattr(self.event, "plugins_name", None)
        if (enabled_plugins is not None and enabled_plugins != ["*"]
                and not bool(getattr(metadata, "reserved", False))
                and plugin_name not in enabled_plugins):
            return None
        try:
            from astrbot.core.star.session_plugin_manager import SessionPluginManager

            if not await SessionPluginManager.is_plugin_enabled_for_session(self._umo, plugin_name):
                return None
        except Exception:
            # Deny when AstrBot cannot verify the current session's plugin policy.
            return None
        return metadata

    async def _plugin(self, plugin_id: str) -> Any | None:
        metadata = await self._current_session_metadata(plugin_id)
        return getattr(metadata, "star_cls", None) if metadata is not None else None

    async def _bilibili(self) -> Any:
        plugin = await self._plugin(BILIBILI_ID)
        if plugin is None or not all(
            hasattr(plugin, name)
            for name in ("subscription_service", "data_manager", "bili_client")
        ):
            raise PluginCapabilityError("Bilibili 插件未安装或未启用")
        return plugin

    async def _twitter(self) -> Any:
        plugin = await self._plugin(TWITTER_ID)
        if plugin is None or not hasattr(plugin, "subscription_service"):
            raise PluginCapabilityError("Twitter 插件未安装或未启用")
        return plugin

    async def _authorize_native_command(self, plugin_id: str, method_name: str) -> Any:
        """Honor native command enablement and permission filters before services.

        This checks AstrBot's live handler and persisted WebUI command switch;
        it never calls the command handler or synthesizes an event.
        """
        metadata = await self._current_session_metadata(plugin_id)
        plugin = getattr(metadata, "star_cls", None) if metadata is not None else None
        if plugin is None:
            raise PluginCapabilityError("对应插件未安装或未启用")
        full_names = getattr(metadata, "star_handler_full_names", ())
        from astrbot.core import db_helper
        from astrbot.core.star.filter.permission import PermissionTypeFilter
        from astrbot.core.star.star_handler import star_handlers_registry

        handlers = [
            handler
            for full_name in full_names
            if (handler := star_handlers_registry.get_handler_by_full_name(full_name)) is not None
            and handler.handler_name == method_name
        ]
        if len(handlers) != 1:
            raise PluginCapabilityError("AstrBot 未注册对应的原生插件命令")
        handler = handlers[0]
        config = await db_helper.get_command_config(handler.handler_full_name)
        if not bool(getattr(handler, "enabled", True)) or (config is not None and not config.enabled):
            raise PluginCapabilityError("该插件命令已在 AstrBot 中停用")
        for event_filter in handler.event_filters:
            if isinstance(event_filter, PermissionTypeFilter):
                if not event_filter.filter(self.event, getattr(plugin, "cfg", None)):
                    raise PluginCapabilityError("当前 QQ 身份没有使用该插件命令的权限")
        return plugin

    async def status(self) -> dict[str, bool]:
        """Report only availability; never return plugin config or credentials."""
        bili = await self._plugin(BILIBILI_ID)
        twitter = await self._plugin(TWITTER_ID)
        bili_client = getattr(bili, "bili_client", None) if bili is not None else None
        twitter_ready = False
        if twitter is not None:
            try:
                twitter_ready = bool(twitter._provider_ready)
            except Exception:
                twitter_ready = False
        return {
            "bilibili_enabled": bili is not None,
            "bilibili_login_configured": bool(getattr(bili_client, "credential", None)),
            "twitter_enabled": twitter is not None,
            "twitter_provider_ready": twitter_ready,
        }

    async def bili_dynamics(self, uid: int, limit: int = 3) -> str:
        """Invoke the installed plugin's permission-guarded native FunctionTool."""
        self._validate_uid(uid)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise PluginCapabilityError("动态条数需为 1 到 10")
        plugin = await self._bilibili()
        manager_getter = getattr(self.context, "get_llm_tool_manager", None)
        if not callable(manager_getter):
            raise PluginCapabilityError("AstrBot 工具管理器不可用")
        manager = manager_getter()
        tool = next(
            (item for item in manager.get_full_tool_set().tools
             if getattr(item, "name", None) == BILIBILI_DYNAMICS_TOOL),
            None,
        )
        if tool is None or not bool(getattr(tool, "active", True)):
            raise PluginCapabilityError("Bilibili 动态查询工具未启用")

        # AstrBot's supported FunctionTool path applies WebUI tool permissions
        # in _PermissionGuardedTool.call. Calling the raw registered handler
        # would bypass that check and is intentionally avoided.
        from astrbot.core.astr_agent_context import AstrAgentContext
        from astrbot.core.agent.run_context import ContextWrapper

        wrapped = ContextWrapper(AstrAgentContext(context=self.context, event=self.event))
        result = await tool.call(wrapped, uid=uid, limit=limit)
        if isinstance(result, str):
            return result[:20_000]
        if result is None:
            return "Bilibili 工具没有返回内容。"
        return str(result)

    async def bili_subscribe(self, uid: int) -> dict[str, Any]:
        """Subscribe the current UMO with the author's default safe filters."""
        self._validate_uid(uid)
        plugin = await self._authorize_native_command(BILIBILI_ID, "dynamic_sub")
        try:
            info, _error = await plugin.bili_client.get_user_info(uid)
        except Exception as exc:
            raise PluginCapabilityError("Bilibili 用户信息暂时无法获取") from exc
        if not info:
            return {"ok": False, "reason": "user_unavailable"}
        try:
            result = await plugin.subscription_service.add_or_update(
                self._umo, uid, [], [], False
            )
        except Exception as exc:
            raise PluginCapabilityError("Bilibili 订阅暂时失败，请稍后重试") from exc
        return {
            "ok": True,
            "uid": uid,
            "name": str(info.get("name", ""))[:100],
            "updated": bool(result.updated),
            "initialized": bool(result.initialized),
            "automatic_updates_configured": bool(getattr(plugin.bili_client, "credential", None)),
            "notice": (None if getattr(plugin.bili_client, "credential", None)
                       else "订阅记录已保存；自动动态推送需要Owner先在AstrBot完成Bilibili登录。"),
        }

    async def bili_subscriptions(self) -> list[dict[str, Any]]:
        """List Bilibili subscriptions belonging to the current UMO only."""
        plugin = await self._authorize_native_command(BILIBILI_ID, "sub_list")
        try:
            records = plugin.data_manager.get_subscriptions_by_user(self._umo) or []
        except Exception as exc:
            raise PluginCapabilityError("Bilibili 订阅列表暂时无法读取") from exc
        result: list[dict[str, Any]] = []
        for item in records:
            result.append({
                "uid": int(item.uid),
                "filter_types": list(item.filter_types or []),
                "filter_regex": list(item.filter_regex or []),
                "live_atall": bool(item.live_atall),
            })
        return result

    async def bili_unsubscribe(self, uid: int) -> dict[str, Any]:
        """Remove a Bilibili subscription from the current UMO only."""
        self._validate_uid(uid)
        plugin = await self._authorize_native_command(BILIBILI_ID, "sub_del")
        try:
            removed = await plugin.data_manager.remove_subscription(self._umo, uid)
        except Exception as exc:
            raise PluginCapabilityError("Bilibili 取消订阅暂时失败") from exc
        return {"ok": bool(removed), "uid": uid, "reason": None if removed else "not_found"}

    async def twitter_subscribe(self, username: str) -> dict[str, Any]:
        """Subscribe current UMO using the author service's normal defaults."""
        name = self._username(username)
        plugin = await self._authorize_native_command(TWITTER_ID, "follow_twitter")
        if not bool(getattr(plugin, "_provider_ready", False)):
            return {"ok": False, "reason": "provider_unavailable"}
        try:
            result = await plugin.subscription_service.add(self._umo, name)
        except Exception as exc:
            raise PluginCapabilityError("Twitter 订阅暂时失败，请稍后重试") from exc
        return self._safe_twitter_result(result)

    async def twitter_subscriptions(self) -> list[dict[str, Any]]:
        """List only authors subscribed by the current UMO."""
        plugin = await self._authorize_native_command(TWITTER_ID, "list_follows")
        try:
            snapshot = await plugin.subscription_service.get_snapshot()
        except Exception as exc:
            raise PluginCapabilityError("Twitter 订阅列表暂时无法读取") from exc
        result: list[dict[str, Any]] = []
        for username, author in snapshot.items():
            subscribers = author.get("subscribers", {}) if isinstance(author, dict) else {}
            current = subscribers.get(self._umo) if isinstance(subscribers, dict) else None
            if isinstance(current, dict):
                result.append({
                    "username": str(username),
                    "screen_name": str(author.get("screen_name") or username)[:100],
                    "enabled": bool(current.get("status", True)),
                    "media_only": bool(current.get("media", False)),
                })
        return result

    async def twitter_unsubscribe(self, username: str) -> dict[str, Any]:
        """Remove a Twitter subscription from the current UMO only."""
        name = self._username(username)
        plugin = await self._authorize_native_command(TWITTER_ID, "unfollow_twitter")
        try:
            result = await plugin.subscription_service.remove(self._umo, name)
        except Exception as exc:
            raise PluginCapabilityError("Twitter 取消订阅暂时失败，请稍后重试") from exc
        return self._safe_twitter_result(result)

    @staticmethod
    def _validate_uid(uid: int) -> None:
        if type(uid) is not int or not 1 <= uid <= 9_223_372_036_854_775_807:
            raise PluginCapabilityError("UID 必须是正整数")

    @staticmethod
    def _username(username: str) -> str:
        if not isinstance(username, str):
            raise PluginCapabilityError("用户名格式无效")
        value = username.strip().removeprefix("@").strip()
        if not value or len(value) > 64 or any(ch.isspace() for ch in value):
            raise PluginCapabilityError("用户名格式无效")
        return value

    @staticmethod
    def _safe_twitter_result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"ok": False, "reason": "invalid_plugin_response"}
        clean: dict[str, Any] = {"ok": result.get("ok") is True}
        reason = result.get("reason")
        if isinstance(reason, str) and reason in {
            "duplicate", "provider_unavailable", "not_found", "invalid_plugin_response"
        }:
            clean["reason"] = reason
        for key in ("username", "screen_name"):
            value = result.get(key)
            if isinstance(value, str):
                clean[key] = value[:100]
        for key in ("created_author", "replaced", "author_removed"):
            if isinstance(result.get(key), bool):
                clean[key] = result[key]
        return clean
