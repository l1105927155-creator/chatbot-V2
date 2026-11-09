"""Native AstrBot host for the V2 group memory manager."""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import filter
from astrbot.api.web import error_response, request

from .store import CursorConflict, RevisionConflict


@star.register("v2_memory", "chatbot-V2", "V2 群聊记忆", "0.1.0")
class V2Memory(star.Star):
    async def initialize(self) -> None:
        self._manager = None
        self._bound_router = None
        self._start_lock = asyncio.Lock()
        self._stopping = False
        self.context.register_web_api(
            "/v2_memory/api", self.api, ["POST"], "V2 群聊记忆管理"
        )

    def _ready_router(self):
        try:
            metadata = self.context.get_registered_star("v2_dsh_router")
            router = metadata.star_cls if metadata else None
        except Exception:  # noqa: BLE001 - unavailable router must fail closed
            return None
        if (
            router is None
            or not getattr(router, "_initialized", False)
            or getattr(router, "_closing", True)
        ):
            return None
        return router

    async def start(self) -> bool:
        if self._stopping:
            return False
        async with self._start_lock:
            router = self._ready_router()
            if router is None:
                return False
            if self._manager is not None and self._bound_router is router:
                return True
            if self._manager is not None:
                await self._close_manager()

            existing = getattr(router, "memory_manager", None)
            if existing is not None:
                self.logger.warning("V2 memory router binding is already occupied")
                return False

            try:
                from .manager import MemoryManager

                data_dir = Path(star.StarTools.get_data_dir("v2_memory"))
                data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                manager = MemoryManager(router, data_dir, self.logger)
                try:
                    await manager.start()
                except BaseException:
                    await manager.close()
                    raise
                # Do not bind a manager to a router that began shutting down
                # while its storage and workers were initializing.
                if self._ready_router() is not router or self._stopping:
                    await manager.close()
                    return False
                router.memory_manager = manager
            except Exception as error:
                self.logger.warning(
                    "V2 memory service start failed: %s", type(error).__name__, exc_info=True
                )
                return False
            self._manager = manager
            self._bound_router = router
            return True

    async def _close_manager(self) -> None:
        manager, router = self._manager, self._bound_router
        self._manager = None
        self._bound_router = None
        if router is not None and getattr(router, "memory_manager", None) is manager:
            try:
                delattr(router, "memory_manager")
            except AttributeError:
                pass
        if manager is not None:
            await manager.close()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self.start()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        if getattr(metadata, "name", None) in {"v2_memory", "v2_dsh_router"}:
            await self.start()

    @staticmethod
    def _authorize(username: str | None, owner: object) -> None:
        if not isinstance(owner, str) or not owner or username != owner:
            raise PermissionError

    async def api(self):
        try:
            dashboard = self.context.get_config().get("dashboard", {})
            self._authorize(request.username, dashboard.get("username"))
        except PermissionError:
            return error_response("当前 Dashboard 身份无权管理群记忆。", status_code=403)
        except Exception as error:  # noqa: BLE001 - no configuration detail to caller
            self.logger.warning("V2 memory API authorization failed: %s", type(error).__name__)
            return error_response("群记忆管理暂不可用。", status_code=503)

        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return error_response("请求格式无效。", status_code=400)
            action = payload.get("action")
            if action not in {"status", "list", "get", "edit", "enable", "export", "clear", "cancel",
                              "configure_incremental", "retry_incremental", "failures", "retry_failure"}:
                return error_response("不支持此管理操作。", status_code=400)
            manager = self._manager
            if manager is None or self._bound_router is not self._ready_router():
                return error_response("群记忆服务尚未就绪。", status_code=503)
            arguments = {key: value for key, value in payload.items() if key != "action"}
            if action == "status":
                result = await manager.status()
            else:
                result = await manager.handle(action, arguments)
            return {"status": "ok", "data": result, "message": ""}
        except (CursorConflict, RevisionConflict):
            return error_response("记忆已发生变更，请刷新后核对再保存。", status_code=409)
        except (ValueError, TypeError, KeyError) as error:
            self.logger.info("V2 memory request rejected: %s", type(error).__name__)
            return error_response("请求参数无效或目标不存在。", status_code=400)
        except PermissionError:
            return error_response("当前操作不被允许。", status_code=403)
        except Exception as error:
            if type(error).__name__ == "SettingsConflict":
                return error_response("工作区设置与后台发生冲突；请核对 settings 中的状态文件后重新保存。", status_code=409)
            self.logger.warning("V2 memory request failed: %s", type(error).__name__, exc_info=True)
            return error_response("群记忆操作未完成，请刷新状态并检查服务日志。", status_code=500)

    def workspace_settings(self) -> list[dict]:
        manager = self._manager
        if manager is None or self._stopping:
            return []
        return manager.workspace_settings()

    async def terminate(self) -> None:
        self._stopping = True
        try:
            from ..v2_console.workspace_registry import detach_workspace_settings
        except ImportError:
            try:
                from v2_console.workspace_registry import detach_workspace_settings
            except ImportError:
                detach_workspace_settings = None
        if detach_workspace_settings is not None:
            await detach_workspace_settings(self.context, self)
        async with self._start_lock:
            await self._close_manager()


__all__ = ["V2Memory"]
