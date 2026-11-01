"""Bounded, event-bound QQ forward reading and native forward sending."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit


MAX_DEPTH = 6
MAX_NODES = 50
MAX_TEXT = 40_000
MAX_SEGMENT_TEXT = 4_000
MAX_NODE_TEXT = 12_000
MAX_SEGMENTS_PER_NODE = 100
MAX_FORWARD_IMAGE_BYTES = 200 * 1024 * 1024
_MEDIA_KEYS = frozenset({"file_id", "file", "id", "res_id", "file_name", "name",
                         "file_size", "size", "duration", "sub_type", "subtype",
                         "summary", "md5", "busid", "cache", "media_type"})
_OPAQUE = re.compile(r"^[^\x00-\x20]{1,2048}$")


class QQForwards:
    """Read and send forwarded messages through the current QQ AstrBot event."""

    def __init__(self, messaging: Any) -> None:
        self.messaging = messaging
        self.event = messaging.event
        self.journal = messaging.journal
        self.conversation = messaging.conversation

    @property
    def history_after(self) -> int:
        return self.messaging.history_after

    @property
    def history_through(self) -> int | None:
        return self.messaging.history_through

    def _is_qq(self) -> None:
        try:
            valid = self.event.get_platform_name() == "aiocqhttp"
        except Exception:
            valid = False
        if not valid:
            raise ValueError("转发消息能力仅支持当前 QQ 会话。")

    @staticmethod
    def _path(value: Any) -> list[int]:
        if value is None:
            return []
        # The journal's root forward is depth one; each pair selects one nested
        # forward, so at most five pairs keep the total traversal at six layers.
        if not isinstance(value, (list, tuple)) or len(value) % 2 or len(value) > (MAX_DEPTH - 1) * 2:
            raise ValueError("node_path 必须是最多六层的偶数长度整数数组。")
        if any(type(item) is not int or item < 0 for item in value):
            raise ValueError("node_path 只能包含非负整数。")
        return list(value)

    async def _source_row(self, journal_id: Any):
        if type(journal_id) is not int or journal_id <= self.history_after:
            raise ValueError("转发来源不在当前会话重置后的可见历史中。")
        if self.history_through is not None and journal_id > self.history_through:
            raise ValueError("转发来源超出本轮历史范围。")
        try:
            row = await asyncio.to_thread(self.journal.by_journal_id, self.conversation, journal_id)
        except Exception as exc:
            raise ValueError("无法核实转发来源。") from exc
        if row is None or row.message.conversation_key != self.conversation.key:
            raise ValueError("转发来源不属于当前会话。")
        return row

    @staticmethod
    def _segments(content: Any) -> list[Any]:
        if isinstance(content, list):
            return content
        if isinstance(content, Mapping):
            for key in ("content", "message"):
                value = content.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _kind(segment: Any) -> str:
        return str(segment.get("type", "")).lower() if isinstance(segment, Mapping) else ""

    @staticmethod
    def _data(segment: Mapping[str, Any]) -> Mapping[str, Any]:
        data = segment.get("data")
        return data if isinstance(data, Mapping) else segment

    @staticmethod
    def _nodes(payload: Any) -> list[Any]:
        """Unwrap known OneBot/NapCat response envelopes, failing closed."""
        for _ in range(4):
            if isinstance(payload, list):
                return payload
            if not isinstance(payload, Mapping):
                break
            if isinstance(payload.get("messages"), list):
                return payload["messages"]
            if isinstance(payload.get("data"), (list, Mapping)):
                payload = payload["data"]
                continue
            break
        raise ValueError("NapCat 返回的转发节点格式无法识别。")

    @staticmethod
    def _forward_identifier(segment: Mapping[str, Any]) -> str | None:
        data = QQForwards._data(segment)
        for key in ("id", "res_id", "message_id"):
            value = data.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                text = str(value)
                if _OPAQUE.fullmatch(text) and "://" not in text:
                    return text
        return None

    @staticmethod
    def _inline_forward(segment: Mapping[str, Any]) -> Any | None:
        data = QQForwards._data(segment)
        for key in ("content", "message", "messages"):
            value = data.get(key)
            if isinstance(value, (list, Mapping)):
                return value
        return None

    async def _fetch_forward(self, segment: Mapping[str, Any]) -> list[Any]:
        inline = self._inline_forward(segment)
        if inline is not None:
            return self._nodes(inline)
        forward_id = self._forward_identifier(segment)
        if not forward_id:
            raise ValueError("该转发段没有可展开的 NapCat 转发标识。")
        try:
            result = await self.event.bot.call_action("get_forward_msg", message_id=forward_id)
        except Exception as exc:
            raise ValueError("NapCat 获取转发详情失败。") from exc
        try:
            return self._nodes(result)
        except ValueError as exc:
            raise ValueError("NapCat 返回的转发详情无效。") from exc

    async def _resolve_layer(self, row: Any, segment_index: int, path: list[int]) -> list[Any]:
        content = row.message.content
        segments = content.get("segments") if isinstance(content, Mapping) else None
        if not isinstance(segments, list) or not 0 <= segment_index < len(segments):
            raise ValueError("指定位置不是当前 Journal 消息的有效段。")
        source = segments[segment_index]
        if not isinstance(source, Mapping) or self._kind(source) != "forward":
            raise ValueError("指定位置不是转发消息段。")
        if not self._forward_identifier(source) and content.get("forward_reference_missing") is True:
            source = await self._recover_forward_segment(row, segment_index)
        nodes = await self._fetch_forward(source)
        for depth in range(0, len(path), 2):
            node_index, nested_segment_index = path[depth:depth + 2]
            if node_index >= len(nodes):
                raise ValueError("node_path 中的节点位置越界。")
            node = nodes[node_index]
            data = self._data(node) if isinstance(node, Mapping) else {}
            body = self._segments(data)
            if nested_segment_index >= len(body):
                raise ValueError("node_path 中的消息段位置越界。")
            nested = body[nested_segment_index]
            if not isinstance(nested, Mapping) or self._kind(nested) != "forward":
                raise ValueError("node_path 选择的位置不是嵌套转发段。")
            nodes = await self._fetch_forward(nested)
        return nodes

    async def _recover_forward_segment(self, row: Any, segment_index: int) -> Mapping[str, Any]:
        """Recover one explicitly marked missing receipt reference via its real message."""
        message = row.message
        if (not isinstance(message.message_id, str) or not message.message_id
                or message.content.get("message_id_is_synthetic") is True):
            raise ValueError("转发回执缺少可回查的平台消息编号。")
        try:
            result = await self.event.bot.call_action("get_msg", message_id=message.message_id)
        except Exception as exc:
            raise ValueError("NapCat 无法回查转发回执。") from exc
        raw = result.get("data", result) if isinstance(result, Mapping) else None
        if not isinstance(raw, Mapping) or not isinstance(raw.get("message"), list):
            raise ValueError("NapCat 返回的转发回执格式无效。")
        if raw.get("message_id") is None or str(raw["message_id"]) != message.message_id:
            raise ValueError("NapCat 返回的转发回执编号不一致。")
        self._validate_message_scope(raw, message)
        if segment_index >= len(raw["message"]):
            raise ValueError("NapCat 返回的转发段位置无效。")
        segment = raw["message"][segment_index]
        if not isinstance(segment, Mapping) or self._kind(segment) != "forward":
            raise ValueError("NapCat 回查位置不是转发段。")
        if not self._forward_identifier(segment) and self._inline_forward(segment) is None:
            raise ValueError("NapCat 回查的转发段仍没有可用引用。")
        return segment

    @staticmethod
    def _safe_scalar(value: Any) -> str | int | float | bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and len(value) <= 256 and not value.startswith(("base64://", "data:")):
            return value
        return None

    def _validate_message_scope(self, raw: Mapping[str, Any], message: Any) -> None:
        if self.conversation.key.startswith("group:"):
            if raw.get("group_id") is not None and str(raw["group_id"]) != self.conversation.key.split(":", 1)[1]:
                raise ValueError("NapCat 返回的消息不属于当前群。")
            return
        peer_id = self.conversation.key.split(":", 1)[1]
        source_sender = raw.get("user_id")
        if source_sender is None and isinstance(raw.get("sender"), Mapping):
            source_sender = raw["sender"].get("user_id")
        if message.direction == "outbound":
            if source_sender is not None and str(source_sender) != str(self.conversation.bot_id):
                raise ValueError("NapCat 返回的引用消息发送者与 Bot 不一致。")
            if raw.get("target_id") is not None and str(raw["target_id"]) != peer_id:
                raise ValueError("NapCat 返回的消息不属于当前私聊。")
        elif source_sender is not None and str(source_sender) != peer_id:
            raise ValueError("NapCat 返回的消息不属于当前私聊。")

    def _normalize_content(self, segments: list[Any], path: list[int], node_index: int,
                           text_budget: list[int]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        local_text = 0
        for seg_index, segment in enumerate(segments[:MAX_SEGMENTS_PER_NODE]):
            if not isinstance(segment, Mapping):
                continue
            kind = self._kind(segment)
            data = self._data(segment)
            if kind in {"text", "plain"}:
                raw = data.get("text", "")
                if not isinstance(raw, str):
                    continue
                allowance = min(MAX_SEGMENT_TEXT, MAX_NODE_TEXT - local_text, text_budget[0])
                value = raw[:max(0, allowance)]
                local_text += len(value)
                text_budget[0] -= len(value)
                if value or len(raw) > len(value):
                    normalized.append({"type": "text", "segment_index": seg_index, "text": value,
                                       **({"truncated": True} if len(raw) > len(value) else {})})
                continue
            item: dict[str, Any] = {"type": kind or "unknown", "segment_index": seg_index}
            if kind == "forward":
                # Keep the route marker even for parseMultMsg inline-only subtrees.
                item["node_path"] = path + [node_index, seg_index]
                identifier = self._forward_identifier(segment)
                if identifier:
                    item["available"] = True
                elif self._inline_forward(segment) is not None:
                    item["available"] = True
                else:
                    item["available"] = False
            elif kind in {"image", "record", "video", "file"}:
                metadata = {}
                for key in _MEDIA_KEYS:
                    if key in data:
                        value = self._safe_scalar(data[key])
                        if key in {"file", "file_id", "id", "res_id"}:
                            if not isinstance(value, (str, int)) or isinstance(value, bool):
                                continue
                            value = str(value)
                            if not _OPAQUE.fullmatch(value) or "://" in value or "/" in value or "\\" in value:
                                continue
                        if value is not None:
                            metadata[key] = value
                if metadata:
                    item["metadata"] = metadata
            elif kind == "at":
                for key in ("qq", "name"):
                    value = self._safe_scalar(data.get(key))
                    if value is not None:
                        item[key] = value
            normalized.append(item)
        return normalized

    async def read(self, journal_id: int, segment_index: int, node_path=None,
                   offset: int = 0, limit: int = 10) -> dict[str, Any]:
        self._is_qq()
        path = self._path(node_path)
        if type(segment_index) is not int or segment_index < 0:
            raise ValueError("segment_index 必须是非负整数。")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset 必须是非负整数。")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit 必须在 1 到 20 之间。")
        row = await self._source_row(journal_id)
        nodes = await self._resolve_layer(row, segment_index, path)
        selected = nodes[offset:offset + limit]
        remaining = [MAX_TEXT]
        output = []
        for local_index, raw_node in enumerate(selected, start=offset):
            if not isinstance(raw_node, Mapping):
                raise ValueError("NapCat 返回了无效转发节点。")
            data = self._data(raw_node)
            body = self._segments(data)
            sender = data.get("nickname", data.get("name", data.get("sender_name")))
            if sender is None:
                source_sender = data.get("sender")
                if isinstance(source_sender, Mapping):
                    sender = source_sender.get("card") or source_sender.get("nickname")
                elif isinstance(source_sender, str):
                    sender = source_sender
            if sender is None:
                sender = data.get("user_id")
            sender = self._safe_scalar(sender)
            timestamp = data.get("time")
            timestamp = timestamp if type(timestamp) is int and timestamp >= 0 else None
            normalized = self._normalize_content(body, path, local_index, remaining)
            output.append({"sender": sender, "user_id": self._safe_scalar(data.get("user_id")),
                           "time": timestamp, "content": normalized,
                           "node_index": local_index,
                           "content_truncated": len(body) > MAX_SEGMENTS_PER_NODE})
        return {"nodes": output, "offset": offset, "limit": limit,
                "next_offset": offset + len(output) if offset + len(output) < len(nodes) else None,
                "total": len(nodes), "truncated": False,
                "text_truncated": any(
                    segment.get("truncated") is True
                    for item in output for segment in item["content"]
                )}

    async def read_image(self, journal_id: int, segment_index: int, node_path,
                         node_index: int, image_segment_index: int) -> dict[str, Any]:
        self._is_qq()
        path = self._path(node_path)
        if type(node_index) is not int or node_index < 0 or type(image_segment_index) is not int or image_segment_index < 0:
            raise ValueError("节点和图片段位置必须是非负整数。")
        row = await self._source_row(journal_id)
        nodes = await self._resolve_layer(row, segment_index, path)
        if node_index >= len(nodes):
            raise ValueError("指定转发节点越界。")
        node = nodes[node_index]
        data = self._data(node) if isinstance(node, Mapping) else {}
        segments = self._segments(data)
        if image_segment_index >= len(segments):
            raise ValueError("指定图片段越界。")
        image_segment = segments[image_segment_index]
        if not isinstance(image_segment, Mapping) or self._kind(image_segment) != "image":
            raise ValueError("指定位置不是图片段。")
        return await self._read_validated_image(
            row, image_segment, (journal_id, segment_index, tuple(path), node_index, image_segment_index)
        )

    async def _read_validated_image(self, row: Any, segment: Mapping[str, Any], cache_identity: Any) -> dict[str, Any]:
        from .file_transfer import resolve_onebot_file
        from .vision import QQVision

        data = self._data(segment)
        flat = {"type": "image"}
        for key, value in data.items():
            if key == "url":
                if isinstance(value, str) and len(value) <= 8192:
                    parsed = urlsplit(value)
                    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                        flat[key] = value
                continue
            if key in _MEDIA_KEYS or key == "proxy":
                scalar = self._safe_scalar(value)
                if scalar is not None:
                    flat[key] = scalar
        if not any(isinstance(flat.get(key), str) and flat[key] for key in ("file_id", "file", "id")):
            raise ValueError("图片段缺少可解析的 QQ 资源标识。")

        napcat_url = flat.get("url")

        async def resolve(image_segment):
            # An HTTPS URL embedded by NapCat in the selected forward node is
            # already scoped to this message. Prefer it; never accept a model URL.
            if napcat_url:
                return {"url": napcat_url}
            try:
                return await asyncio.wait_for(
                    resolve_onebot_file(self.event.bot, self.conversation, image_segment), timeout=3
                )
            except Exception:
                raise

        # Use QQVision's validation and a path-specific cache key.
        identity = repr(cache_identity).encode()
        synthetic_id = int.from_bytes(hashlib.sha256(identity).digest()[:7], "big") + 1
        synthetic_message = SimpleNamespace(
            platform_id=row.message.platform_id,
            bot_id=row.message.bot_id,
            conversation_key=row.message.conversation_key,
            content={"segments": [flat]},
        )
        synthetic_row = type("VisionRow", (), {"journal_id": synthetic_id, "message": synthetic_message})()
        result = await QQVision(resolve).prepare(
            [synthetic_row], {synthetic_id}, selected_segments={(synthetic_id, 0)}
        )
        if result["errors"] or not result["images"]:
            raise ValueError("无法安全读取该转发图片。")
        return result["images"][0]

    async def _referenced_components(self, journal_id: int) -> tuple[list[Any], str, str | None]:
        row = await self._source_row(journal_id)
        message = row.message
        if (not isinstance(message.message_id, str) or not message.message_id
                or message.content.get("message_id_is_synthetic") is True):
            raise ValueError("引用来源没有可核实的平台消息编号。")
        try:
            result = await self.event.bot.call_action("get_msg", message_id=message.message_id)
        except Exception as exc:
            raise ValueError("NapCat 无法读取引用消息。") from exc
        raw = result.get("data", result) if isinstance(result, Mapping) else None
        if not isinstance(raw, Mapping) or not isinstance(raw.get("message"), list):
            raise ValueError("NapCat 返回的引用消息格式无效。")
        if raw.get("message_id") is None or str(raw["message_id"]) != message.message_id:
            raise ValueError("NapCat 返回的消息编号与 Journal 来源不一致。")
        self._validate_message_scope(raw, message)
        components = []
        from astrbot.api.message_components import At, Forward, Image, Plain
        if len(raw["message"]) > MAX_SEGMENTS_PER_NODE:
            raise ValueError("引用消息段数量超过安全上限。")
        reference_text = 0
        for segment in raw["message"]:
            if not isinstance(segment, Mapping):
                raise ValueError("引用消息含有不支持的消息段。")
            kind, data = self._kind(segment), self._data(segment)
            if kind == "text":
                text = data.get("text")
                if not isinstance(text, str):
                    raise ValueError("引用消息含有无效文本段。")
                reference_text += len(text)
                if reference_text > MAX_TEXT:
                    raise ValueError("引用消息文本总量超过 40000 字符。")
                components.append(Plain(text))
            elif kind == "at":
                qq = data.get("qq")
                if not isinstance(qq, (str, int)) or isinstance(qq, bool) or not str(qq).isdecimal() or int(qq) <= 0:
                    raise ValueError("引用消息含有不支持的提及对象。")
                components.append(At(qq=str(qq), name=""))
            elif kind == "image":
                image = await self._read_validated_image(
                    row, segment, ("journal-reference", journal_id, len(components))
                )
                components.append(Image.fromBase64(image["data"]))
            elif kind == "forward":
                identity = self._forward_identifier(segment)
                if not identity:
                    raise ValueError("引用的嵌套转发段无法安全生成。")
                components.append(Forward(id=identity))
            else:
                raise ValueError(f"引用消息含有不支持的 {kind or 'unknown'} 消息段。")
        return components, str(message.actor_id), message.actor_name

    async def send(self, nodes: Any):
        self._is_qq()
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
            raise ValueError("nodes 必须是 1 到 50 个节点。")
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Forward, Image, Node, Nodes, Plain

        count = [0]
        text_total = [0]
        image_count = [0]
        image_bytes = [0]
        bot_id = str(self.conversation.bot_id)

        async def build_content(specs: list[Any], depth: int) -> list[Any]:
            if depth > MAX_DEPTH:
                raise ValueError("转发嵌套最多六层。")
            built = []
            for spec in specs:
                if isinstance(spec, str):
                    spec = {"text": spec}
                if not isinstance(spec, Mapping) or len(spec) != 1:
                    raise ValueError("节点内容只能是 text、nodes 或 journal_id 三选一。")
                key, value = next(iter(spec.items()))
                if key == "text":
                    if not isinstance(value, str):
                        raise ValueError("转发文本必须是字符串。")
                    text_total[0] += len(value)
                    if text_total[0] > MAX_TEXT:
                        raise ValueError("转发文本总量超过 40000 字符。")
                    built.append(Plain(value))
                else:
                    raise ValueError("text 节点内容必须是字符串。")
            return built

        async def build_node(spec: Any, depth: int) -> Any:
            if depth > MAX_DEPTH or not isinstance(spec, Mapping) or len(spec) != 1:
                raise ValueError("每个节点必须是 text、nodes 或 journal_id 三选一，且嵌套最多六层。")
            if "text" in spec:
                content = await build_content([{"text": spec["text"]}], depth)
                author_id, author_name = bot_id, ""
            elif "nodes" in spec:
                children = spec["nodes"]
                if not isinstance(children, list) or not children:
                    raise ValueError("嵌套 nodes 必须是非空数组。")
                child_nodes = [await build_node(child, depth + 1) for child in children]
                content = child_nodes
                author_id, author_name = bot_id, ""
            elif "journal_id" in spec:
                if type(spec["journal_id"]) is not int:
                    raise ValueError("journal_id 必须是整数。")
                content, author_id, author_name = await self._referenced_components(spec["journal_id"])
                text_total[0] += sum(len(item.text) for item in content if isinstance(item, Plain))
                if text_total[0] > MAX_TEXT:
                    raise ValueError("转发文本总量超过 40000 字符。")
            else:
                raise ValueError("每个节点必须是 text、nodes 或 journal_id 三选一。")
            count[0] += 1
            if count[0] > MAX_NODES:
                raise ValueError("转发节点总数超过 50。")
            image_count[0] += sum(isinstance(item, Image) for item in content)
            if image_count[0] > 20:
                raise ValueError("转发图片总数超过 20。")
            for item in content:
                if isinstance(item, Image) and isinstance(item.file, str) and item.file.startswith("base64://"):
                    image_bytes[0] += (len(item.file) - len("base64://")) * 3 // 4
            if image_bytes[0] > MAX_FORWARD_IMAGE_BYTES:
                raise ValueError("转发图片数据总量超过 200 MiB。")
            if author_name is not None and len(author_name) > 256:
                author_name = author_name[:256]
            return Node(content=content, name=author_name or "", uin=author_id)

        native_nodes = [await build_node(spec, 1) for spec in nodes]
        return await self.event.send(MessageChain([Nodes(native_nodes)]))
