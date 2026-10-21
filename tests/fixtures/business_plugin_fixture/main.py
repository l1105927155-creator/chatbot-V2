"""Sandbox-only deterministic data source for the installed author plugins.

This fixture keeps AstrBot's native Twitter/Bilibili services, persistence,
message construction, dispatcher, and Context.send_message path. Only data
fetches and polling schedules are replaced with local examples.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from typing import Any

from aiohttp import web
from astrbot.api import logger
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


TWITTER_FIXTURE_USERNAME = "v2_fixture"
BILIBILI_FIXTURE_UID = 20261019
_BILI_DYNAMIC_ENABLED = False
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "l1sAAAAASUVORK5CYII="
)


async def _twitter_ready_fxtwitter(self, *args, **kwargs):
    self.provider_ready = True
    return True


async def _twitter_ready_nitter(self, website_list):
    self.nitter_url = "http://127.0.0.1/business-fixture"
    self.provider_ready = True
    return self.nitter_url


async def _twitter_default_user_info(self, username):
    username = str(username or "").strip().lstrip("@")
    if username.casefold() != TWITTER_FIXTURE_USERNAME:
        return {"status": False, "screen_name": "", "bio": "", "user_name": username}
    return {"status": True, "screen_name": "V2 本地夹具", "bio": "Sandbox fixture", "user_name": username}


async def _twitter_default_newtimeline(self, username, since_id=""):
    return ["1000"] if not since_id else []


async def _twitter_default_timeline(self, username, since_id="", limit=0):
    return []


async def _twitter_default_tweet(self, username, tweet_id):
    return {"status": False, "username": username, "tweet_id": str(tweet_id)}


async def _bili_user_info(self, uid: int):
    if int(uid) != BILIBILI_FIXTURE_UID:
        return None, "fixture only supports its local UID"
    return {"mid": BILIBILI_FIXTURE_UID, "name": "V2 本地夹具", "face": ""}, ""


async def _bili_latest_dynamics(self, uid: int):
    if int(uid) != BILIBILI_FIXTURE_UID:
        return None
    latest = "3001" if _BILI_DYNAMIC_ENABLED else "3000"
    items = []
    for dyn_id in ([latest, "3000"] if latest != "3000" else ["3000"]):
        body = "本地夹具：Bilibili 图文动态已更新" if dyn_id == "3001" else "本地夹具订阅起点"
        items.append({
            "id_str": dyn_id,
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"name": "V2 本地夹具", "face": "", "pendant": {}},
                "module_dynamic": {
                    "desc": {"text": body},
                    "topic": None,
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {
                            "summary": {"text": body, "rich_text_nodes": []},
                            "jump_url": f"//t.bilibili.com/{dyn_id}",
                            "title": "隔离沙箱动态夹具",
                            "pics": [{"url": "http://127.0.0.1/business-fixture/image.png"}],
                        },
                    },
                },
            },
        })
    return {"items": items}


async def _bili_live_info(self, uids):
    return None


# Patch the pinned third-party API client classes before their plugin instances
# initialize. This prevents their startup probes or poll tasks from touching
# the public Internet even if the fixture's on_astrbot_loaded hook runs later.
try:
    from data.plugins.astrbot_plugin_twitter.twitter_api import TwitterAPI

    TwitterAPI.check_fxtwitter_available = _twitter_ready_fxtwitter
    TwitterAPI.check_website_available = _twitter_ready_nitter
    TwitterAPI.get_user_info = _twitter_default_user_info
    TwitterAPI.get_user_newtimeline = _twitter_default_newtimeline
    TwitterAPI.get_user_timeline_items = _twitter_default_timeline
    TwitterAPI.get_tweet = _twitter_default_tweet
except (ImportError, ModuleNotFoundError) as exc:
    logger.warning("Sandbox business fixture could not preload TwitterAPI: %s", exc)

try:
    from data.plugins.astrbot_plugin_bilibili.bili_client import BiliClient
    from data.plugins.astrbot_plugin_bilibili.main import Main as BilibiliMain

    BiliClient.get_user_info = _bili_user_info
    BiliClient.get_latest_dynamics = _bili_latest_dynamics
    BiliClient.get_live_info_by_uids = _bili_live_info
    # Main.__init__ calls this while constructing the real author plugin.
    # No background polling is needed in a deterministic sandbox check.
    BilibiliMain._start_tasks = lambda self: None
except (ImportError, ModuleNotFoundError) as exc:
    logger.warning("Sandbox business fixture could not preload BiliClient: %s", exc)


@star.register("business_plugin_fixture", "chatbot-V2", "Isolated author-service fixtures", "0.1.0")
class BusinessPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self._twitter = None
        self._bilibili = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._image_url = ""
        self._loaded = False

    @filter.on_astrbot_loaded()
    async def prepare_local_services(self):
        self._runner = web.AppRunner(web.Application())
        self._runner.app.router.add_get("/image.png", self._serve_image)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        sockets = getattr(self._site, "_server", None).sockets
        port = sockets[0].getsockname()[1]
        self._image_url = f"http://127.0.0.1:{port}/image.png"

        for metadata in self.context.get_all_stars():
            instance = getattr(metadata, "star_cls", None)
            if instance is None:
                continue
            if all(hasattr(instance, name) for name in ("twitter_api", "polling_service", "subscription_service")):
                self._twitter = instance
            if all(hasattr(instance, name) for name in ("bili_client", "dynamic_listener", "subscription_service", "data_manager")):
                self._bilibili = instance

        if self._twitter is not None:
            await self._stop_task(self._twitter, "_poll_task", "_running")
            self._twitter.twitter_api.check_website_available = _twitter_ready_nitter.__get__(self._twitter.twitter_api)
            self._twitter.twitter_api.check_fxtwitter_available = _twitter_ready_fxtwitter.__get__(self._twitter.twitter_api)
            self._twitter.twitter_api.get_user_info = self._twitter_user_info
            self._twitter.twitter_api.get_user_newtimeline = self._twitter_newtimeline
            self._twitter.twitter_api.get_user_timeline_items = self._twitter_timeline
            self._twitter.twitter_api.get_tweet = self._twitter_tweet
            self._twitter._provider_ready = True
            self._twitter.message_service.settings = replace(
                self._twitter.message_service.settings,
                text_render_mode="text",
                translate_enabled=False,
                pre_download_media=False,
            )
            self._twitter.delivery_service.settings = replace(
                self._twitter.delivery_service.settings,
                use_node=False,
                collective_forward=False,
            )

        if self._bilibili is not None:
            self._bilibili.dynamic_listener.rai = False
            self._bilibili.dynamic_listener.node = False
            await self._stop_task(self._bilibili, "dynamic_listener_task")
            self._bilibili.bili_client.get_user_info = self._bili_user_info
            self._bilibili.bili_client.get_latest_dynamics = self._bili_dynamics
            self._bilibili.bili_client.get_live_info_by_uids = _bili_live_info.__get__(self._bilibili.bili_client)

        self._loaded = True
        logger.info("Sandbox business plugin fixture ready; public network fetches disabled")

    @staticmethod
    async def _stop_task(plugin: Any, task_name: str, running_name: str | None = None):
        task = getattr(plugin, task_name, None)
        if running_name:
            setattr(plugin, running_name, False)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hasattr(plugin, task_name):
            setattr(plugin, task_name, None)

    async def _serve_image(self, request):
        return web.Response(body=_PIXEL_PNG, content_type="image/png", headers={"Cache-Control": "no-store"})

    async def _twitter_user_info(self, username):
        return await _twitter_default_user_info(self._twitter.twitter_api, username)

    async def _twitter_newtimeline(self, username, since_id=""):
        if str(username).casefold() != TWITTER_FIXTURE_USERNAME:
            return []
        return ["1000"] if not since_id else []

    async def _twitter_timeline(self, username, since_id="", limit=0):
        if str(username).casefold() != TWITTER_FIXTURE_USERNAME or int(since_id or 0) >= 1001:
            return []
        return [{"tweet_id": "1001", "username": TWITTER_FIXTURE_USERNAME, "is_retweet": False}]

    async def _twitter_tweet(self, username, tweet_id):
        if str(username).casefold() != TWITTER_FIXTURE_USERNAME or str(tweet_id) != "1001":
            return {"status": False, "username": username, "tweet_id": str(tweet_id)}
        return {
            "status": True,
            "username": TWITTER_FIXTURE_USERNAME,
            "screen_name": "V2 本地夹具",
            "tweet_id": "1001",
            "text": "本地夹具：Twitter 推文图片与正文已送入 AstrBot 原生投递链路。",
            "images": [self._image_url],
            "videos": [],
            "quote": None,
            "date": "2026-10-19T00:00:00Z",
            "url": f"https://x.com/{TWITTER_FIXTURE_USERNAME}/status/1001",
        }

    async def _bili_user_info(self, uid: int):
        return await _bili_user_info(self._bilibili.bili_client, uid)

    async def _bili_dynamics(self, uid: int):
        if int(uid) != BILIBILI_FIXTURE_UID:
            return None
        latest = "3001" if _BILI_DYNAMIC_ENABLED else "3000"
        ids = [latest, "3000"] if latest != "3000" else ["3000"]
        items = []
        for dyn_id in ids:
            body = "本地夹具：Bilibili 图文动态已更新" if dyn_id == "3001" else "本地夹具订阅起点"
            items.append({
                "id_str": dyn_id,
                "type": "DYNAMIC_TYPE_DRAW",
                "modules": {
                    "module_author": {"name": "V2 本地夹具", "face": self._image_url, "pendant": {}},
                    "module_dynamic": {
                        "desc": {"text": body},
                        "topic": None,
                        "major": {"type": "MAJOR_TYPE_OPUS", "opus": {
                            "summary": {"text": body, "rich_text_nodes": []},
                            "jump_url": f"//t.bilibili.com/{dyn_id}",
                            "title": "隔离沙箱动态夹具",
                            "pics": [{"url": self._image_url}],
                        }},
                    },
                },
            })
        return {"items": items}

    @filter.command("business_poll_fixture")
    async def poll_twitter_fixture(self, event: AstrMessageEvent):
        """Run one real Twitter PollingService check for the fixture author."""
        if not self._loaded or self._twitter is None:
            yield event.plain_result("business fixture: TwitterPlugin unavailable")
            return
        umo = str(event.unified_msg_origin)
        subscriptions = await self._twitter.subscription_service.get_all()
        key = self._twitter.subscription_service.find_key(subscriptions, TWITTER_FIXTURE_USERNAME)
        info = subscriptions.get(key, {}).get("subscribers", {}).get(umo) if key else None
        if not isinstance(info, dict):
            yield event.plain_result("business fixture: subscribe this sandbox conversation to v2_fixture first")
            return
        await self._twitter.polling_service.check_user(key, subscriptions[key])
        yield event.plain_result(json.dumps({"twitter_polled": True, "author": key, "conversation": umo}, ensure_ascii=False))

    @filter.command("business_poll_bili_fixture")
    async def poll_bili_fixture(self, event: AstrMessageEvent):
        """Run one real Bilibili DynamicListener pass for the fixture UID."""
        global _BILI_DYNAMIC_ENABLED
        if not self._loaded or self._bilibili is None:
            yield event.plain_result("business fixture: Bilibili plugin unavailable")
            return
        umo = str(event.unified_msg_origin)
        records = self._bilibili.data_manager.get_subscriptions_by_user(umo) or []
        record = next((item for item in records if int(item.uid) == BILIBILI_FIXTURE_UID), None)
        if record is None:
            yield event.plain_result(f"business fixture: subscribe UID {BILIBILI_FIXTURE_UID} first")
            return
        _BILI_DYNAMIC_ENABLED = True
        await self._bilibili.dynamic_listener._run_uid_task(
            BILIBILI_FIXTURE_UID, [(umo, record)]
        )
        yield event.plain_result(json.dumps({"bilibili_polled": True, "uid": BILIBILI_FIXTURE_UID, "conversation": umo}, ensure_ascii=False))

    async def terminate(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
