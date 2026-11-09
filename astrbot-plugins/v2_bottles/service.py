"""Command parsing and user-facing text for the V2 drift-bottle pool."""

from __future__ import annotations

from typing import Any

from .settings import SettingsStore
from .store import BottleLimitError, BottleStore, format_bottle_time
from .texts import TextStore

PICK = "pick"
RULES = "rules"
THROW = "throw"
THROW_ANONYMOUS = "throw_anonymous"


class BottleError(Exception):
    """A user-facing refusal with a machine-readable reason."""

    def __init__(self, kind: str, **details: Any) -> None:
        super().__init__(kind)
        self.kind = kind
        self._public_message = details.pop("_public_message", None)
        self.details = details

    @property
    def public_message(self) -> str:
        if isinstance(self._public_message, str):
            return self._public_message
        if self.kind == "empty":
            return "漂流瓶内容不能为空。"
        if self.kind == "too_long":
            return (f"漂流瓶内容 {self.details.get('length', 0)} 字，"
                    f"超过单条上限 {self.details.get('limit', 0)} 字，没有存入。")
        if self.kind == "count":
            return (f"漂流瓶池已满（上限 {self.details.get('limit', 0)} 条），"
                    "暂时不能再扔。")
        if self.kind == "disk":
            return (f"漂流瓶存储空间已满（上限 {self.details.get('limit_mb', 0)} MB），"
                    "暂时不能再扔。")
        return "漂流瓶操作未完成，请稍后再试。"


def parse_command(text: str) -> tuple[str, str] | None:
    """Match only whole-message commands; throw commands require one space.

    ``捡`` and ``漂流瓶`` are exact commands. ``扔`` and ``偷偷扔`` are only
    commands when followed by a literal space, so a normal sentence such as
    ``扔了一只猫`` stays in the DSH chat path. No colon or no-space shorthand
    is accepted.
    """
    text = str(text or "").strip()
    if text == "捡":
        return (PICK, "")
    if text == "漂流瓶":
        return (RULES, "")
    if text.startswith("偷偷扔 "):
        return (THROW_ANONYMOUS, text[len("偷偷扔"):].strip())
    if text.startswith("扔 "):
        return (THROW, text[len("扔"):].strip())
    return None


def _positive_int(config: Any, key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (AttributeError, TypeError, ValueError):
        return default
    return value if value > 0 else default


def _string_list(config: Any, key: str) -> list[str]:
    try:
        value = config.get(key, [])
    except (AttributeError, TypeError, ValueError):
        return []
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            item = str(item)
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


class BottleService:
    def __init__(self, store: BottleStore, config: Any, logger=None,
                 texts: TextStore | None = None,
                 settings: SettingsStore | None = None) -> None:
        self.store = store
        self.logger = logger
        self.texts = texts if texts is not None else TextStore(None)
        self.settings = (
            settings
            if settings is not None
            else SettingsStore(
                None,
                {"max_chars": _positive_int(config, "max_chars", 200)},
            )
        )
        self.max_bottles = _positive_int(config, "max_bottles", 1000)
        self.max_disk_mb = _positive_int(config, "max_disk_mb", 1024)
        self.max_disk_bytes = self.max_disk_mb * 1024 * 1024
        self.enabled_groups = set(_string_list(config, "enabled_groups"))

    @property
    def max_chars(self) -> int:
        return self.settings.get("max_chars")

    def text(self, key: str, **fields: Any) -> str:
        return self.texts.get(key, **fields)

    def group_enabled(self, group_id: str) -> bool:
        """A group must be explicitly listed before any bottle command runs."""
        return str(group_id) in self.enabled_groups

    def throw(self, *, content: str, group_id: str, sender_qq: str,
              sender_name: str, anonymous: bool) -> str:
        content = str(content or "").strip()
        if not content:
            raise BottleError("empty", _public_message=self.text("empty_content"))
        if len(content) > self.max_chars:
            raise BottleError(
                "too_long",
                length=len(content),
                limit=self.max_chars,
                _public_message=self.text("too_long", length=len(content),
                                         limit=self.max_chars),
            )
        try:
            self.store.add(
                content=content,
                group_id=group_id,
                sender_qq=sender_qq,
                sender_name=sender_name,
                anonymous=anonymous,
                max_count=self.max_bottles,
                max_disk_bytes=self.max_disk_bytes,
            )
        except BottleLimitError as error:
            if error.kind == "count":
                raise BottleError(
                    "count",
                    limit=self.max_bottles,
                    _public_message=self.text("pool_full", limit=self.max_bottles),
                ) from error
            raise BottleError(
                "disk",
                limit_mb=self.max_disk_mb,
                _public_message=self.text("disk_full", limit_mb=self.max_disk_mb),
            ) from error
        if anonymous:
            return self.text("throw_anonymous_success")
        return self.text("throw_success")

    def pick(self, bot_id: str | None = None) -> str:
        row = self.store.random_one()
        if row is None:
            return self.text("empty_pool")
        display_time = row.get("created_at_text")
        if not isinstance(display_time, str) or not display_time.strip():
            display_time = format_bottle_time(row["created_at"])
        lines = [
            self.text("pick_header", time=display_time),
            str(row["content"]),
        ]
        if bool(row["anonymous"]):
            lines.append(self.text("anonymous_source"))
        else:
            name = row["sender_name"] or "未知昵称"
            source_key = (
                "bot_public_source"
                if bot_id and str(row["sender_qq"]) == str(bot_id)
                else "public_source"
            )
            lines.append(self.text(
                source_key,
                group_id=row["group_id"],
                sender_name=name,
                sender_qq=row["sender_qq"],
            ))
        return "\n".join(lines)

    def rules_texts(self) -> list[str]:
        """Return the merged-forward nodes; the first line reports the total."""
        total = self.store.count()
        disk_mb = f"{self.store.disk_bytes() / (1024 * 1024):.2f}"
        return [
            self.text("rules_summary", count=total),
            self.text("rules_usage"),
            self.text(
                "rules_limits",
                max_chars=self.max_chars,
                max_bottles=self.max_bottles,
                max_disk_mb=self.max_disk_mb,
                disk_mb=disk_mb,
            ),
        ]

    def status(self) -> dict[str, Any]:
        return {
            "count": self.store.count(),
            "max_bottles": self.max_bottles,
            "max_chars": self.max_chars,
            "disk_bytes": self.store.disk_bytes(),
            "max_disk_bytes": self.max_disk_bytes,
            "enabled_groups": sorted(self.enabled_groups),
        }

    def recent(self, limit: int = 50, *, group_id: str | None = None,
               qq: str | None = None) -> list[dict[str, Any]]:
        return self.store.recent(limit, group_id=group_id, qq=qq)


__all__ = [
    "BottleError",
    "BottleService",
    "PICK",
    "RULES",
    "THROW",
    "THROW_ANONYMOUS",
    "parse_command",
]
