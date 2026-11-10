"""AstrBot host for the V2 text-only public drift-bottle pool."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.web import error_response, request

from .service import (
    PICK,
    RULES,
    THROW_ANONYMOUS,
    BottleError,
    BottleService,
    parse_command,
)
from .settings import SETTINGS_FIELDS, SettingsStore
from .store import BottleStore
from .texts import DEFAULT_TEXTS, TEXT_FIELDS, TextStore

_MEDIA_SEGMENTS = frozenset(
    {"image", "record", "video", "file", "forward", "json", "redbag"}
)


class BottleCommandFilter(CustomFilter):
    """Match a group message that mentions this Bot and is a bottle command."""

    def filter(self, event, cfg):
        if event.get_platform_name() != "aiocqhttp" or event.is_private_chat():
            return False
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, Mapping) or raw.get("message_type") != "group":
            return False
        bot_id = str(raw.get("self_id") or "")
        segments = raw.get("message")
        if not bot_id or not isinstance(segments, list):
            return False
        for segment in segments:
            if not isinstance(segment, Mapping) or not isinstance(
                segment.get("data"), Mapping
            ):
                return False
            data = segment["data"]
            if segment.get("type") == "text" and not str(data.get("text", "")).strip():
                continue
            if not (segment.get("type") == "at" and str(data.get("qq", "")) == bot_id):
                return False
            break
        else:
            return False
        text = event.get_message_str()
        if parse_command(text) is not None:
            return True
        return _has_media(event) and _throw_keyword_without_content(text)


def _config_int(config, key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (AttributeError, TypeError, ValueError):
        return default
    return value if value > 0 else default


def _throw_keyword_without_content(text: str) -> bool:
    """True when the text is only the throw keyword, with no argument."""
    return str(text or "").strip() in {"扔", "偷偷扔"}


def _has_media(event: AstrMessageEvent) -> bool:
    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "raw_message", None)
    if not isinstance(raw, Mapping):
        return False
    segments = raw.get("message")
    if not isinstance(segments, list):
        return False
    return any(
        isinstance(segment, Mapping) and segment.get("type") in _MEDIA_SEGMENTS
        for segment in segments
    )


class BottleCapability:
    """Bot-facing throw operation bound to the current conversation."""

    def __init__(self, service: BottleService, conversation, bot_name: str | None = None) -> None:
        self._service = service
        self._conversation = conversation
        self._bot_name = bot_name or "小v"

    def throw(self, content: str, anonymous: bool = False) -> dict:
        key = str(getattr(self._conversation, "key", ""))
        if not key.startswith("group:"):
            return {"ok": False, "error": "not_group", "message": "漂流瓶仅支持群聊。"}
        group_id = key.split(":", 1)[1]
        if not self._service.group_enabled(group_id):
            return {"ok": False, "error": "disabled_group", "message": "本群未启用漂流瓶。"}
        if not isinstance(content, str):
            return {"ok": False, "error": "invalid", "message": "漂流瓶内容必须是文本。"}
        try:
            self._service.throw(
                content=content,
                group_id=group_id,
                sender_qq=str(getattr(self._conversation, "bot_id", "")),
                sender_name=self._bot_name,
                anonymous=bool(anonymous),
            )
        except BottleError as error:
            return {"ok": False, "error": error.kind, "message": error.public_message}
        except Exception:
            return {"ok": False, "error": "throw_failed", "message": "漂流瓶投放未完成。"}
        return {"ok": True, "anonymous": bool(anonymous), "length": len(content.strip())}


@star.register(
    "v2_bottles", "chatbot-V2",
    "群聊文本漂流瓶：投放、匿名投放、随机捡取与可编辑提示文本后台。",
    "0.1.0",
)
class V2Bottles(star.Star):
    def __init__(self, context, config=None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._store: BottleStore | None = None
        self._texts: TextStore | None = None
        self._settings: SettingsStore | None = None
        self._service: BottleService | None = None
        self._initialized = False
        self._closing = False

    async def initialize(self) -> None:
        self._closing = False
        data_dir = Path(star.StarTools.get_data_dir("v2_bottles"))
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._store = BottleStore(data_dir / "bottles.sqlite3")
        self._texts = TextStore(data_dir / "texts.json", self.logger)
        self._settings = SettingsStore(
            data_dir / "settings.json",
            {"max_chars": _config_int(self.config, "max_chars", 200)},
            self.logger,
        )
        self._service = BottleService(
            self._store, self.config, self.logger, self._texts,
            settings=self._settings,
        )
        self.context.register_web_api(
            "/v2_bottles/api", self.api, ["POST"], "V2 漂流瓶后台"
        )
        self._initialized = True
        self.logger.info(
            "V2 bottles ready (max=%s chars=%s disk_mb=%s)",
            self._service.max_bottles,
            self._service.max_chars,
            self._service.max_disk_mb,
        )

    def capability_for(self, conversation, actor_id=None, bot_name=None):
        if not self.ready():
            raise RuntimeError("漂流瓶服务尚未就绪")
        return BottleCapability(self._service, conversation, bot_name)

    def ready(self) -> bool:
        return bool(
            self._initialized
            and not self._closing
            and self._service is not None
            and self._store is not None
            and self._texts is not None
            and self._settings is not None
        )

    @filter.custom_filter(BottleCommandFilter)
    async def bottle_command(self, event: AstrMessageEvent):
        """Handle the four exact bottle commands after a real @Bot mention."""
        text = event.get_message_str()
        parsed = parse_command(text)
        media = _has_media(event)
        media_throw = (
            parsed is None and media and _throw_keyword_without_content(text)
        )
        if parsed is None and not media_throw:
            return
        action, content = parsed if parsed is not None else (None, "")
        if not self.ready():
            event.set_extra("v2_deterministic_handled", True)
            notice = (
                self._texts.get("not_ready")
                if self._texts is not None
                else DEFAULT_TEXTS["not_ready"]
            )
            yield event.plain_result(notice)
            return
        group_id = str(event.get_group_id() or "")
        if not self._service.group_enabled(group_id):
            # Disabled groups keep the normal DSH chat path; this plugin must
            # not claim events while it is not enabled for that group.
            return
        event.set_extra("v2_deterministic_handled", True)
        if action == RULES:
            try:
                nodes = [
                    Node(
                        content=[Plain(text=text)],
                        name="漂流瓶",
                        uin=str(event.get_self_id() or "0"),
                    )
                    for text in self._service.rules_texts()
                ]
                yield event.chain_result([Nodes(nodes)])
            except Exception as error:  # noqa: BLE001 - command text must stay user-safe
                self.logger.warning(
                    "V2 bottle rules failed: %s", type(error).__name__, exc_info=True
                )
                yield event.plain_result(self._service.text("rules_error"))
            return
        if action == PICK:
            try:
                yield event.plain_result(
                    self._service.pick(bot_id=str(event.get_self_id() or ""))
                )
            except Exception as error:  # noqa: BLE001 - command text must stay user-safe
                self.logger.warning(
                    "V2 bottle pick failed: %s", type(error).__name__, exc_info=True
                )
                yield event.plain_result(self._service.text("pick_error"))
            return
        if media:
            yield event.plain_result(self._service.text("media_rejected"))
            return
        if not content:
            yield event.plain_result(self._service.text("usage_hint"))
            return
        try:
            text = self._service.throw(
                content=content,
                group_id=group_id,
                sender_qq=str(event.get_sender_id() or ""),
                sender_name=str(event.get_sender_name() or ""),
                anonymous=(action == THROW_ANONYMOUS),
            )
            yield event.plain_result(text)
        except BottleError as error:
            yield event.plain_result(error.public_message)
        except Exception as error:  # noqa: BLE001 - command text must stay user-safe
            self.logger.warning(
                "V2 bottle throw failed: %s", type(error).__name__, exc_info=True
            )
            yield event.plain_result(self._service.text("throw_error"))

    # ---- Dashboard API: read-only records, editable prompt texts --------

    async def api(self):
        try:
            dashboard = self.context.get_config().get("dashboard", {})
            owner = dashboard.get("username")
            if not isinstance(owner, str) or not owner or request.username != owner:
                raise PermissionError
        except PermissionError:
            return error_response(
                "当前 Dashboard 身份无权查看漂流瓶后台。", status_code=403
            )
        except Exception as error:  # noqa: BLE001 - no configuration detail to caller
            self.logger.warning(
                "V2 bottles API authorization failed: %s", type(error).__name__
            )
            return error_response("漂流瓶后台暂不可用。", status_code=503)
        if not self.ready():
            return error_response("漂流瓶服务尚未就绪。", status_code=503)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return error_response("请求格式无效。", status_code=400)
            action = payload.get("action")
            if action == "status":
                return {
                    "status": "ok",
                    "data": self._service.status(),
                    "message": "",
                }
            if action == "list":
                limit = int(payload.get("limit", 50))
                group_id = payload.get("group_id") or None
                qq = payload.get("qq") or None
                if group_id is not None and (
                    not isinstance(group_id, str) or len(group_id) > 64
                ):
                    raise ValueError
                if qq is not None and (not isinstance(qq, str) or len(qq) > 32):
                    raise ValueError
                return {
                    "status": "ok",
                    "data": {
                        "bottles": self._service.recent(
                            limit, group_id=group_id, qq=qq
                        )
                    },
                    "message": "",
                }
            if action == "texts":
                return {
                    "status": "ok",
                    "data": {
                        "texts": self._texts.snapshot(),
                        "defaults": self._texts.defaults(),
                        "fields": TEXT_FIELDS,
                    },
                    "message": "",
                }
            if action == "texts.save":
                texts = payload.get("texts")
                if not isinstance(texts, dict):
                    raise ValueError
                self._texts.update(texts)
                return {
                    "status": "ok",
                    "data": {"texts": self._texts.snapshot()},
                    "message": "提示文本已保存，立即生效。",
                }
            if action == "settings":
                return {
                    "status": "ok",
                    "data": {
                        "settings": self._settings.snapshot(),
                        "defaults": self._settings.defaults(),
                        "fields": SETTINGS_FIELDS,
                    },
                    "message": "",
                }
            if action == "settings.save":
                settings = payload.get("settings")
                if not isinstance(settings, dict):
                    raise ValueError
                self._settings.update(settings)
                return {
                    "status": "ok",
                    "data": {"settings": self._settings.snapshot()},
                    "message": "运行设置已保存，立即生效。",
                }
            if action == "delete":
                ids = payload.get("ids")
                if not isinstance(ids, list) or not ids or len(ids) > 200:
                    raise ValueError
                if any(type(item) is not int or item <= 0 for item in ids):
                    raise ValueError
                deleted = self._store.delete(ids)
                return {
                    "status": "ok",
                    "data": {"deleted": deleted},
                    "message": f"已删除 {deleted} 条漂流瓶。",
                }
            return error_response("不支持此管理操作。", status_code=400)
        except (ValueError, TypeError):
            return error_response("请求参数无效。", status_code=400)
        except Exception as error:  # noqa: BLE001 - no internal detail to caller
            self.logger.warning(
                "V2 bottles API failed: %s", type(error).__name__, exc_info=True
            )
            return error_response(
                "漂流瓶后台操作未完成，请刷新后重试。", status_code=500
            )

    async def terminate(self) -> None:
        self._closing = True
        if self._store is not None:
            self._store.close()
            self._store = None
        self._texts = None
        self._settings = None
        self._service = None
        self._initialized = False


__all__ = ["BottleCapability", "BottleCommandFilter", "V2Bottles"]
