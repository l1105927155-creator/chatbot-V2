"""Event-bound native QQ messaging tools for the DSH router."""

from __future__ import annotations

import asyncio
import re
from typing import Any


_USER_ID = re.compile(r"[1-9][0-9]*\Z")
_INVALID_MENTION = "提及对象无效或不在当前群。"
_GROUP_UNAVAILABLE = "无法确认当前 QQ 群及成员列表。"
_QUOTE_UNAVAILABLE = "无法引用这条会话中的消息。"


class QQMessaging:
    """Send native QQ components bound to one incoming AstrBot event."""

    def __init__(self, event: Any, journal: Any, conversation: Any) -> None:
        self.event = event
        self.journal = journal
        self.conversation = conversation
        self._history_after = 0
        self._history_through: int | None = None
        self._mention_user_ids: list[str] = []
        self._reply_to_journal_id: int | None = None

    @property
    def history_after(self) -> int:
        return self._history_after

    @history_after.setter
    def history_after(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("历史起点必须是非负整数。")
        self._history_after = value

    @property
    def history_through(self) -> int | None:
        return self._history_through

    @history_through.setter
    def history_through(self, value: int | None) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError("历史终点必须是正整数或 None。")
        self._history_through = value

    @staticmethod
    def _mentions(value: list[str]) -> list[str]:
        if not isinstance(value, list) or len(value) > 10:
            raise ValueError("提及列表最多包含 10 个有效用户。")
        result: list[str] = []
        seen: set[str] = set()
        for user_id in value:
            if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
                raise ValueError(_INVALID_MENTION)
            if user_id not in seen:
                seen.add(user_id)
                result.append(user_id)
        return result

    @staticmethod
    def _reply_id(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("引用消息编号必须是正整数。")
        return value

    @staticmethod
    def _member_value(member: Any, name: str) -> Any:
        if isinstance(member, dict):
            return member.get(name)
        return getattr(member, name, None)

    def _current_group_id(self) -> str | None:
        try:
            group_id = self.event.get_group_id()
        except (AttributeError, TypeError):
            return None
        if group_id is None or str(group_id) == "":
            return None
        group_id = str(group_id)
        if getattr(self.conversation, "key", None) != f"group:{group_id}":
            raise ValueError(_GROUP_UNAVAILABLE)
        return group_id

    async def _group(self, *, require_members: bool):
        group_id = self._current_group_id()
        if group_id is None:
            raise ValueError("此操作需要当前 QQ 群。")
        try:
            group = await asyncio.wait_for(self.event.get_group(), timeout=5)
        except Exception as exc:
            raise ValueError(_GROUP_UNAVAILABLE) from exc
        if group is None or str(getattr(group, "group_id", "")) != group_id:
            raise ValueError(_GROUP_UNAVAILABLE)
        members = getattr(group, "members", None)
        if require_members and members is None:
            raise ValueError(_GROUP_UNAVAILABLE)
        return group

    def _check_members(self, ids: list[str], group: Any) -> None:
        if not ids:
            return
        members = getattr(group, "members", None)
        if members is None:
            raise ValueError(_GROUP_UNAVAILABLE)
        available = {
            str(self._member_value(member, "user_id"))
            for member in members
            if self._member_value(member, "user_id") is not None
        }
        if any(user_id not in available for user_id in ids):
            raise ValueError(_INVALID_MENTION)

    async def _resolve_reply(self, reply_id: int | None):
        if reply_id is None:
            return None
        if self.history_through is not None and reply_id > self.history_through:
            raise ValueError(_QUOTE_UNAVAILABLE)
        try:
            rows = await asyncio.to_thread(
                self.journal.before,
                self.conversation,
                reply_id + 1,
                1,
                after_id=self.history_after,
            )
        except Exception as exc:
            raise ValueError(_QUOTE_UNAVAILABLE) from exc
        if not rows or rows[-1].journal_id != reply_id:
            raise ValueError(_QUOTE_UNAVAILABLE)
        message = rows[-1].message
        if (message.conversation_key != self.conversation.key
                or not isinstance(message.message_id, str)
                or not message.message_id
                or message.content.get("message_id_is_synthetic") is True):
            raise ValueError(_QUOTE_UNAVAILABLE)
        return message.message_id

    async def _validated_options(self, mention_user_ids, reply_to_journal_id):
        try:
            is_qq = self.event.get_platform_name() == "aiocqhttp"
        except (AttributeError, TypeError):
            is_qq = False
        if not is_qq:
            raise ValueError("此能力仅支持 QQ 消息。")
        ids = self._mentions(mention_user_ids)
        reply_id = self._reply_id(reply_to_journal_id)
        # Bind every operation to the group reported by the incoming event.
        group_id = self._current_group_id()
        group = None
        if ids:
            if group_id is None:
                raise ValueError(_INVALID_MENTION)
            group = await self._group(require_members=bool(ids))
        self._check_members(ids, group)
        platform_reply_id = await self._resolve_reply(reply_id)
        return ids, platform_reply_id

    @staticmethod
    def _build(text: str, ids: list[str], platform_reply_id: str | None):
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Plain, Reply

        chain = MessageChain()
        if platform_reply_id is not None:
            chain.chain.append(Reply(id=platform_reply_id))
        chain.chain.extend(At(name="", qq=user_id) for user_id in ids)
        chain.chain.append(Plain(text))
        return chain

    async def prepare_reply(
        self,
        mention_user_ids: list[str],
        reply_to_journal_id: int | None,
    ) -> None:
        ids, _ = await self._validated_options(mention_user_ids, reply_to_journal_id)
        # Replace only after every validation succeeds, so errors preserve state.
        self._mention_user_ids = ids
        self._reply_to_journal_id = reply_to_journal_id

    async def send_origin(
        self,
        text: str,
        mention_user_ids: list[str],
        reply_to_journal_id: int | None,
    ):
        if not isinstance(text, str):
            raise ValueError("消息内容必须是文本。")
        ids, platform_reply_id = await self._validated_options(
            mention_user_ids, reply_to_journal_id
        )
        return await self.event.send(self._build(text, ids, platform_reply_id))

    async def send_final(self, text: str):
        if not isinstance(text, str):
            raise ValueError("消息内容必须是文本。")
        ids, platform_reply_id = await self._validated_options(
            self._mention_user_ids, self._reply_to_journal_id
        )
        return await self.event.send(self._build(text, ids, platform_reply_id))

    async def members(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        try:
            is_qq = self.event.get_platform_name() == "aiocqhttp"
        except (AttributeError, TypeError):
            is_qq = False
        if not is_qq:
            raise ValueError("此能力仅支持 QQ 消息。")
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError("成员搜索词最多 100 个字符。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("成员数量必须在 1 到 100 之间。")
        group = await self._group(require_members=True)
        normalized = query.casefold()
        matches = []
        for member in group.members:
            user_id = self._member_value(member, "user_id")
            if user_id is None:
                continue
            user_id = str(user_id)
            nickname = self._member_value(member, "nickname") or ""
            nickname = str(nickname)
            if normalized and normalized not in user_id.casefold() and normalized not in nickname.casefold():
                continue
            matches.append({"user_id": user_id, "nickname": nickname})
        matches.sort(key=lambda item: (int(item["user_id"]) if item["user_id"].isdigit() else 0,
                                       item["user_id"]))
        return {"members": matches[:limit], "truncated": len(matches) > limit}
