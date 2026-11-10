import asyncio
import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_bottles.main import BottleCommandFilter, V2Bottles  # noqa: E402
from v2_bottles.service import BottleService  # noqa: E402
from v2_bottles.settings import SettingsStore  # noqa: E402
from v2_bottles.store import BottleStore  # noqa: E402
from v2_bottles.texts import DEFAULT_TEXTS, TextStore  # noqa: E402

SELF_ID = "2869323076"
GROUP_ID = "123"
SENDER_QQ = "10001"
SENDER_NAME = "昵称"


def make_raw(text, *, self_id=SELF_ID, group=GROUP_ID, extra_segments=None):
    segments = [
        {"type": "at", "data": {"qq": self_id}},
        {"type": "text", "data": {"text": " " + text}},
    ]
    if extra_segments:
        segments.extend(extra_segments)
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": self_id,
        "user_id": SENDER_QQ,
        "group_id": group,
        "message": segments,
    }


class FakeEvent:
    def __init__(self, text, raw=None, *, private=False, platform="aiocqhttp",
                 group=GROUP_ID, sender=SENDER_QQ, name=SENDER_NAME,
                 self_id=SELF_ID):
        self._text = text
        self._private = private
        self._platform = platform
        self._group = group
        self._sender = sender
        self._name = name
        self._self_id = self_id
        self.message_obj = SimpleNamespace(raw_message=raw)
        self.extras = {}

    def get_platform_name(self):
        return self._platform

    def is_private_chat(self):
        return self._private

    def get_message_str(self):
        return self._text

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_self_id(self):
        return self._self_id

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def plain_result(self, text):
        return {"kind": "text", "text": text}

    def chain_result(self, chain):
        return {"kind": "chain", "chain": chain}


def ready_plugin(tmp_path, **config):
    values = {"max_bottles": 10, "max_chars": 200, "max_disk_mb": 1024,
              "enabled_groups": [GROUP_ID]}
    values.update(config)
    store = BottleStore(tmp_path / "bottles.sqlite3")
    texts = TextStore(tmp_path / "texts.json")
    settings = SettingsStore(
        tmp_path / "settings.json", {"max_chars": values["max_chars"]}
    )
    service = BottleService(store, values, texts=texts, settings=settings)
    plugin = V2Bottles(SimpleNamespace(), values)
    plugin._store = store
    plugin._texts = texts
    plugin._settings = settings
    plugin._service = service
    plugin._initialized = True
    plugin._closing = False
    return plugin, store, service


async def collect(plugin, event):
    items = []
    async for item in plugin.bottle_command(event):
        items.append(item)
    return items


def test_uninitialized_bottle_command_does_not_access_missing_service():
    async def scenario():
        plugin = V2Bottles(SimpleNamespace(), {"enabled_groups": [GROUP_ID]})
        event = FakeEvent("捡", make_raw("捡"))
        replies = await collect(plugin, event)
        assert replies == [{"kind": "text", "text": DEFAULT_TEXTS["not_ready"]}]
        assert event.get_extra("v2_deterministic_handled") is True

    asyncio.run(scenario())


def test_filter_requires_group_at_bot_and_exact_command_shape():
    command_filter = BottleCommandFilter()
    assert command_filter.filter(FakeEvent("扔 一只猫", make_raw("扔 一只猫")), {}) is True
    assert command_filter.filter(FakeEvent("捡", make_raw("捡")), {}) is True
    assert command_filter.filter(FakeEvent("漂流瓶", make_raw("漂流瓶")), {}) is True

    assert command_filter.filter(
        FakeEvent("扔了一只猫", make_raw("扔了一只猫")), {}
    ) is False
    assert command_filter.filter(FakeEvent("扔", make_raw("扔")), {}) is False
    assert command_filter.filter(
        FakeEvent("扔 一只猫", make_raw("扔 一只猫"), private=True), {}
    ) is False
    assert command_filter.filter(
        FakeEvent("扔 一只猫", make_raw("扔 一只猫"), platform="telegram"), {}
    ) is False
    no_at = {
        "post_type": "message",
        "message_type": "group",
        "self_id": SELF_ID,
        "message": [{"type": "text", "data": {"text": "扔 一只猫"}}],
    }
    assert command_filter.filter(FakeEvent("扔 一只猫", no_at), {}) is False

    media_segments = [{"type": "image", "data": {"file": "x"}}]
    assert command_filter.filter(
        FakeEvent("扔", make_raw("扔", extra_segments=media_segments)), {}
    ) is True
    assert command_filter.filter(
        FakeEvent("偷偷扔", make_raw("偷偷扔", extra_segments=media_segments)), {}
    ) is True
    assert command_filter.filter(
        FakeEvent("喂", make_raw("喂", extra_segments=media_segments)), {}
    ) is False


def test_throw_sets_marker_and_stores_non_anonymous_bottle(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    event = FakeEvent("扔 一只猫", make_raw("扔 一只猫"))
    items = asyncio.run(collect(plugin, event))
    assert event.extras["v2_deterministic_handled"] is True
    assert items[0]["kind"] == "text"
    assert items[0]["text"] == "你的瓶子已经扔向大海啦"
    assert store.count() == 1
    row = store.recent(1)[0]
    assert row["content"] == "一只猫"
    assert row["group_id"] == GROUP_ID
    assert row["sender_qq"] == SENDER_QQ
    assert row["anonymous"] == 0
    store.close()


def test_anonymous_throw_is_hidden_in_pick_reply(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    throw_items = asyncio.run(
        collect(plugin, FakeEvent("偷偷扔 秘密", make_raw("偷偷扔 秘密")))
    )
    assert throw_items[0]["text"] == "你的瓶子已经偷偷扔向大海啦"
    items = asyncio.run(collect(plugin, FakeEvent("捡", make_raw("捡"))))
    assert items[0]["kind"] == "text"
    assert "秘密" in items[0]["text"]
    assert "匿名漂流瓶" in items[0]["text"]
    assert GROUP_ID not in items[0]["text"]
    assert SENDER_QQ not in items[0]["text"]
    assert SENDER_NAME not in items[0]["text"]
    assert store.recent(1)[0]["anonymous"] == 1
    store.close()


def test_pick_is_random_and_does_not_delete(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    asyncio.run(collect(plugin, FakeEvent("扔 一", make_raw("扔 一"))))
    asyncio.run(collect(plugin, FakeEvent("扔 二", make_raw("扔 二"))))
    items = asyncio.run(collect(plugin, FakeEvent("捡", make_raw("捡"))))
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", items[0]["text"].splitlines()[0])
    assert store.count() == 2
    store.close()


def test_disabled_group_is_not_claimed_and_not_stored(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    event = FakeEvent("扔 一只猫", make_raw("扔 一只猫", group="999"), group="999")
    items = asyncio.run(collect(plugin, event))
    assert items == []
    assert event.extras.get("v2_deterministic_handled") is None
    assert store.count() == 0
    store.close()


def test_rules_use_a_merged_forward_with_total_first(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    asyncio.run(collect(plugin, FakeEvent("扔 一只猫", make_raw("扔 一只猫"))))
    event = FakeEvent("漂流瓶", make_raw("漂流瓶"))
    items = asyncio.run(collect(plugin, event))
    assert event.extras["v2_deterministic_handled"] is True
    assert items[0]["kind"] == "chain"
    nodes = items[0]["chain"][0].nodes
    texts = [node.content[0].text for node in nodes]
    assert texts[0].startswith("当前共有 1 个漂流瓶。")
    assert any("必须有空格" in text for text in texts)
    store.close()


def test_media_on_throw_is_rejected_with_requested_reply(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    event = FakeEvent(
        "扔 一只猫",
        make_raw("扔 一只猫", extra_segments=[{"type": "image", "data": {"file": "x"}}]),
    )
    items = asyncio.run(collect(plugin, event))
    assert items[0]["text"] == "暂不支持图片"
    assert event.extras["v2_deterministic_handled"] is True
    assert store.count() == 0
    store.close()


def test_bot_capability_throw_and_bot_source_line(tmp_path):
    plugin, store, service = ready_plugin(tmp_path)
    conversation = SimpleNamespace(key=f"group:{GROUP_ID}", bot_id="bot-123")
    capability = plugin.capability_for(conversation, bot_name="小v")

    result = capability.throw("来自Bot的瓶子", anonymous=False)
    assert result == {
        "ok": True, "anonymous": False, "length": len("来自Bot的瓶子")
    }
    row = store.recent(1)[0]
    assert row["sender_qq"] == "bot-123"
    assert row["sender_name"] == "小v"
    assert row["anonymous"] == 0

    picked = service.pick(bot_id="bot-123")
    assert "（Bot）" in picked
    assert "小v" in picked
    user_view = service.pick(bot_id="other")
    assert "（Bot）" not in user_view
    store.close()


def test_bot_capability_anonymous_uses_anonymous_source(tmp_path):
    plugin, store, service = ready_plugin(tmp_path)
    conversation = SimpleNamespace(key=f"group:{GROUP_ID}", bot_id="bot-123")
    capability = plugin.capability_for(conversation, bot_name="小v")

    result = capability.throw("匿名Bot的瓶子", anonymous=True)
    assert result["ok"] is True
    assert result["anonymous"] is True
    row = store.recent(1)[0]
    assert row["anonymous"] == 1
    picked = service.pick(bot_id="bot-123")
    assert "匿名漂流瓶" in picked
    assert "（Bot）" not in picked
    store.close()


def test_bot_capability_rejects_private_disabled_and_empty(tmp_path):
    plugin, store, service = ready_plugin(tmp_path)

    private = SimpleNamespace(key="private:1", bot_id="bot-123")
    result = plugin.capability_for(private).throw("x")
    assert result["ok"] is False
    assert result["error"] == "not_group"

    disabled = SimpleNamespace(key="group:999", bot_id="bot-123")
    result = plugin.capability_for(disabled).throw("x")
    assert result["ok"] is False
    assert result["error"] == "disabled_group"

    enabled = SimpleNamespace(key=f"group:{GROUP_ID}", bot_id="bot-123")
    result = plugin.capability_for(enabled).throw("")
    assert result["ok"] is False
    assert result["error"] == "empty"
    assert store.count() == 0
    store.close()


def test_media_only_throw_is_rejected(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    media_segments = [{"type": "image", "data": {"file": "x"}}]
    for text in ("扔", "偷偷扔"):
        event = FakeEvent(
            text, make_raw(text, extra_segments=media_segments)
        )
        items = asyncio.run(collect(plugin, event))
        assert items[0]["text"] == "暂不支持图片"
        assert event.extras["v2_deterministic_handled"] is True
    assert store.count() == 0
    store.close()


def test_pick_with_image_still_works(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path)
    asyncio.run(collect(plugin, FakeEvent("扔 一只猫", make_raw("扔 一只猫"))))
    event = FakeEvent(
        "捡",
        make_raw("捡", extra_segments=[{"type": "image", "data": {"file": "x"}}]),
    )
    items = asyncio.run(collect(plugin, event))
    assert "一只猫" in items[0]["text"]
    store.close()


def test_too_long_throw_reports_the_limit(tmp_path):
    plugin, store, _ = ready_plugin(tmp_path, max_chars=5)
    items = asyncio.run(
        collect(plugin, FakeEvent("扔 一二三四五六", make_raw("扔 一二三四五六")))
    )
    assert "超过单条上限 5 字" in items[0]["text"]
    assert store.count() == 0
    store.close()


def test_dashboard_api_authorizes_owner_and_returns_readonly_data(tmp_path):
    from starlette.requests import Request

    from astrbot.api.web import PluginRequest, bind_request_context

    class Context:
        def get_config(self):
            return {"dashboard": {"username": "owner@example.test"}}

    plugin, store, _ = ready_plugin(tmp_path)
    plugin.context = Context()
    asyncio.run(collect(plugin, FakeEvent("扔 一只猫", make_raw("扔 一只猫"))))

    scope = {
        "type": "http", "method": "POST", "path": "/v2_bottles/api",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("localhost", 80), "client": ("127.0.0.1", 1),
    }
    web = importlib.import_module("astrbot.api.web")
    original_json = web.request.json

    async def call(payload, username="owner@example.test"):
        web.request.json = lambda: asyncio.sleep(0, result=payload)
        with bind_request_context(PluginRequest(Request(scope), username=username)):
            return await plugin.api()

    async def scenario():
        try:
            denied = await call({"action": "status"}, username="api_key:owner@example.test")
            assert denied.status_code == 403
            status = await call({"action": "status"})
            assert status["status"] == "ok"
            assert status["data"]["count"] == 1
            listing = await call({"action": "list", "qq": SENDER_QQ})
            assert listing["status"] == "ok"
            assert listing["data"]["bottles"][0]["sender_qq"] == SENDER_QQ
            bottle_id = listing["data"]["bottles"][0]["id"]

            deleted = await call({"action": "delete", "ids": [bottle_id]})
            assert deleted["status"] == "ok"
            assert deleted["data"]["deleted"] == 1
            status_after = await call({"action": "status"})
            assert status_after["data"]["count"] == 0

            empty = await call({"action": "delete", "ids": []})
            assert empty.status_code == 400
            invalid = await call({"action": "delete", "ids": ["bad"]})
            assert invalid.status_code == 400
            unknown = await call({"action": "admin.exec"})
            assert unknown.status_code == 400
        finally:
            web.request.json = original_json

    asyncio.run(scenario())
    store.close()


def test_texts_api_saves_and_applies_immediately(tmp_path):
    from starlette.requests import Request

    from astrbot.api.web import PluginRequest, bind_request_context

    class Context:
        def get_config(self):
            return {"dashboard": {"username": "owner@example.test"}}

    plugin, store, service = ready_plugin(tmp_path)
    plugin.context = Context()

    scope = {
        "type": "http", "method": "POST", "path": "/v2_bottles/api",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("localhost", 80), "client": ("127.0.0.1", 1),
    }
    web = importlib.import_module("astrbot.api.web")
    original_json = web.request.json

    async def call(payload, username="owner@example.test"):
        web.request.json = lambda: asyncio.sleep(0, result=payload)
        with bind_request_context(PluginRequest(Request(scope), username=username)):
            return await plugin.api()

    async def scenario():
        try:
            listing = await call({"action": "texts"})
            assert listing["status"] == "ok"
            assert "empty_pool" in listing["data"]["texts"]
            assert listing["data"]["fields"]

            saved = await call({
                "action": "texts.save",
                "texts": {
                    "empty_pool": "新空池提示",
                    "throw_success": "新投放提示",
                },
            })
            assert saved["status"] == "ok"
            assert saved["message"] == "提示文本已保存，立即生效。"
            assert service.text("empty_pool") == "新空池提示"

            items = await collect(plugin, FakeEvent("捡", make_raw("捡")))
            assert items[0]["text"] == "新空池提示"
            items = await collect(
                plugin, FakeEvent("扔 一只猫", make_raw("扔 一只猫"))
            )
            assert items[0]["text"] == "新投放提示"

            denied = await call(
                {"action": "texts.save", "texts": {"empty_pool": "x"}},
                username="api_key:owner@example.test",
            )
            assert denied.status_code == 403
        finally:
            web.request.json = original_json

    asyncio.run(scenario())
    store.close()


def test_settings_api_saves_and_applies_immediately(tmp_path):
    from starlette.requests import Request

    from astrbot.api.web import PluginRequest, bind_request_context

    class Context:
        def get_config(self):
            return {"dashboard": {"username": "owner@example.test"}}

    plugin, store, service = ready_plugin(tmp_path)
    plugin.context = Context()

    scope = {
        "type": "http", "method": "POST", "path": "/v2_bottles/api",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("localhost", 80), "client": ("127.0.0.1", 1),
    }
    web = importlib.import_module("astrbot.api.web")
    original_json = web.request.json

    async def call(payload, username="owner@example.test"):
        web.request.json = lambda: asyncio.sleep(0, result=payload)
        with bind_request_context(PluginRequest(Request(scope), username=username)):
            return await plugin.api()

    async def scenario():
        try:
            listing = await call({"action": "settings"})
            assert listing["status"] == "ok"
            assert listing["data"]["settings"]["max_chars"] == 200
            assert listing["data"]["defaults"]["max_chars"] == 200
            assert listing["data"]["fields"]

            saved = await call({
                "action": "settings.save",
                "settings": {"max_chars": 5},
            })
            assert saved["status"] == "ok"
            assert saved["data"]["settings"]["max_chars"] == 5
            assert service.max_chars == 5

            items = await collect(
                plugin, FakeEvent("扔 123456", make_raw("扔 123456"))
            )
            assert "超过单条上限 5 字" in items[0]["text"]
            assert store.count() == 0

            invalid = await call({
                "action": "settings.save",
                "settings": {"max_chars": 0},
            })
            assert invalid.status_code == 400
            denied = await call(
                {"action": "settings.save", "settings": {"max_chars": 10}},
                username="api_key:owner@example.test",
            )
            assert denied.status_code == 403
        finally:
            web.request.json = original_json

    asyncio.run(scenario())
    store.close()


def test_page_uses_deferred_module_script_for_the_bridge():
    page = (
        ROOT / "astrbot-plugins/v2_bottles/pages/bottles/index.html"
    ).read_text(encoding="utf-8")
    assert '<script type="module" src="./app.js"></script>' in page
    assert "<script>" not in page
    assert 'id="texts"' in page
    assert 'id="save-texts"' in page
    assert 'id="select-all"' in page
    assert 'id="delete-selected"' in page
    assert 'id="settings"' in page
    assert 'id="save-settings"' in page
    script = (
        ROOT / "astrbot-plugins/v2_bottles/pages/bottles/app.js"
    ).read_text(encoding="utf-8")
    assert "window.AstrBotPluginPage" in script
    assert 'texts.save' in script
    assert 'settings.save' in script
    assert "deleteSelected" in script
    assert "row-select" in script


def test_plugin_initialize_registers_api_and_terminate_closes_store(tmp_path, monkeypatch):
    from v2_bottles import main as bottle_main

    class Context:
        def __init__(self):
            self.apis = []

        def register_web_api(self, route, handler, methods, desc):
            self.apis.append((route, methods))

    monkeypatch.setattr(
        bottle_main.star.StarTools,
        "get_data_dir",
        staticmethod(lambda name: str(tmp_path / name)),
    )
    context = Context()
    plugin = V2Bottles(context, {"max_bottles": 5, "max_chars": 20, "max_disk_mb": 1})

    async def exercise():
        await plugin.initialize()
        assert plugin.ready()
        assert context.apis == [("/v2_bottles/api", ["POST"])]
        await plugin.terminate()
        assert not plugin.ready()
        assert plugin._store is None

    asyncio.run(exercise())
