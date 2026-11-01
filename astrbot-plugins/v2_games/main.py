"""AstrBot host for the V2 local mini-game hub and 2048 leaderboard."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.web import error_response, request
from astrbot.core.star.filter.command import GreedyStr

from .service import GameCapability, GameError, GameService
from .store import GameStore
from .webapp import GameWebApp, GameWebServer


@star.register("v2_games", "chatbot-V2", "V2 小游戏与排行榜", "0.1.0")
class V2Games(star.Star):
    def __init__(self, context, config=None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._store: GameStore | None = None
        self._service: GameService | None = None
        self._web: GameWebServer | None = None
        self._web_error = ""
        self._initialized = False
        self._closing = False
        self._start_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._closing = False
        self._web_error = ""
        data_dir = Path(star.StarTools.get_data_dir("v2_games"))
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret = self._cookie_secret(data_dir)
        self._store = GameStore(data_dir / "games.sqlite3")
        self._service = GameService(self._store, self.config, self.logger)
        self.context.register_web_api(
            "/v2_games/api", self.api, ["POST"], "V2 小游戏排行榜管理"
        )
        if self._config_bool("web_enabled", True):
            await self._start_web(secret)
        self._initialized = True
        self.logger.info("V2 games service ready (web=%s)", bool(self._web))

    @staticmethod
    def _cookie_secret(data_dir: Path) -> bytes:
        path = data_dir / "cookie.secret"
        if path.is_file():
            value = path.read_bytes()
            if len(value) >= 32:
                return value
        value = secrets.token_bytes(32)
        path.write_bytes(value)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value

    def _config_bool(self, key: str, default: bool) -> bool:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return bool(value) if isinstance(value, bool) else default

    async def _start_web(self, secret: bytes) -> None:
        try:
            port = int(self.config.get("listen_port", 6230))
            if not 1 <= port <= 65535:
                raise ValueError("listen_port invalid")
        except (TypeError, ValueError):
            self._web_error = "listen_port 配置无效"
            self.logger.warning("V2 games web not started: invalid listen_port")
            return
        app = GameWebApp(self._service, secret=secret,
                         web_dir=Path(__file__).resolve().parent / "web").app()
        server = GameWebServer(app, port=port, logger=self.logger)
        try:
            await server.start()
        except Exception as error:
            self._web_error = type(error).__name__
            self.logger.warning("V2 games web failed to start: %s", type(error).__name__,
                                exc_info=True)
            return
        self._web = server

    def ready(self) -> bool:
        return bool(self._initialized and not self._closing and self._service is not None)

    def capability_for(self, conversation, actor_id: str | None = None) -> GameCapability:
        if not self.ready():
            raise GameError("小游戏服务尚未就绪。", code="not_ready")
        return GameCapability(self._service, conversation, actor_id=actor_id)

    # ---- QQ commands ---------------------------------------------------

    @filter.command("games", alias={"游戏"})
    async def games_command(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """本群小游戏入口、排行榜与个人成绩。"""
        event.set_extra("v2_deterministic_handled", True)
        if not self.ready():
            yield event.plain_result("小游戏服务尚未就绪，请稍后再试。")
            return
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("这些小游戏命令仅用于当前 QQ 会话。")
            return
        group_id = str(event.get_group_id() or "")
        sender = str(event.get_sender_id() or "")
        if not group_id:
            yield event.plain_result(
                "请在群聊中使用 /games；群入口链接和本群排行榜按群生成。")
            return
        text = str(args or "").strip()
        try:
            if text in {"", "入口", "链接", "link", "帮助", "help", "排行", "rank",
                        "排行榜", "我", "me", "周榜"}:
                if text in {"帮助", "help"}:
                    yield event.plain_result(self._help_text())
                elif text in {"排行", "rank", "排行榜"}:
                    yield event.plain_result(self._rank_text(group_id, "all"))
                elif text == "周榜":
                    yield event.plain_result(self._rank_text(group_id, "week"))
                elif text in {"我", "me"}:
                    yield event.plain_result(self._me_text(group_id, sender))
                else:
                    yield event.plain_result(self._entry_text(group_id))
                return
            yield event.plain_result(self._help_text())
        except GameError as error:
            yield event.plain_result(error.public_message)
        except Exception as error:  # noqa: BLE001 - command text must stay user-safe
            self.logger.warning("V2 games command failed: %s", type(error).__name__,
                                exc_info=True)
            yield event.plain_result("小游戏命令暂不可用，请稍后再试。")

    def _entry_text(self, group_id: str) -> str:
        link = self._service.group_link(group_id)
        lines = ["2048 小游戏"]
        if link["link"]:
            lines.append(f"本群入口：{link['link']}")
        else:
            lines.append("本群入口尚未配置：请在 AstrBot 的 v2_games 插件设置中填写 public_base_url。")
        board = self._service.leaderboard(group_id=group_id, period="all",
                                          kind="human", limit=3)
        if board["rows"]:
            lines.append("本群前三：")
            for row in board["rows"]:
                lines.append(f"{row['rank']}. {row['display_name']}  {row['score']}")
        lines.append("打开网页后填写 QQ 号即可开始；成绩按本群 + QQ 记录。")
        lines.append("排行：/games 排行；周榜：/games 周榜；我的：/games 我")
        return "\n".join(lines)

    def _rank_text(self, group_id: str, period: str) -> str:
        board = self._service.leaderboard(group_id=group_id, period=period,
                                          kind="human", limit=10)
        title = "本群 2048 周榜" if period == "week" else "本群 2048 总榜"
        if not board["rows"]:
            return f"{title}\n还没有成绩，先发 /games 获取入口。"
        lines = [title]
        for row in board["rows"]:
            lines.append(f"{row['rank']}. {row['display_name']}  {row['score']}"
                         f"  最大块 {row['max_tile']}")
        lines.append("公开网页只显示掩码 QQ；完整 QQ 仅在 Bot 控制台可见。")
        return "\n".join(lines)

    def _me_text(self, group_id: str, qq: str) -> str:
        if not qq:
            return "无法识别当前 QQ，不能查询个人成绩。"
        me = self._service.me(group_id=group_id, qq=qq)
        if me["best_score"] <= 0:
            return (f"{me['display_name']} 在本群还没有 2048 成绩。\n"
                    "发送 /games 获取本群入口；打开网页填写 QQ 后即可开始。")
        lines = [
            f"{me['display_name']} 的本群 2048 成绩",
            f"最高分：{me['best_score']}（最大块 {me['best_max_tile']}）",
            f"本群排名：{me['rank'] or '未上榜'}",
        ]
        if me["recent"]:
            lines.append("最近对局：")
            for row in me["recent"]:
                lines.append(f"- {row['score']} 分，最大块 {row['max_tile']}，"
                             f"{row['status']}")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return ("2048 小游戏命令\n"
                "/games：获取本群游戏入口和前三名\n"
                "/games 排行：查看本群总榜\n"
                "/games 周榜：查看最近 7 天本群榜\n"
                "/games 我：查看自己的本群最高分和排名\n"
                "网页打开后填写 QQ 号即可开始；成绩由服务端回放校验后保存。")

    # ---- Dashboard management API -------------------------------------

    async def api(self):
        try:
            dashboard = self.context.get_config().get("dashboard", {})
            owner = dashboard.get("username")
            if not isinstance(owner, str) or not owner or request.username != owner:
                raise PermissionError
        except PermissionError:
            return error_response("当前 Dashboard 身份无权管理小游戏。", status_code=403)
        except Exception as error:  # noqa: BLE001 - no configuration detail to caller
            self.logger.warning("V2 games API authorization failed: %s",
                                type(error).__name__)
            return error_response("小游戏管理暂不可用。", status_code=503)
        if not self.ready() or self._service is None:
            return error_response("小游戏服务尚未就绪。", status_code=503)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return error_response("请求格式无效。", status_code=400)
            action = payload.get("action")
            if action == "status":
                status = self._service.status()
                status["web"] = {
                    "running": bool(self._web),
                    "port": self._web.port if self._web else None,
                    "error": self._web_error,
                }
                return {"status": "ok", "data": status, "message": ""}
            if action == "groups":
                groups = self._service.store.list_groups()
                base = self._service.public_base_url
                for group in groups:
                    group["link"] = f"{base}/g/{group['token']}" if base else ""
                return {"status": "ok", "data": {"groups": groups}, "message": ""}
            if action == "leaderboard":
                group_id = payload.get("group_id") or None
                if not isinstance(group_id, str):
                    raise ValueError
                period = payload.get("period", "all")
                kind = payload.get("kind", "all")
                limit = int(payload.get("limit", 20))
                return {"status": "ok", "message": "", "data": self._service.admin_leaderboard(
                    group_id=group_id, period=period, kind=kind, limit=limit)}
            if action == "runs":
                limit = int(payload.get("limit", 50))
                return {"status": "ok", "message": "",
                        "data": {"rows": self._service.admin_runs(limit)}}
            return error_response("不支持此管理操作。", status_code=400)
        except (ValueError, TypeError):
            return error_response("请求参数无效。", status_code=400)
        except Exception as error:  # noqa: BLE001 - no internal detail to caller
            self.logger.warning("V2 games management failed: %s", type(error).__name__,
                                exc_info=True)
            return error_response("小游戏管理操作未完成，请刷新后重试。", status_code=500)

    async def terminate(self) -> None:
        self._closing = True
        async with self._start_lock:
            web, self._web = self._web, None
            if web is not None:
                try:
                    await web.close()
                except Exception as error:  # noqa: BLE001 - shutdown must continue
                    self.logger.warning("V2 games web shutdown failed: %s",
                                        type(error).__name__)
            if self._store is not None:
                self._store.close()
                self._store = None
            self._service = None
            self._initialized = False


__all__ = ["V2Games"]
