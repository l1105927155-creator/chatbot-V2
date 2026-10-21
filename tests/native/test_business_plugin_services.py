"""Exercise the locked author plugin implementations without external APIs."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ASTRBOT = ROOT / ".runtime/astrbot"
PLUGIN_ROOT = ASTRBOT / "data/plugins"
if not (PLUGIN_ROOT / "astrbot_plugin_bilibili/main.py").is_file():
    pytest.skip("business plugin snapshots are not bootstrapped", allow_module_level=True)
sys.path.insert(0, str(ASTRBOT))
sys.path.insert(0, str(PLUGIN_ROOT))


@pytest.mark.asyncio
async def test_native_services_store_current_umo_and_poll_local_twitter_fixture(tmp_path, monkeypatch):
    from astrbot.api.star import StarTools
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot_plugin_bilibili.core.data_manager import DataManager
    from astrbot_plugin_bilibili.services.subscription_service import (
        SubscriptionService as BiliSubscriptionService,
    )
    from astrbot_plugin_twitter.main import TwitterPlugin
    from astrbot_plugin_twitter.services import PollingService, PollingSettings
    from astrbot_plugin_twitter.services.subscription_service import (
        SubscriptionService as TwitterSubscriptionService,
    )
    from astrbot_plugin_twitter.services.tweet_delivery_service import (
        DeliveryResult,
        DeliveryState,
    )

    # Import the exact author modules that AstrBot discovers. Assert native
    # subscription commands and the model-query FunctionTool are registered.
    import astrbot_plugin_bilibili.main as bili_main
    import astrbot_plugin_twitter.main as twitter_main
    assert hasattr(bili_main.Main, "dynamic_sub")
    assert hasattr(twitter_main.TwitterPlugin, "follow_twitter")
    handlers = list(star_handlers_registry.star_handlers_map.values())
    for handler_name in ("dynamic_sub", "sub_list", "sub_del", "follow_twitter", "unfollow_twitter", "list_follows"):
        assert any(item.handler_name == handler_name for item in handlers)
    assert hasattr(bili_main, "BiliUserDynamicsTool")
    assert "uid" in bili_main.BiliUserDynamicsTool().parameters["properties"]

    monkeypatch.setattr(StarTools, "get_data_dir", staticmethod(lambda plugin_name: str(tmp_path / plugin_name)))
    bili_store = DataManager()

    class BiliFixtureClient:
        async def get_latest_dynamics(self, uid):
            return {"items": []}

    bili_service = BiliSubscriptionService(bili_store, BiliFixtureClient(), lambda _data, _record: [])
    await bili_service.add_or_update("qq:group:current", 12345, [], [], False)
    assert [record.uid for record in bili_store.get_subscriptions_by_user("qq:group:current")] == [12345]
    assert not bili_store.get_subscriptions_by_user("qq:group:other")
    assert await bili_store.remove_subscription("qq:group:current", 12345)
    assert not bili_store.get_subscriptions_by_user("qq:group:current")

    persisted: dict[str, object] = {}

    async def get_kv(key, default):
        return persisted.get(key, default)

    async def put_kv(key, value):
        persisted[key] = value

    class TwitterFixtureAPI:
        is_ready = True
        nitter_url = "fixture"

        async def get_user_info(self, username):
            return {"status": True, "screen_name": "fixture-writer", "bio": "local fixture"}

        async def get_user_newtimeline(self, username):
            return ["100"]

        async def get_user_timeline_items(self, username, since_id):
            assert since_id == "100"
            return [{"tweet_id": "101", "username": username, "created_at": "fixture"}]

        async def get_tweet(self, username, tweet_id):
            return {"status": True, "tweet_id": tweet_id, "username": username, "text": "local poll fixture"}

    api = TwitterFixtureAPI()
    twitter_service = TwitterSubscriptionService(get_kv, put_kv, api, lambda: True)
    await twitter_service.add("qq:group:current", "fixture-writer")
    snapshot = await twitter_service.get_snapshot()
    assert "qq:group:current" in snapshot["fixture-writer"]["subscribers"]
    assert "qq:group:other" not in snapshot["fixture-writer"]["subscribers"]

    class DeliveryFixture:
        collective_enabled = False

        async def push_to_subscribers(self, username, tweet_info):
            assert username == "fixture-writer"
            assert tweet_info["text"] == "local poll fixture"
            return DeliveryResult(DeliveryState.DELIVERED)

    poller = PollingService(
        api,
        twitter_service,
        DeliveryFixture(),
        PollingSettings(True, "fxtwitter", "", (), 3),
    )
    assert await poller.check_user("fixture-writer", snapshot["fixture-writer"])
    updated = await twitter_service.get_snapshot()
    assert updated["fixture-writer"]["since_id"] == "101"
    assert "101" in updated["fixture-writer"]["processed_tweet_ids"]
    assert isinstance(TwitterPlugin, type)
