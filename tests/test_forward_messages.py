"""Forward messages stay opt-in, bounded, and bound to current Journal scope."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/astrbot"))
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Node, Nodes, Plain
from v2_dsh_router.forward_messages import QQForwards


class Journal:
    def __init__(self, rows):
        self.rows = {row.journal_id: row for row in rows}

    def by_journal_id(self, conversation, journal_id):
        row = self.rows.get(journal_id)
        return row if row and row.message.conversation_key == conversation.key else None


def make_row(journal_id=10, forward=None, *, conversation_key="group:500"):
    message = NS(
        platform_id="qq", bot_id="9000", conversation_key=conversation_key,
        message_id="platform-10", actor_id="42", actor_name="Journal author", direction="inbound",
        content={"segments": [forward or {"type": "forward", "data": {"id": "fwd-root"}}]},
    )
    return NS(journal_id=journal_id, message=message)


def harness(rows=None, *, after=0, through=100, response=None, failure=None):
    conversation = NS(platform_id="qq", bot_id="9000", key="group:500")
    calls = []

    async def call_action(action, **kwargs):
        calls.append((action, kwargs))
        if failure:
            raise failure
        if action == "get_msg":
            return {"data": {"message_id": "platform-10", "group_id": 500,
                             "message": [{"type": "text", "data": {"text": "quoted"}}]}}
        return response

    event = NS(get_platform_name=lambda: "aiocqhttp", bot=NS(call_action=call_action))
    messaging = NS(event=event, journal=Journal(rows or [make_row()]), conversation=conversation,
                   history_after=after, history_through=through)
    return QQForwards(messaging), calls, event


def node(*segments, sender="sender", timestamp=123):
    return {"type": "node", "data": {"nickname": sender, "time": timestamp,
                                       "message": list(segments)}}


def text(value):
    return {"type": "text", "data": {"text": value}}


def test_forward_cards_keep_visible_fields_and_share_page_text_budget():
    fwd, _, _ = harness()
    card = {'type':'json','data':{'data':json.dumps({
        'app':'com.tencent.miniapp_01','prompt':'分享视频','config':{'token':'PRIVATE'},
        'meta':{'detail_1':{'title':'视频标题','desc':'说明'*1000}}
    })}}
    budget = [500]
    content = fwd._normalize_content([card]*20, [], 0, budget)
    assert len(content) == 20 and all(item['type']=='json' for item in content)
    assert budget[0]>=0 and any(item.get('truncated') for item in content)
    assert 'PRIVATE' not in json.dumps(content) and '视频' in json.dumps(content,ensure_ascii=False)
    assert len(json.dumps(content,ensure_ascii=False).encode()) < 5000


def test_read_pages_one_layer_and_does_not_fetch_nested_forward_automatically():
    async def run():
        nested = {"type": "forward", "data": {"id": "nested-id"}}
        fwd, calls, _ = harness(response={"data": {"messages": [
            node(text("first")), node(text("second"), nested), node(text("third")),
        ]}})
        result = await fwd.read(10, 0, offset=1, limit=1)
        assert result["nodes"] == [{"sender": "sender", "user_id": None, "time": 123,
                                    "node_index": 1,
                                    "content_truncated": False,
                                    "content": [{"type": "text", "segment_index": 0, "text": "second"},
                                                {"type": "forward", "segment_index": 1,
                                                 "node_path": [1, 1], "available": True}]}]
        assert result["next_offset"] == 2 and result["total"] == 3
        assert calls == [("get_forward_msg", {"message_id": "fwd-root"})]

    asyncio.run(run())


def test_page_totals_allow_more_than_fifty_source_nodes_and_report_text_truncation():
    async def run():
        raw_nodes = [node(text("x" * 5001)) for _ in range(55)]
        fwd, _, _ = harness(response={"messages": raw_nodes})
        page = await fwd.read(10, 0, offset=40, limit=20)
        assert page["total"] == 55 and page["truncated"] is False
        assert len(page["nodes"]) == 15 and page["next_offset"] is None
        first_text = page["nodes"][0]["content"][0]
        assert len(first_text["text"]) == 4000 and first_text["truncated"] is True
        assert page["text_truncated"] is True

    asyncio.run(run())


def test_two_level_inline_path_selects_only_requested_nested_tree():
    async def run():
        deepest = [node(text("deep detail"))]
        nested_forward = {"type": "forward", "data": {"id": "nested", "res_id": "resource",
                                                          "content": deepest}}
        root_inline = [node(text("outer"), nested_forward)]
        fwd, calls, _ = harness(response=None, rows=[make_row(forward={"type": "forward", "data": {
            "id": "root", "content": root_inline}})])
        result = await fwd.read(10, 0, node_path=[0, 1])
        assert result["nodes"][0]["content"][0] == {"type": "text", "segment_index": 0, "text": "deep detail"}
        assert calls == []  # parseMultMsg content already carries the selected child.

    asyncio.run(run())


def test_redacted_napcat_parse_mult_msg_fixture_two_level_shape():
    async def run():
        fixture = json.loads((ROOT / "tests/fixtures/qq-forward-napcat.json").read_text())
        fwd, calls, _ = harness(rows=[make_row(forward=fixture["message"])])
        result = await fwd.read(10, 0, node_path=[0, 0])
        assert result["total"] == 1
        assert result["nodes"][0]["content"][0]["type"] == "image"
        assert "url" not in result["nodes"][0]["content"][0].get("metadata", {})
        assert calls == []

    asyncio.run(run())


@pytest.mark.parametrize("journal_id", [9, 101])
def test_reset_floor_and_history_through_reject_source_before_platform_call(journal_id):
    async def run():
        fwd, calls, _ = harness(after=9, through=100)
        with pytest.raises(ValueError, match="历史|范围"):
            await fwd.read(journal_id, 0)
        assert calls == []

    asyncio.run(run())


def test_history_bounds_are_read_dynamically_after_forwards_is_constructed():
    async def run():
        fwd, calls, _ = harness(after=0, through=100, response={"messages": [node(text("ok"))]})
        fwd.messaging.history_after = 10
        with pytest.raises(ValueError, match="重置"):
            await fwd.read(10, 0)
        fwd.messaging.history_after = 9
        fwd.messaging.history_through = 9
        with pytest.raises(ValueError, match="本轮历史范围"):
            await fwd.read(10, 0)
        assert calls == []

    asyncio.run(run())


def test_wrong_conversation_and_bad_paths_are_rejected():
    async def run():
        other = make_row(conversation_key="group:999")
        fwd, calls, _ = harness(rows=[other], response={"messages": []})
        with pytest.raises(ValueError, match="核实|属于"):
            await fwd.read(10, 0)
        with pytest.raises(ValueError, match="node_path"):
            await fwd.read(10, 0, node_path=[0, True])
        with pytest.raises(ValueError, match="node_path"):
            await fwd.read(10, 0, node_path=[0])
        assert calls == []

    asyncio.run(run())


def test_invalid_forward_position_and_napcat_failure_are_explicit():
    async def run():
        fwd, calls, _ = harness(response=None)
        with pytest.raises(ValueError, match="NapCat 返回"):
            await fwd.read(10, 0)
        assert calls == [("get_forward_msg", {"message_id": "fwd-root"})]
        bad, bad_calls, _ = harness(rows=[make_row(forward={"type": "image", "data": {"file": "x"}})])
        with pytest.raises(ValueError, match="不是转发"):
            await bad.read(10, 0)
        assert bad_calls == []

    asyncio.run(run())


def test_missing_forward_receipt_reference_is_recovered_once_by_scoped_get_msg():
    async def run():
        source = make_row(forward={"type": "forward", "data": {}})
        source.message.content["forward_reference_missing"] = True
        fwd, calls, event = harness(rows=[source], response={"messages": [node(text("details"))]})

        async def call_action(action, **kwargs):
            calls.append((action, kwargs))
            if action == "get_msg":
                return {"data": {"message_id": "platform-10", "group_id": 500,
                                 "message": [{"type": "forward", "data": {"res_id": "recovered", "content": [node(text("details"))]}}]}}
            return {"messages": [node(text("details"))]}

        event.bot.call_action = call_action
        result = await fwd.read(10, 0)
        assert result["nodes"][0]["content"] == [{"type": "text", "segment_index": 0, "text": "details"}]
        assert calls == [("get_msg", {"message_id": "platform-10"})]

    asyncio.run(run())


def test_bounds_reject_bad_page_and_node_path_depth():
    async def run():
        fwd, calls, _ = harness(response={"messages": []})
        with pytest.raises(ValueError, match="limit"):
            await fwd.read(10, 0, limit=21)
        with pytest.raises(ValueError, match="六层"):
            await fwd.read(10, 0, node_path=[0] * 14)
        assert calls == []

    asyncio.run(run())


def test_send_serializes_native_nested_node_and_uses_bot_identity():
    async def run():
        fwd, _, event = harness(response={"messages": []})
        sent = []

        async def send(chain):
            sent.append(chain)
            return chain

        event.send = send
        await fwd.send([{"text": "outer"}, {"nodes": [{"text": "inner"}]}])
        chain = sent[0]
        assert isinstance(chain, MessageChain)
        assert len(chain.chain) == 1 and isinstance(chain.chain[0], Nodes)
        roots = chain.chain[0].nodes
        assert all(isinstance(item, Node) and item.uin == "9000" for item in roots)
        assert isinstance(roots[0].content[0], Plain) and roots[0].content[0].text == "outer"
        assert isinstance(roots[1].content[0], Node)
        assert roots[1].content[0].content[0].text == "inner"
        assert roots[0].uin == roots[1].uin == "9000"
        serialized = await chain.chain[0].to_dict()
        assert serialized["messages"][1]["data"]["content"][0]["type"] == "node"

    asyncio.run(run())


def test_send_allows_fifty_total_nodes_and_rejects_fifty_one_before_send():
    async def run():
        fwd, _, event = harness(response={"messages": []})
        sent = []

        async def send(chain):
            sent.append(chain)

        event.send = send
        await fwd.send([{"text": str(i)} for i in range(50)])
        assert len(sent) == 1 and len(sent[0].chain[0].nodes) == 50
        with pytest.raises(ValueError, match="50"):
            await fwd.send([{"text": str(i)} for i in range(51)])
        assert len(sent) == 1

    asyncio.run(run())


def test_reference_fetches_real_message_and_rejects_unsupported_component():
    async def run():
        fwd, calls, event = harness(response={"messages": []})
        sent = []

        async def send(chain):
            sent.append(chain)
            return chain

        event.send = send
        await fwd.send([{"journal_id": 10}])
        assert calls == [("get_msg", {"message_id": "platform-10"})]
        root = sent[0].chain[0].nodes[0]
        assert root.uin == "42" and root.name == "Journal author"
        bad, _, bad_event = harness(response={"messages": []})

        async def unsupported(_action, **_kwargs):
            return {"data": {"message_id": "platform-10", "group_id": 500,
                             "message": [{"type": "file", "data": {"file": "opaque"}}]}}

        bad_event.bot.call_action = unsupported
        with pytest.raises(ValueError, match="不支持"):
            await bad.send([{"journal_id": 10}])

    asyncio.run(run())


@pytest.mark.parametrize(("direction", "actor_id", "response"), [
    ("outbound", "9000", {"user_id": 9000, "target_id": 42}),
    ("inbound", "42", {"user_id": 42}),
])
def test_private_reference_validation_handles_inbound_and_bot_outbound(direction, actor_id, response):
    async def run():
        conversation = NS(platform_id="qq", bot_id="9000", key="private:42")
        message = NS(platform_id="qq", bot_id="9000", conversation_key="private:42",
                     message_id="private-10", actor_id=actor_id, actor_name="author", direction=direction,
                     content={"segments": [{"type": "forward", "data": {"id": "root"}}]})
        row = NS(journal_id=10, message=message)

        async def call_action(action, **_kwargs):
            return {"data": {"message_id": "private-10", **response,
                             "message": [{"type": "text", "data": {"text": "message"}}]}}

        event = NS(get_platform_name=lambda: "aiocqhttp", bot=NS(call_action=call_action))
        messaging = NS(event=event, journal=Journal([row]), conversation=conversation,
                       history_after=0, history_through=20)
        fwd = QQForwards(messaging)
        components, source_id, source_name = await fwd._referenced_components(10)
        assert components[0].text == "message"
        assert (source_id, source_name) == (actor_id, "author")

    asyncio.run(run())


def test_referenced_image_is_resolved_and_real_node_to_dict_serializes_validated_bytes(monkeypatch):
    async def run():
        fwd, _, event = harness(response={"messages": []})
        sent = []

        async def send(chain):
            sent.append(chain)
            return chain

        async def get_msg(_action, **_kwargs):
            return {"data": {"message_id": "platform-10", "group_id": 500,
                             "message": [{"type": "image", "data": {"file": "opaque-image-id"}}]}}

        event.send = send
        event.bot.call_action = get_msg
        png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/SAAAAABJRU5ErkJggg=="
        resolve_calls = []

        async def resolve_onebot_file(_bot, _conversation, segment):
            resolve_calls.append(segment)
            return {"url": "safe-resolved-resource"}

        class FakeVision:
            def __init__(self, resolve):
                self.resolve = resolve

            async def prepare(self, rows, visible_ids, *, selected_segments):
                assert visible_ids == {rows[0].journal_id} and selected_segments == {(rows[0].journal_id, 0)}
                await self.resolve(rows[0].message.content["segments"][0])
                return {"images": [{"type": "image", "data": png_b64, "mimeType": "image/png"}], "errors": []}

        import v2_dsh_router.file_transfer as file_transfer
        import v2_dsh_router.vision as vision
        monkeypatch.setattr(file_transfer, "resolve_onebot_file", resolve_onebot_file)
        monkeypatch.setattr(vision, "QQVision", FakeVision)
        await fwd.send([{"journal_id": 10}])
        assert resolve_calls == [{"type": "image", "file": "opaque-image-id"}]
        serialized = await sent[0].chain[0].to_dict()
        assert serialized["messages"][0]["data"]["content"][0]["type"] == "image"
        assert serialized["messages"][0]["data"]["content"][0]["data"]["file"].startswith("base64://")

    asyncio.run(run())


def test_read_image_uses_vision_and_exact_selected_segment(monkeypatch):
    async def run():
        image = {"type": "image", "data": {"file": "napcat-resource"}}
        row = make_row(forward={"type": "forward", "data": {"content": [node(image)]}})
        fwd, _, _ = harness(rows=[row])
        calls = []

        async def resolve_onebot_file(_bot, _conversation, segment):
            calls.append(segment)
            return {"url": "resource"}

        class FakeVision:
            def __init__(self, resolve):
                self.resolve = resolve

            async def prepare(self, rows, visible_ids, *, selected_segments):
                assert visible_ids == {rows[0].journal_id}
                assert selected_segments == {(rows[0].journal_id, 0)}
                await self.resolve(image)
                return {"images": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}],
                        "errors": []}

        import v2_dsh_router.file_transfer as file_transfer
        import v2_dsh_router.vision as vision
        monkeypatch.setattr(file_transfer, "resolve_onebot_file", resolve_onebot_file)
        monkeypatch.setattr(vision, "QQVision", FakeVision)
        result = await fwd.read_image(10, 0, [], 0, 0)
        assert result["mimeType"] == "image/png" and calls == [image]

    asyncio.run(run())


def test_read_image_uses_real_vision_validation_and_returns_decodable_image(tmp_path, monkeypatch):
    async def run():
        import base64
        import io
        from PIL import Image as PILImage

        png_buffer = io.BytesIO()
        PILImage.new("RGBA", (1, 1), (24, 128, 200, 255)).save(png_buffer, format="PNG")
        image_path = tmp_path / "one.png"
        image_path.write_bytes(png_buffer.getvalue())
        image = {"type": "image", "data": {"file": "napcat-image-id"}}
        row = make_row(forward={"type": "forward", "data": {"content": [node(image)]}})
        fwd, _, _ = harness(rows=[row])

        async def resolve_onebot_file(_bot, _conversation, segment):
            assert segment == {"type": "image", "file": "napcat-image-id"}
            return {"file": str(image_path)}

        import v2_dsh_router.file_transfer as file_transfer
        monkeypatch.setattr(file_transfer, "resolve_onebot_file", resolve_onebot_file)
        result = await fwd.read_image(10, 0, [], 0, 0)
        decoded = base64.b64decode(result["data"])
        with PILImage.open(io.BytesIO(decoded)) as opened:
            opened.verify()
        assert result["mimeType"] == "image/png"

    asyncio.run(run())


def test_read_image_prefers_bounded_napcat_response_url_without_using_model_url(monkeypatch):
    async def run():
        image = {"type": "image", "data": {"file": "napcat-image-id",
                                               "url": "https://images.example.invalid/?sig=" + "s" * 600}}
        row = make_row(forward={"type": "forward", "data": {"content": [node(image)]}})
        fwd, _, _ = harness(rows=[row])

        async def resolve_onebot_file(_bot, _conversation, _segment):
            raise AssertionError("response URL should be tried before the action lookup")

        class FakeVision:
            def __init__(self, resolve):
                self.resolve = resolve

            async def prepare(self, rows, visible_ids, *, selected_segments):
                resolved = await self.resolve(rows[0].message.content["segments"][0])
                assert resolved["url"].startswith("https://images.example.invalid/")
                assert len(resolved["url"]) > 256
                return {"images": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}], "errors": []}

        import v2_dsh_router.file_transfer as file_transfer
        import v2_dsh_router.vision as vision
        monkeypatch.setattr(file_transfer, "resolve_onebot_file", resolve_onebot_file)
        monkeypatch.setattr(vision, "QQVision", FakeVision)
        result = await fwd.read_image(10, 0, [], 0, 0)
        assert result["mimeType"] == "image/png"

    asyncio.run(run())
