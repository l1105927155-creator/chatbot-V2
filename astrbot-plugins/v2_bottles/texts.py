"""Owner-editable user-facing text templates for the drift-bottle plugin."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_FIELD_LENGTH = 4000

DEFAULT_TEXTS: dict[str, str] = {
    "empty_pool": "漂流瓶池还是空的。先发 @Bot 扔 内容 扔一个吧。",
    "throw_success": "你的瓶子已经扔向大海啦",
    "throw_anonymous_success": "你的瓶子已经偷偷扔向大海啦",
    "pick_header": "{time}",
    "anonymous_source": "—— 匿名漂流瓶（来源仅后台可查）",
    "public_source": "—— {group_id} - {sender_name} - {sender_qq}",
    "bot_public_source": "—— {group_id} - {sender_name}（Bot） - {sender_qq}",
    "empty_content": "漂流瓶内容不能为空。",
    "too_long": "漂流瓶内容 {length} 字，超过单条上限 {limit} 字，没有存入。",
    "pool_full": "漂流瓶池已满（上限 {limit} 条），暂时不能再扔。",
    "disk_full": "漂流瓶存储空间已满（上限 {limit_mb} MB），暂时不能再扔。",
    "media_rejected": "暂不支持图片",
    "usage_hint": "用法：@Bot 扔 内容（命令和内容之间必须有空格）。",
    "not_ready": "漂流瓶服务尚未就绪，请稍后再试。",
    "pick_error": "漂流瓶暂时无法拾取，请稍后再试。",
    "throw_error": "漂流瓶操作未完成，请稍后再试。",
    "rules_error": "漂流瓶说明暂时无法生成，请稍后再试。",
    "rules_summary": (
        "当前共有 {count} 个漂流瓶。\n"
        "漂流瓶是公共的：任何群捡到的都可能是别的群扔的；拾取不会让瓶子消失。"
    ),
    "rules_usage": (
        "【用法】\n"
        "@Bot 捡：随机捡一个\n"
        "@Bot 漂流瓶：查看本说明\n"
        "@Bot 扔 内容：非匿名投放，被捡到时显示来源群、昵称和 QQ\n"
        "@Bot 偷偷扔 内容：匿名投放，被捡到时只显示匿名，后台仍保留来源\n"
        "命令和内容之间必须有空格。没有空格（如 @Bot 扔了一只猫）不会存入。"
    ),
    "rules_limits": (
        "【限制】\n"
        "单条最多 {max_chars} 字；第一版只支持文本，暂不支持图片。\n"
        "池子上限 {max_bottles} 条、存储上限 {max_disk_mb} MB；当前已用 {disk_mb} MB。\n"
        "达到任一上限后停止存入并提示。"
    ),
}

TEXT_FIELDS: list[dict[str, Any]] = [
    {"key": "empty_pool", "label": "空池提示", "hint": "捡到空池时显示。", "rows": 2},
    {"key": "throw_success", "label": "投放成功提示", "hint": "非匿名投放成功时显示。", "rows": 1},
    {"key": "throw_anonymous_success", "label": "匿名投放成功提示", "hint": "匿名投放成功时显示。", "rows": 1},
    {"key": "pick_header", "label": "拾取首行", "hint": "可用 {time}；默认只有时间。", "rows": 1},
    {"key": "anonymous_source", "label": "匿名来源行", "hint": "匿名瓶公开显示的来源行。", "rows": 1},
    {"key": "public_source", "label": "公开来源行", "hint": "用户非匿名瓶使用；可用 {group_id}、{sender_name}、{sender_qq}。", "rows": 1},
    {"key": "bot_public_source", "label": "Bot 公开来源行", "hint": "Bot 非匿名瓶使用；可用 {group_id}、{sender_name}、{sender_qq}。", "rows": 1},
    {"key": "empty_content", "label": "空内容提示", "hint": "内容为空时显示。", "rows": 1},
    {"key": "too_long", "label": "超长提示", "hint": "可用 {length}、{limit}。", "rows": 2},
    {"key": "pool_full", "label": "池满提示", "hint": "可用 {limit}。", "rows": 2},
    {"key": "disk_full", "label": "磁盘满提示", "hint": "可用 {limit_mb}。", "rows": 2},
    {"key": "media_rejected", "label": "媒体拒绝提示", "hint": "投放带图片/文件时显示。", "rows": 1},
    {"key": "usage_hint", "label": "用法提示", "hint": "命令缺少内容时显示。", "rows": 1},
    {"key": "not_ready", "label": "服务未就绪提示", "hint": "插件尚未初始化时显示。", "rows": 1},
    {"key": "pick_error", "label": "拾取异常提示", "hint": "拾取内部异常时显示。", "rows": 1},
    {"key": "throw_error", "label": "投放异常提示", "hint": "投放内部异常时显示。", "rows": 1},
    {"key": "rules_error", "label": "规则异常提示", "hint": "生成规则转发失败时显示。", "rows": 1},
    {"key": "rules_summary", "label": "规则首段", "hint": "可用 {count}；显示为合并转发第一段。", "rows": 3},
    {"key": "rules_usage", "label": "规则用法段", "hint": "显示为合并转发第二段。", "rows": 5},
    {"key": "rules_limits", "label": "规则限制段", "hint": "可用 {max_chars}、{max_bottles}、{max_disk_mb}、{disk_mb}。", "rows": 4},
]


def _render(template: str, fields: Mapping[str, Any]) -> str:
    """Replace known placeholders only; leave any other braces untouched."""
    result = template
    for key, value in fields.items():
        result = result.replace("{" + key + "}", str(value))
    return result


class TextStore:
    """Keep prompt texts in memory and persist Owner edits atomically."""

    def __init__(self, path: Path | None, logger=None) -> None:
        self.path = Path(path) if path is not None else None
        self.logger = logger
        self._lock = threading.RLock()
        self._values = dict(DEFAULT_TEXTS)
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if self.logger:
                self.logger.warning("V2 bottle texts file is unreadable; using defaults")
            return
        if not isinstance(data, dict):
            return
        for key in DEFAULT_TEXTS:
            value = data.get(key)
            if isinstance(value, str) and len(value) <= MAX_FIELD_LENGTH:
                self._values[key] = value

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._values)

    @staticmethod
    def defaults() -> dict[str, str]:
        return dict(DEFAULT_TEXTS)

    def update(self, values: Mapping[str, Any]) -> list[str]:
        if not isinstance(values, Mapping):
            raise ValueError("texts must be a mapping")
        validated: dict[str, str] = {}
        for key, value in values.items():
            if key not in DEFAULT_TEXTS:
                raise ValueError(f"unknown text key: {key}")
            if not isinstance(value, str):
                raise ValueError(f"text {key} must be a string")
            if len(value) > MAX_FIELD_LENGTH:
                raise ValueError(f"text {key} is too long")
            validated[key] = value
        with self._lock:
            self._values.update(validated)
            self._write()
        return sorted(validated)

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._values, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self.path)

    def get(self, key: str, **fields: Any) -> str:
        with self._lock:
            template = self._values.get(key, DEFAULT_TEXTS[key])
        return _render(template, fields)


__all__ = [
    "DEFAULT_TEXTS",
    "MAX_FIELD_LENGTH",
    "TEXT_FIELDS",
    "TextStore",
    "_render",
]
