"""Require a leading QQ Bot mention for native group command handlers."""

from collections.abc import Mapping

from astrbot.core.star.filter import HandlerFilter
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter


class GroupCommandMentionFilter(HandlerFilter):
    _v2_group_command_mention = True

    def filter(self, event, cfg):
        if event.get_platform_name() != "aiocqhttp" or event.is_private_chat():
            return True
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, Mapping) or raw.get("message_type") != "group":
            return False
        bot = str(raw.get("self_id") or "")
        segments = raw.get("message")
        if not bot or not isinstance(segments, list):
            return False
        for segment in segments:
            if not isinstance(segment, Mapping) or not isinstance(segment.get("data"), Mapping):
                return False
            data = segment["data"]
            if segment.get("type") == "text" and not str(data.get("text", "")).strip():
                continue
            return segment.get("type") == "at" and str(data.get("qq", "")) == bot
        return False


def install(registry):
    for handler in registry:
        filters = handler.event_filters
        if any(isinstance(item, (CommandFilter, CommandGroupFilter)) for item in filters):
            if not any(getattr(item, "_v2_group_command_mention", False) for item in filters):
                filters.insert(0, GroupCommandMentionFilter())


def restore(registry):
    for handler in registry:
        handler.event_filters[:] = [item for item in handler.event_filters
                                   if not getattr(item, "_v2_group_command_mention", False)]
