"""Authenticated AstrBot page/API entry for the V2 management console."""
from pathlib import Path
import asyncio
import os

from astrbot.api import star
from astrbot.api.web import request, error_response, stream_response
from starlette.background import BackgroundTask

from .controller import ConsoleController
from .runtime_control import authorize_dashboard
from .store import ConsoleStore
from .preset_templates import install_workspace_persona_presets


@star.register("v2_console", "chatbot-V2", "V2 会话设置与工作区控制台", "0.1.0")
class V2Console(star.Star):
    async def initialize(self):
        from .runtime import RuntimeAdapter
        root = Path(star.StarTools.get_data_dir("v2_console"))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        repo = Path(os.environ.get("V2_CONSOLE_REPO_ROOT", Path(__file__).resolve().parents[2])).resolve(strict=True)
        if not (repo / "upstream.lock.json").is_file():
            raise RuntimeError("V2_CONSOLE_REPO_ROOT must identify the trusted V2 deployment")
        store = ConsoleStore(root / "state.json")
        install_workspace_persona_presets(store, root / "templates", repo)
        self.controller = ConsoleController(store, root / "templates",
                                            RuntimeAdapter(self.context, repo))
        self.context.register_web_api("/v2_console/dispatch", self.dispatch, ["POST"], "V2 Owner 控制台")
        self.context.register_web_api("/v2_console/export", self.export, ["GET"], "导出会话工作区 ZIP")

    def _pipeline_reloader_for_current_request(self):
        """Resolve the pinned Dashboard lifecycle for the authenticated web API call."""
        try:
            native_request = request._get_current()._request
            app = native_request.app
            lifecycle = app.state.core_lifecycle
        except (AttributeError, RuntimeError) as error:
            raise RuntimeError("AstrBot 原生消息流水线刷新接口不可用。") from error
        reload_pipeline_scheduler = getattr(lifecycle, "reload_pipeline_scheduler", None)
        if (getattr(lifecycle, "star_context", None) is not self.context
                or not callable(reload_pipeline_scheduler)):
            raise RuntimeError("AstrBot 原生消息流水线刷新接口不可用。")
        return reload_pipeline_scheduler

    async def dispatch(self):
        try:
            # The sole dashboard administrator was explicitly authorized by the
            # user. API-key pseudo-users and page asset tokens do not match it.
            dashboard = self.context.get_config().get("dashboard", {})
            authorize_dashboard(request.username, {"dashboard_owner_username": dashboard.get("username")})
            payload = await request.json()
            kwargs = {}
            if isinstance(payload, dict) and payload.get("action") == "group.add":
                kwargs["reload_pipeline_scheduler"] = self._pipeline_reloader_for_current_request()
            result = await self.controller.dispatch(payload, **kwargs)
            return {"status": "ok", "data": result, "message": ""}
        except (ValueError, TypeError, KeyError, PermissionError, OSError, RuntimeError) as error:
            # Do not surface arbitrary upstream exception text or provider keys.
            from .store import RevisionConflict
            if (isinstance(error, RevisionConflict)
                    or (type(error).__name__ == "RevisionConflict"
                        and type(getattr(error, "expected_revision", None)) is int
                        and type(getattr(error, "actual_revision", None)) is int)):
                message = "设置已被修改，请重新加载后再保存。"
            elif isinstance(error, FileExistsError):
                message = "文件已存在或已被修改，请重新打开后再操作。"
            elif isinstance(error, PermissionError):
                message = "当前身份或文件访问不被允许。"
            elif hasattr(error, "completed"):
                message = "操作未完成；已完成：" + "、".join(error.completed) + "。请刷新核对后重试。"
            else:
                message = "操作未完成，请刷新状态并检查服务日志。"
            self.logger.warning("V2 console action failed: %s", type(error).__name__, exc_info=True)
            return {"status": "error", "data": {"error_type": type(error).__name__,
                    "completed": list(getattr(error, "completed", ()))}, "message": message}

    async def export(self):
        try:
            dashboard = self.context.get_config().get("dashboard", {})
            authorize_dashboard(request.username, {"dashboard_owner_username": dashboard.get("username")})
            archive, filename = await self.controller.export_workspace(
                {"conversation_id": request.query.get("conversation_id")})
        except (ValueError, TypeError, KeyError, PermissionError, OSError, RuntimeError) as error:
            status = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, KeyError) else 409
            self.logger.warning("V2 workspace export failed: %s", type(error).__name__, exc_info=True)
            return error_response("工作区导出未完成，请刷新会话或检查服务日志。", status_code=status)

        async def content():
            try:
                while chunk := await asyncio.to_thread(archive.read, 1024 * 1024):
                    yield chunk
            finally:
                archive.close()

        response = stream_response(content(), content_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        })
        response.background = BackgroundTask(archive.close)
        return response
