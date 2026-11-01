import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_games import engine2048  # noqa: E402
from v2_games.main import V2Games  # noqa: E402
from v2_games.service import GameService  # noqa: E402
from v2_games.store import GameStore  # noqa: E402


class FakeEvent:
    def __init__(self):
        self.extras = {}

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "10001"

    def plain_result(self, text):
        return {"text": text}


def plugin_for(tmp_path, public_base_url="https://example.test:8443"):
    store = GameStore(tmp_path / "games.sqlite3")
    service = GameService(store, {"public_base_url": public_base_url})
    plugin = V2Games(SimpleNamespace(), {"public_base_url": public_base_url})
    plugin._store = store
    plugin._service = service
    plugin._initialized = True
    plugin._closing = False
    return plugin, store, service


async def collect(plugin, args=""):
    event = FakeEvent()
    items = []
    async for item in plugin.games_command(event, args=args):
        items.append(item)
    assert event.extras.get("v2_deterministic_handled") is True
    return "\n".join(item["text"] for item in items)


def test_games_command_entry_rank_and_me(tmp_path):
    import asyncio
    plugin, store, service = plugin_for(tmp_path)
    entry = asyncio.run(collect(plugin, ""))
    assert "本群入口：" in entry
    assert "/g/" in entry
    assert "本群前三" not in entry

    run = service.create_web_run(group_id="123", qq="10001")
    seed = store.get_run(run["run_id"])["seed"]
    state = engine2048.new_game(seed)
    moves = []
    for _ in range(6):
        for direction in range(4):
            if engine2048.can_move(state, direction):
                moves.append(direction)
                engine2048.move(state, direction)
                break
    service.submit_web_run(
        run_id=run["run_id"], group_id="123", qq="10001", moves=moves,
        client_score=state.score, max_tile=engine2048.max_tile(state),
        duration_ms=1000,
    )
    rank = asyncio.run(collect(plugin, "排行"))
    assert "本群 2048 总榜" in rank
    assert "1000****" in rank
    me = asyncio.run(collect(plugin, "我"))
    assert "最高分" in me
    assert "本群排名：1" in me
    store.close()


def test_games_command_without_public_url_explains_configuration(tmp_path):
    import asyncio
    plugin, store, _ = plugin_for(tmp_path, public_base_url="")
    text = asyncio.run(collect(plugin, ""))
    assert "public_base_url" in text
    store.close()


def test_plugin_initialize_starts_and_stops_local_web(tmp_path, monkeypatch):
    import asyncio

    import httpx

    from v2_games import main as games_main

    class Context:
        def __init__(self):
            self.apis = []

        def register_web_api(self, route, handler, methods, desc):
            self.apis.append((route, methods))

        def get_config(self):
            return {"dashboard": {"username": "owner"}}

    monkeypatch.setattr(games_main.star.StarTools, "get_data_dir",
                        staticmethod(lambda name: str(tmp_path / name)))
    plugin = V2Games(Context(), {
        "web_enabled": True,
        "listen_port": 6291,
        "public_base_url": "https://example.test:8443",
        "leaderboard_limit": 20,
    })

    async def exercise():
        await plugin.initialize()
        try:
            assert plugin.ready()
            assert plugin._web is not None and plugin._web.port == 6291
            async with httpx.AsyncClient(base_url="http://127.0.0.1:6291") as client:
                assert (await client.get("/healthz")).status_code == 200
        finally:
            await plugin.terminate()

    asyncio.run(exercise())
    assert not plugin.ready()
    assert plugin._service is None


def test_dashboard_api_authorizes_owner_and_rejects_pseudouser(tmp_path):
    import asyncio
    import importlib

    from starlette.requests import Request

    from astrbot.api.web import PluginRequest, bind_request_context

    class Context:
        def get_config(self):
            return {"dashboard": {"username": "owner@example.test"}}

    store = GameStore(tmp_path / "games.sqlite3")
    service = GameService(store, {"public_base_url": "https://example.test:8443"})
    plugin = V2Games(Context(), {"public_base_url": "https://example.test:8443"})
    plugin._store = store
    plugin._service = service
    plugin._initialized = True
    plugin._closing = False

    scope = {"type": "http", "method": "POST", "path": "/v2_games/api",
             "headers": [], "query_string": b"", "scheme": "http",
             "server": ("localhost", 80), "client": ("127.0.0.1", 1)}
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
            assert status["data"]["web"]["running"] is False
            groups = await call({"action": "groups"})
            assert groups["status"] == "ok"
            unknown = await call({"action": "admin.exec"})
            assert unknown.status_code == 400
        finally:
            web.request.json = original_json

    asyncio.run(scenario())
    store.close()
