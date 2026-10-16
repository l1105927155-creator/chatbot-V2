"""Local REST control plane and OneBot v11 reverse-WebSocket peer.

Run with the AstrBot venv so the exact pinned websocket dependency is used:
``python -m sandbox.server --data-dir .runtime/sandbox --onebot-url ws://127.0.0.1:6220/ws``
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import mimetypes
import secrets
import sqlite3
import socket
import time
import os
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from aiohttp import web
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .model import BOT_ID, BOT_NAME, SandboxStore, make_message_event
from .control import SandboxControl, RevisionConflict
from .evidence import EvidenceSnapshotter, _event_cursor

LOG = logging.getLogger("sandbox")
MAX_UPLOAD = 25 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def validate_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("OneBot target must be a ws(s) URL without credentials")
    host = parsed.hostname.lower().rstrip(".")
    if host not in LOOPBACK_NAMES:
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("OneBot target must use a loopback address")
        except ValueError as exc:
            if str(exc) == "OneBot target must use a loopback address":
                raise
            raise ValueError("OneBot target must use a loopback address") from exc
    if not parsed.path:
        parsed = parsed._replace(path="/ws")
    return parsed.geturl()


def _is_loopback(value: str | None) -> bool:
    if not value:
        return False
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _host_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0].strip("[]").lower()
    return host in LOOPBACK_NAMES or _is_loopback(host)


def _check_local_request(request: web.Request) -> None:
    peer = request.remote
    if not _is_loopback(peer) or not _host_loopback(request.headers.get("Host")):
        raise web.HTTPForbidden(text="sandbox only accepts loopback requests")
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not _host_loopback(parsed.netloc) or parsed.netloc.lower() != request.headers.get("Host", "").lower():
            raise web.HTTPForbidden(text="cross-origin requests are not allowed")
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        raise web.HTTPForbidden(text="cross-site requests are not allowed")
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        content_type = request.content_type.lower()
        expected = "multipart/form-data" if request.path == "/api/upload" else "application/json"
        if content_type != expected:
            raise web.HTTPUnsupportedMediaType(text=f"expected {expected}")


@web.middleware
async def local_only(request: web.Request, handler):
    _check_local_request(request)
    return await handler(request)


class OneBotPeer:
    """Reconnectable OneBot universal reverse-WS client and action responder."""

    def __init__(self, app: "SandboxApp", url: str, token: str | None,
                 api_timeout: float = 180.0, fault_delay: float = 181.0):
        self.app = app
        self.url = validate_loopback_url(url)
        self.token = token
        self.api_timeout = api_timeout
        self.fault_delay = fault_delay
        self.ws: ClientConnection | None = None
        self.connected = asyncio.Event()
        self.enabled = True
        self.stopping = False
        self.send_lock = asyncio.Lock()
        self.pending_faults: dict[str, deque[tuple[float, dict[str, bool]]]] = defaultdict(deque)
        self.action_tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        backoff = 0.4
        while not self.stopping:
            if not self.enabled:
                await asyncio.sleep(0.1)
                continue
            try:
                headers = {"X-Self-ID": BOT_ID, "X-Client-Role": "universal"}
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                async with connect(self.url, additional_headers=headers, proxy=None, open_timeout=5,
                                   close_timeout=1, max_size=8 * 1024 * 1024,
                                   ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    self.connected.set()
                    self.app.connection_changed(True)
                    backoff = 0.4
                    LOG.info("connected to isolated AstrBot OneBot endpoint")
                    await self._receive(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.enabled and not self.stopping:
                    LOG.warning("OneBot connection unavailable: %s", type(exc).__name__)
            finally:
                self.ws = None
                self.connected.clear()
                self.app.connection_changed(False)
                pending = tuple(self.action_tasks)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.8, 5.0)

    async def _receive(self, ws: ClientConnection) -> None:
        async for frame in ws:
            try:
                payload = json.loads(frame)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and "action" in payload:
                task = asyncio.create_task(self._handle_action(payload, ws))
                self.action_tasks.add(task)
                task.add_done_callback(self.action_tasks.discard)
            elif isinstance(payload, dict) and "post_type" in payload:
                # AstrBot is not expected to emit events over this reverse connection.
                self.app.store.record_action("unexpected_event", payload, None, "ignored",
                                             "OneBot peer received event from AstrBot")

    async def send_event(self, event: dict[str, Any], ws: ClientConnection | None = None) -> None:
        ws = ws or self.ws
        if ws is None:
            raise RuntimeError("OneBot reverse WebSocket is disconnected")
        async with self.send_lock:
            await ws.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    async def disconnect(self) -> None:
        ws = self.ws
        if ws:
            await ws.close(code=1012, reason="sandbox injected disconnect")

    async def _handle_action(self, request: dict[str, Any], reply_ws: ClientConnection | None = None) -> None:
        action = str(request.get("action", ""))
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        is_send = action in {"send_group_msg", "send_private_msg", "send_msg", "send_group_forward_msg", "send_private_forward_msg"}
        conv_type = "group" if "group" in action or (action == "send_msg" and params.get("message_type") == "group") else "private"
        conv_id = params.get("group_id") if conv_type == "group" else params.get("user_id")
        conv_key = f"{conv_type}:{conv_id}"
        fault: dict[str, bool] = {}
        if is_send and conv_id is not None:
            queue = self.pending_faults.get(conv_key)
            while queue and time.monotonic() - queue[0][0] > 120:
                queue.popleft()
            if queue:
                _, fault = queue.popleft()
            if not queue:
                self.pending_faults.pop(conv_key, None)
        feedback = None
        try:
            response_data, outbound = await self.app.handle_action(action, params, fault=fault)
            response = {"status": "ok", "retcode": 0, "data": response_data,
                        "echo": request.get("echo")}
            if outbound is not None:
                self.app.store.add_event(outbound, "outbound")
            if fault.get("drop_feedback"):
                feedback = "dropped"
                outbound = None
            elif outbound is not None:
                feedback = "delivered"
            if outbound is not None:
                await self.send_event(outbound, reply_ws)
            if fault.get("timeout"):
                # Simulate an action that occurred but whose response arrives after the client deadline.
                await asyncio.sleep(self.fault_delay)
            self.app.store.record_action(action, params, response,
                                         "delayed_response" if fault.get("timeout") else "ok",
                                         feedback=feedback)
            ws = reply_ws or self.ws
            if ws is not None:
                async with self.send_lock:
                    await ws.send(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = {"status": "failed", "retcode": 1404 if isinstance(exc, KeyError) else 1001,
                        "data": None, "message": str(exc), "wording": str(exc),
                        "echo": request.get("echo")}
            self.app.store.record_action(action, params, response, "failed", str(exc), feedback=feedback)
            ws = reply_ws or self.ws
            if ws is not None:
                try:
                    async with self.send_lock:
                        await ws.send(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
                except ConnectionClosed:
                    pass


class SandboxApp:
    def __init__(self, data_dir: str | Path, onebot_url: str, token: str | None = None,
                 bind: str = "127.0.0.1", port: int = 6221, asset_base_url: str | None = None,
                 fault_delay: float = 181.0):
        if not _is_loopback(bind):
            raise ValueError("sandbox HTTP server must bind to loopback")
        self.store = SandboxStore(data_dir)
        self.control = SandboxControl(self.store, owner_id=os.environ.get("SANDBOX_OWNER_ID") or None)
        self.evidence = EvidenceSnapshotter(self.store.data_dir.parent)
        self.bind = bind
        self.port = port
        self.asset_base_url = asset_base_url or f"http://127.0.0.1:{port}"
        if not _host_loopback(urlsplit(self.asset_base_url).netloc):
            raise ValueError("asset base URL must use loopback")
        self.peer = OneBotPeer(self, onebot_url, token or os.environ.get("ONEBOT_ACCESS_TOKEN"), fault_delay=fault_delay)
        self.control_token = os.environ.get("SANDBOX_CONTROL_TOKEN") or self.peer.token or secrets.token_urlsafe(32)
        self.artifact_root = Path(os.environ.get("SANDBOX_ARTIFACT_ROOT", str(self.store.data_dir / "astrbot-artifacts"))).resolve()
        self.manual_connected = True
        self.last_error: str | None = None
        self.app = web.Application(client_max_size=MAX_JSON, middlewares=[local_only])
        self.app.add_routes([
            web.get("/", self.api_index),
            web.get("/static/{filename}", self.api_static),
            web.get("/api/state", self.api_state),
            web.get("/api/scenario", self.api_scenario),
            web.put("/api/scenario", self.api_scenario),
            web.get("/api/evidence", self.api_evidence),
            web.get("/api/evidence/export", self.api_evidence),
            web.get("/api/events", self.api_events),
            web.post("/api/wait", self.api_wait),
            web.get("/api/faults", self.api_faults),
            web.post("/api/faults", self.api_faults),
            web.delete("/api/faults/{fault_id}", self.api_cancel_fault),
            web.post("/api/scripts", self.api_script),
            web.post("/api/internal/claim", self.api_claim),
            web.post("/api/internal/probe", self.api_probe),
            web.post("/api/members", self.api_add_member),
            web.patch("/api/members/{member_id}", self.api_update_member),
            web.post("/api/conversations", self.api_add_conversation),
            web.post("/api/send", self.api_send),
            web.post("/api/upload", self.api_upload),
            web.get("/api/assets/{asset_id}", self.api_asset),
            web.get("/api/forward/{forward_id}", self.api_forward),
            web.post("/api/replay", self.api_replay),
            web.post("/api/connect", self.api_connect),
        ])
        self.runner: web.AppRunner | None = None
        self.peer_task: asyncio.Task | None = None

    def connection_changed(self, connected: bool) -> None:
        if not connected:
            self.last_error = "OneBot WebSocket disconnected"
        else:
            self.last_error = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.bind, self.port)
        await site.start()
        self.peer_task = asyncio.create_task(self.peer.run(), name="sandbox-onebot-peer")

    async def close(self) -> None:
        self.peer.stopping = True
        if self.peer_task:
            self.peer_task.cancel()
            try:
                await self.peer_task
            except asyncio.CancelledError:
                pass
        tasks = tuple(self.peer.action_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        self.store.close()

    def state(self) -> dict[str, Any]:
        return {"connected": self.peer.connected.is_set(), "connecting": self.peer.enabled and not self.peer.connected.is_set(),
                "scenario": self.control.scenario(),
                "mode": __import__("os").environ.get("SANDBOX_DSH_MODE", "disabled"),
                "bot": {"id": BOT_ID, "nickname": BOT_NAME}, "members": self.store.members(),
                "conversations": self.store.conversations(), "events": self.store.events(),
                "forwards": self.store.forward_list(),
                "actions": self.store.actions(), "assets": self.store.asset_list(),
                "faults": {"pending": {k: [item[1] for item in values] for k, values in self.peer.pending_faults.items()}},
                "error": self.last_error}

    async def api_index(self, request: web.Request) -> web.Response:
        path = Path(__file__).parent / "static" / "index.html"
        if not path.is_file():
            raise web.HTTPNotFound()
        headers = {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                   "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
        return web.FileResponse(path, headers=headers)

    async def api_static(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        if filename not in {"app.js", "style.css"}:
            raise web.HTTPNotFound()
        path = Path(__file__).parent / "static" / filename
        headers = {"Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'",
                   "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
        return web.FileResponse(path, headers=headers)

    async def api_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state())

    async def api_scenario(self, request):
        if request.method == "GET":
            return web.json_response(self.control.scenario())
        try:
            return web.json_response(self.control.update_scenario(await _json_body(request)))
        except RevisionConflict as exc:
            return _error(str(exc), 409)
        except ValueError as exc:
            return _error(str(exc))

    async def api_evidence(self, request):
        try:
            snapshot = self.evidence.snapshot(conversation_key=request.query.get("conversation_key"),
                                              session_id=request.query.get("session_id"), limit=250)
            headers = {"Cache-Control": "no-store"}
            if request.path.endswith("/export"):
                headers["Content-Disposition"] = 'attachment; filename="sandbox-evidence.json"'
            return web.json_response(snapshot, headers=headers)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return _error("evidence unavailable: " + type(exc).__name__, 503)

    async def api_events(self, request):
        events = self.evidence._all_lifecycle()
        cursor = _event_cursor(events[-1]) if events else request.query.get("after")
        after = request.query.get("after")
        if after:
            index = next((i for i, event in enumerate(events) if _event_cursor(event) == after), None)
            if index is None:
                return web.json_response({"events": [], "cursor": after, "cursor_expired": True}, status=409)
            events = events[index + 1:]
        for key in ("conversation_key", "session_id"):
            if request.query.get(key):
                events = [event for event in events if event.get(key) == request.query[key]]
        return web.json_response({"events": events, "cursor": cursor})

    async def api_wait(self, request):
        data = await _json_body(request)
        selector = {k: v for k, v in data.items() if k not in {"after", "timeout_seconds"}}
        try:
            timeout = float(data.get("timeout_seconds", 30))
            if not 0 < timeout <= 30:
                raise ValueError("timeout_seconds must be between 0 and 30")
            result = await self.evidence.wait_event(selector, after_cursor=data.get("after"), timeout=timeout)
            return web.json_response(result)
        except (TypeError, ValueError) as exc:
            return _error(str(exc))

    async def api_faults(self, request):
        if request.method == "GET":
            return web.json_response({"faults": self.control.faults()})
        try:
            return web.json_response(self.control.arm_fault(await _json_body(request)), status=201)
        except ValueError as exc:
            return _error(str(exc))

    async def api_cancel_fault(self, request):
        fault = self.control.cancel_fault(request.match_info["fault_id"])
        return web.json_response(fault) if fault else _error("fault not found", 404)

    async def api_script(self, request):
        if os.environ.get("SANDBOX_DSH_MODE") != "scripted":
            return _error("scripted plans require startup scripted mode", 409)
        try:
            return web.json_response(self.control.add_script(await _json_body(request)), status=201)
        except ValueError as exc:
            return _error(str(exc))

    def _native_authorized(self, request):
        if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.control_token):
            raise web.HTTPUnauthorized(text="sandbox native probe authorization required")

    def _session_conversation(self, session_id):
        if not session_id:
            return None
        keys = set()
        for _domain, path in self.evidence.state_sources():
            rows = self.evidence._db_rows(path, "SELECT conversation_key FROM conversation_state WHERE session_id=?", (str(session_id),))
            keys.update(row["conversation_key"] for row in rows)
        return next(iter(keys)) if len(keys) == 1 else None

    async def api_claim(self, request):
        self._native_authorized(request)
        data = await _json_body(request)
        try:
            return web.json_response(self.control.claim(data, conversation_key=self._session_conversation(data.get("session_id"))))
        except ValueError as exc:
            return _error(str(exc))

    async def api_probe(self, request):
        self._native_authorized(request)
        data = await _json_body(request)
        try:
            recorded = self.control.record_probe(data, conversation_key=self._session_conversation(data.get("session_id")))
            return web.json_response({"cursor": recorded["cursor"]})
        except ValueError as exc:
            return _error(str(exc))

    async def api_add_member(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        nickname = _required_text(data, "nickname", max_len=100)
        try:
            member = self.store.add_member(data.get("id"), nickname, str(data.get("card", ""))[:100])
        except sqlite3.IntegrityError:
            return _error("member id already exists", 409)
        except ValueError as exc:
            return _error(str(exc))
        except ValueError as exc:
            return _error(str(exc))
        return web.json_response(member, status=201)

    async def api_update_member(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        nickname = data.get("nickname")
        card = data.get("card")
        if nickname is not None and (not isinstance(nickname, str) or not nickname.strip()):
            return _error("nickname must be non-empty text")
        member = self.store.update_member(request.match_info["member_id"], nickname, card)
        return web.json_response(member) if member else _error("member not found", 404)

    async def api_add_conversation(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        try:
            conv = self.store.add_conversation(str(data.get("type", "")), data.get("id"), str(data.get("title", ""))[:160])
        except ValueError as exc:
            return _error(str(exc))
        except sqlite3.IntegrityError:
            return _error("conversation already exists", 409)
        return web.json_response(conv, status=201)

    async def api_send(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        conv = data.get("conversation")
        if not isinstance(conv, dict) or conv.get("type") not in ("group", "private") or conv.get("id") is None:
            return _error("conversation must contain type and id")
        conv = {"type": str(conv["type"]), "id": str(conv["id"])}
        if not self._find_conversation(conv["type"], conv["id"]):
            return _error("unknown conversation", 404)
        sender = self.store.get_member(str(data.get("sender_id", "")))
        if not sender:
            return _error("unknown sender", 404)
        if conv["type"] == "private" and sender["id"] != conv["id"]:
            return _error("private conversation identity must match sender", 400)
        message = data.get("message")
        if not isinstance(message, list) or not message:
            return _error("message must be a non-empty OneBot segment array")
        try:
            message = [self._validate_segment(s) for s in message]
        except ValueError as exc:
            return _error(str(exc))
        nodes = [s for s in message if s["type"] == "node"]
        if nodes:
            if len(nodes) != len(message):
                return _error("forward node segments must be sent as a single forward message")
            forward_id = f"fwd-in-{self.store.next_message_id()}"
            self.store.set_forward(forward_id, nodes)
            message = [{"type": "forward", "data": {"id": forward_id}}]
        reply_to = data.get("reply_to")
        if reply_to is not None and not isinstance(reply_to, (str, int)):
            return _error("reply_to must be a message id")
        if reply_to is not None and any(s["type"] == "reply" for s in message):
            return _error("use reply_to or a reply segment, not both")
        faults = data.get("faults") or {}
        if not isinstance(faults, dict) or any(k not in {"duplicate", "disconnect", "timeout", "drop_feedback"} for k, v in faults.items() if v):
            return _error("unsupported fault injection")
        if not self.peer.connected.is_set():
            return _error("OneBot reverse WebSocket is disconnected", 503)
        try:
            event = self._new_event(conv, sender, message, reply_to)
        except ValueError as exc:
            return _error(str(exc))
        outgoing_faults = {k: bool(v) for k, v in faults.items() if v and k in {"timeout", "drop_feedback"}}
        if outgoing_faults:
            key = f"{conv['type']}:{conv['id']}"
            self.peer.pending_faults[key].append((time.monotonic(), outgoing_faults))
        saved = self.store.add_event(event, "inbound")
        try:
            await self.peer.send_event(event)
            if faults.get("duplicate"):
                await self.peer.send_event(event)
            if faults.get("disconnect"):
                asyncio.get_running_loop().call_later(0.1, lambda: asyncio.create_task(self.peer.disconnect()))
        except Exception as exc:
            return _error(f"failed to deliver event: {type(exc).__name__}", 503)
        return web.json_response({"event": saved, "injected": faults}, status=202)

    def _new_event(self, conv: dict[str, str], sender: dict[str, str], message: list[dict[str, Any]], reply_to: Any) -> dict[str, Any]:
        msg_id = self.store.next_message_id()
        normalized = []
        for segment in message:
            normalized.append(self._expand_inbound_segment(segment))
        for segment in normalized:
            if segment["type"] == "forward":
                forward_id = str(segment["data"].get("id", ""))
                stored_nodes = self.store.get_forward(forward_id) if forward_id else None
                if forward_id and stored_nodes is None:
                    raise ValueError("forward message id is unknown")
                elif forward_id and stored_nodes is not None:
                    self.store.set_forward(forward_id, [self._expand_inbound_segment(n) for n in stored_nodes])
        return make_message_event(conv, sender, normalized, msg_id,
                                  int(reply_to) if reply_to is not None else None)

    def _expand_inbound_segment(self, segment: dict[str, Any]) -> dict[str, Any]:
        copy = {"type": segment["type"], "data": dict(segment["data"])}
        if copy["type"] in {"image", "file", "record", "video"}:
            asset_id = self._inbound_asset_id(copy["data"])
            asset = self.store.get_asset(asset_id)
            if not asset:
                raise ValueError("unknown sandbox asset")
            url = f"{self.asset_base_url}/api/assets/{asset_id}"
            copy["data"].update({"file": url, "url": url, "file_id": asset_id,
                                 "name": copy["data"].get("name", asset["name"])})
        elif copy["type"] == "node":
            copy["data"]["user_id"] = copy["data"].get("user_id", copy["data"].get("uin"))
            copy["data"]["nickname"] = copy["data"].get("nickname", copy["data"].get("name", ""))
            copy["data"]["content"] = [self._expand_inbound_segment(s) for s in copy["data"].get("content", copy["data"].get("message", []))]
        elif copy["type"] == "forward" and isinstance(copy["data"].get("content"), list):
            copy["data"]["content"] = [self._expand_inbound_segment(s) for s in copy["data"]["content"]]
            self.store.set_forward(str(copy["data"]["id"]), copy["data"]["content"])
        return copy

    def _inbound_asset_id(self, data: dict[str, Any]) -> str:
        value = data.get("file", data.get("url", ""))
        asset_id = ""
        if isinstance(value, str) and value.startswith("sandbox://"):
            asset_id = value[10:]
        elif isinstance(value, str):
            # Existing messages can be forwarded again. Only resolve an opaque
            # fixture reference, never fetch a URL supplied by the caller.
            parsed = urlsplit(value)
            if parsed.scheme in {"http", "https"} and _is_loopback(parsed.hostname):
                prefix = "/api/assets/"
                if parsed.path.startswith(prefix):
                    asset_id = parsed.path[len(prefix):]
        if not asset_id or not self.store.get_asset(asset_id):
            raise ValueError("media must reference an uploaded sandbox asset")
        return asset_id

    def _validate_segment(self, segment: Any, depth: int = 0) -> dict[str, Any]:
        if depth > 8:
            raise ValueError("forward nesting exceeds 8 levels")
        allowed = {"text", "at", "reply", "face", "image", "file", "record", "video", "forward", "node", "json"}
        if not isinstance(segment, dict) or segment.get("type") not in allowed or not isinstance(segment.get("data", {}), dict):
            raise ValueError("invalid OneBot message segment")
        result = {"type": segment["type"], "data": dict(segment.get("data", {}))}
        typ, data = result["type"], result["data"]
        if typ in {"image", "file", "record", "video"}:
            data["file"] = "sandbox://" + self._inbound_asset_id(data)
        if typ == "node":
            data["user_id"] = data.get("user_id", data.get("uin"))
            data["nickname"] = data.get("nickname", data.get("name", ""))
            content = data.get("content", data.get("message"))
            if not isinstance(content, list):
                raise ValueError("node.data.content must be a segment array")
            data["content"] = [self._validate_segment(child, depth + 1) for child in content]
        if typ == "forward" and isinstance(data.get("content"), list):
            content = data["content"]
            if not all(isinstance(n, dict) and n.get("type") == "node" for n in content):
                raise ValueError("forward.data.content must contain node segments")
            data["content"] = [self._validate_segment(n, depth + 1) for n in content]
            forward_id = str(data.get("id") or f"fwd-in-{self.store.next_message_id()}")
            self.store.set_forward(forward_id, data["content"])
            data["id"] = forward_id
        if typ == "forward" and not self.store.get_forward(str(data.get("id", ""))):
            raise ValueError("forward message id is unknown")
        if typ == "at" and str(data.get("qq", "")) not in ("all",) and not re.fullmatch(r"[1-9][0-9]*", str(data.get("qq", ""))):
            raise ValueError("at.qq must be a member id or 'all'")
        if typ == "reply" and not re.fullmatch(r"[1-9][0-9]*", str(data.get("id", ""))):
            raise ValueError("reply.id must be a message id")
        if typ == "text" and not isinstance(data.get("text"), str):
            raise ValueError("text.data.text must be text")
        return result

    async def api_upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            return _error("multipart field 'file' is required")
        data = bytearray()
        while chunk := await part.read_chunk(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_UPLOAD:
                return _error("uploaded file exceeds 25 MiB", 413)
        name = Path(part.filename or "upload.bin").name[:255]
        content_type = part.headers.get("Content-Type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        asset = self.store.save_asset(name, content_type, bytes(data))
        kind = "image" if asset["content_type"].startswith("image/") else "file"
        segment = {"type": kind, "data": {"file": f"sandbox://{asset['id']}", "name": name}}
        return web.json_response({"asset": asset, "segment": segment}, status=201)

    async def api_asset(self, request: web.Request) -> web.Response:
        asset_id = request.match_info["asset_id"]
        path = self.store.asset_path(asset_id)
        asset = self.store.get_asset(asset_id)
        if not path or not asset:
            raise web.HTTPNotFound()
        safe_image = asset["content_type"] in {"image/png", "image/jpeg", "image/gif", "image/webp"}
        headers = {"Content-Type": asset["content_type"], "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "default-src 'none'; sandbox",
                   "Cache-Control": "private, max-age=60",
                   "Content-Disposition": "inline" if safe_image else f"attachment; filename*=UTF-8''{asset['name'].replace(' ', '%20')}"}
        return web.FileResponse(path, headers=headers)

    async def api_forward(self, request: web.Request) -> web.Response:
        messages = self.store.get_forward(request.match_info["forward_id"])
        if messages is None:
            return _error("forward message not found", 404)
        return web.json_response({"message": messages})

    async def api_replay(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        if not isinstance(data.get("message_id"), (str, int)) or not str(data["message_id"]).isdigit():
            return _error("message_id must be numeric")
        event_record = self.store.event(data.get("message_id", -1))
        if not event_record or event_record["direction"] != "inbound":
            return _error("inbound message not found", 404)
        if not self.peer.connected.is_set():
            return _error("OneBot reverse WebSocket is disconnected", 503)
        mode = data.get("mode", "duplicate")
        if mode not in {"duplicate", "new"}:
            return _error("mode must be duplicate or new")
        event = dict(event_record["event"])
        if mode == "new":
            event["message_id"] = self.store.next_message_id()
            event["time"] = int(time.time())
            event_record = self.store.add_event(event, "inbound")
        await self.peer.send_event(event)
        record = self.store.record_action("replay_event", {"message_id": data.get("message_id"), "mode": mode},
                                          {"status": "ok", "message_id": event["message_id"]}, "replayed")
        return web.json_response({"event": event_record, "action": record, "mode": mode}, status=202)

    async def api_connect(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        enabled = data.get("connected")
        if not isinstance(enabled, bool):
            return _error("connected must be a boolean")
        self.peer.enabled = enabled
        if not enabled:
            await self.peer.disconnect()
        return web.json_response({"enabled": enabled, "connected": self.peer.connected.is_set()})

    async def handle_action(self, action: str, params: dict[str, Any], fault: dict[str, bool] | None = None) -> tuple[Any, dict[str, Any] | None]:
        """Implement the OneBot v11 actions used by AstrBot and its plugins."""
        fault = fault or {}
        outbound: dict[str, Any] | None = None
        if action == "get_login_info":
            return {"user_id": int(BOT_ID), "nickname": BOT_NAME}, None
        if action == "get_status":
            return {"online": True, "good": self.peer.connected.is_set()}, None
        if action == "get_version_info":
            return {"app_name": "sandbox-onebot-v11", "app_version": "1.0", "protocol_version": "11"}, None
        if action in ("get_group_list", "get_group_info"):
            groups = [c for c in self.store.conversations() if c["type"] == "group"]
            if action == "get_group_list":
                return [{"group_id": int(g["id"]), "group_name": g["title"], "member_count": len(self.store.members()) + 1, "max_member_count": 500} for g in groups], None
            group = self._find_conversation("group", params.get("group_id"))
            if not group:
                raise KeyError("group not found")
            return {"group_id": int(group["id"]), "group_name": group["title"],
                    "member_count": len(self.store.members()) + 1, "max_member_count": 500}, None
        if action in ("get_group_member_list", "get_group_member_info"):
            group = self._find_conversation("group", params.get("group_id"))
            if not group:
                raise KeyError("group not found")
            bot_member = {"id": BOT_ID, "nickname": BOT_NAME, "card": ""}
            members = [*self.store.members(), bot_member]
            if action == "get_group_member_list":
                return [self._member_info(m, group) for m in members], None
            member_id = str(params.get("user_id", ""))
            member = bot_member if member_id == BOT_ID else self.store.get_member(member_id)
            if not member:
                raise KeyError("member not found")
            return self._member_info(member, group), None
        if action == "get_stranger_info":
            member_id = str(params.get("user_id", ""))
            member = ({"id": BOT_ID, "nickname": BOT_NAME, "card": ""}
                      if member_id == BOT_ID else self.store.get_member(member_id))
            if not member:
                raise KeyError("member not found")
            return {"user_id": int(member["id"]), "nickname": member["nickname"], "sex": "unknown", "age": 0}, None
        if action == "get_msg":
            msg = self.store.event(params.get("message_id", -1))
            if not msg:
                raise KeyError("message not found")
            ev = msg["event"]
            result = {"post_type": "message", "time": ev["time"], "self_id": ev["self_id"],
                      "message_type": ev["message_type"], "message_id": ev["message_id"],
                      "real_id": ev["message_id"], "user_id": ev["user_id"], "sender": ev["sender"],
                      "message": ev["message"], "raw_message": ev["raw_message"],
                      "sub_type": ev.get("sub_type", "normal")}
            if "group_id" in ev:
                result["group"] = {"id": ev["group_id"], "name": self._find_conversation("group", ev["group_id"])["title"]}
            return result, None
        if action == "get_forward_msg":
            forward_id = str(params.get("id", ""))
            messages = self.store.get_forward(forward_id)
            if messages is None:
                raise KeyError("forward message not found")
            api_messages = self._forward_api_nodes(messages)
            return {"messages": api_messages, "message": api_messages}, None
        if action in ("get_group_file_url", "get_private_file_url"):
            asset = self.store.get_asset(str(params.get("file_id", "")))
            if not asset:
                raise KeyError("file not found")
            return {"url": f"{self.asset_base_url}/api/assets/{asset['id']}", "file_name": asset["name"]}, None
        if action in ("send_group_msg", "send_private_msg", "send_msg", "send_group_forward_msg", "send_private_forward_msg"):
            conv_type = "group" if "group" in action or (action == "send_msg" and params.get("message_type") == "group") else "private"
            conv_id = params.get("group_id") if conv_type == "group" else params.get("user_id")
            conv = self._find_conversation(conv_type, conv_id)
            if not conv:
                raise KeyError(f"{conv_type} conversation not found")
            message = params.get("message", [])
            forward_id = None
            if action.endswith("forward_msg"):
                nodes = params.get("messages", message)
                if not isinstance(nodes, list):
                    raise ValueError("forward messages must be an array")
                forward_id = f"fwd-{self.store.next_message_id()}"
                normalized_nodes = []
                for node in nodes:
                    if not isinstance(node, dict) or node.get("type") != "node":
                        raise ValueError("forward messages must contain node segments")
                    normalized_nodes.append(self._normalize_outbound_segment(node))
                self.store.set_forward(forward_id, normalized_nodes)
                message = [{"type": "forward", "data": {"id": forward_id}}]
            if isinstance(message, str):
                message = [{"type": "text", "data": {"text": message}}]
            elif isinstance(message, dict):
                message = [message]
            if not isinstance(message, list):
                raise ValueError("outbound message must be segments")
            normalized = []
            for seg in message:
                if not isinstance(seg, dict) or "type" not in seg:
                    raise ValueError("invalid outbound segment")
                # Preserve the adapter's OneBot segment payload. Never follow arbitrary file URLs.
                normalized.append(self._normalize_outbound_segment({"type": str(seg["type"]), "data": seg.get("data", {})}))
            out_msg_id = self.store.next_message_id()
            self_id_sender = {"id": BOT_ID, "nickname": BOT_NAME, "card": ""}
            outbound = make_message_event(conv, self_id_sender, normalized, out_msg_id, self_id=BOT_ID)
            outbound["post_type"] = "message_sent"
            outbound["message_sent_type"] = "self"
            outbound["sub_type"] = "normal" if conv_type == "group" else "friend"
            if conv_type == "private":
                outbound["user_id"] = int(BOT_ID)
                outbound["target_id"] = int(conv_id)
                outbound["sender"]["user_id"] = int(BOT_ID)
            response = {"message_id": out_msg_id}
            return response, outbound
        if action in {"set_group_kick", "set_group_ban", "set_group_whole_ban", "set_group_card",
                      "set_group_name", "set_friend_add_request", "set_group_add_request", "delete_msg",
                      "send_like", "upload_group_file", "upload_private_file"}:
            # Known state-changing capabilities are explicitly unsupported instead of fake success.
            raise ValueError(f"sandbox does not implement action: {action}")
        raise ValueError(f"unsupported OneBot action: {action}")

    @staticmethod
    def _forward_api_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the receive-side shape consumed by the pinned AstrBot forward parser."""
        result: list[dict[str, Any]] = []
        for node in nodes:
            if isinstance(node, dict) and node.get("type") == "node":
                data = node.get("data") or {}
                result.append({"sender": {"user_id": data.get("user_id", data.get("uin")),
                                           "nickname": data.get("nickname", data.get("name", "")),
                                           "card": data.get("nickname", data.get("name", ""))},
                               "message": data.get("content", data.get("message", []))})
            elif isinstance(node, dict) and isinstance(node.get("sender"), dict):
                result.append(node)
        return result

    def _normalize_outbound_segment(self, segment: dict[str, Any]) -> dict[str, Any]:
        typ = segment["type"]
        data = dict(segment["data"] or {})
        if typ == "node":
            data["user_id"] = data.get("user_id", data.get("uin"))
            data["nickname"] = data.get("nickname", data.get("name", ""))
            content = data.get("content", data.get("message"))
            if not isinstance(content, list):
                raise ValueError("outbound node must have segment-array content")
            data["content"] = [self._normalize_outbound_segment(child) for child in content]
            return {"type": typ, "data": data}
        if typ == "forward" and isinstance(data.get("content"), list):
            data["content"] = [self._normalize_outbound_segment(child) for child in data["content"]]
            forward_id = str(data.get("id") or f"fwd-{self.store.next_message_id()}")
            self.store.set_forward(forward_id, data["content"])
            data["id"] = forward_id
        if typ not in {"image", "file", "record", "video"}:
            return {"type": typ, "data": data}
        value = data.get("file")
        asset = None
        if isinstance(value, str) and value.startswith("sandbox://"):
            asset = self.store.get_asset(value[10:])
        elif isinstance(value, str) and value.startswith("base64://"):
            import base64
            raw = base64.b64decode(value[9:], validate=True)
            if len(raw) > MAX_UPLOAD:
                raise ValueError("outbound media exceeds limit")
            asset = self.store.save_asset(str(data.get("name", f"outbound.{typ}")), str(data.get("content_type", "application/octet-stream")), raw)
        elif isinstance(value, str) and value.startswith("data:") and ";base64," in value:
            import base64
            meta, encoded = value.split(",", 1)
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_UPLOAD:
                raise ValueError("outbound media exceeds limit")
            content_type = meta[5:].split(";", 1)[0]
            asset = self.store.save_asset(str(data.get("name", f"outbound.{typ}")), content_type, raw)
        elif isinstance(value, str) and value.startswith("file://"):
            from urllib.parse import unquote, urlsplit
            parsed = urlsplit(value)
            if parsed.netloc not in ("", "localhost"):
                raise ValueError("outbound file URI authority is not allowed")
            candidate = Path(unquote(parsed.path))
            try:
                relative = candidate.absolute().relative_to(self.artifact_root)
            except ValueError as exc:
                raise ValueError("outbound file is outside the isolated AstrBot artifact root") from exc
            probe = self.artifact_root
            for part in relative.parts:
                probe = probe / part
                if probe.is_symlink():
                    raise ValueError("symlinked outbound artifacts are not allowed")
            path = candidate.resolve(strict=True)
            if not path.is_relative_to(self.artifact_root) or not path.is_file():
                raise ValueError("outbound file is outside the isolated AstrBot artifact root")
            if path.stat().st_size > MAX_UPLOAD:
                raise ValueError("outbound media exceeds limit")
            asset = self.store.save_asset(str(data.get("name", path.name)), mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.read_bytes())
        elif isinstance(value, str) and value.startswith(self.asset_base_url + "/api/assets/"):
            asset = self.store.get_asset(value.rsplit("/", 1)[-1])
        if asset is None:
            raise ValueError("outbound media must be a sandbox asset, base64, or file under SANDBOX_ARTIFACT_ROOT")
        url = f"{self.asset_base_url}/api/assets/{asset['id']}"
        data.update({"file": url, "url": url, "file_id": asset["id"], "name": data.get("name", asset["name"])})
        return {"type": typ, "data": data}

    def _fixture_nodes(self) -> list[dict[str, Any]]:
        members = self.store.members()
        first = members[0] if members else {"id": "90001", "nickname": "测试成员 A"}
        second = members[1] if len(members) > 1 else {"id": "90002", "nickname": "测试成员 B"}
        return [
            {"type": "node", "data": {"user_id": int(first["id"]), "nickname": first["nickname"],
                                         "content": [{"type": "text", "data": {"text": "这是一条合并转发测试消息。"}}]}},
            {"type": "node", "data": {"user_id": int(second["id"]), "nickname": second["nickname"],
                                         "content": [{"type": "face", "data": {"id": "178"}},
                                                     {"type": "text", "data": {"text": " 转发节点"}}]}},
        ]

    def _find_conversation(self, conv_type: str, ident: Any) -> dict[str, str] | None:
        if ident is None:
            return None
        return next((c for c in self.store.conversations() if c["type"] == conv_type and c["id"] == str(ident)), None)

    @staticmethod
    def _member_info(member: dict[str, str], group: dict[str, str]) -> dict[str, Any]:
        return {"group_id": int(group["id"]), "user_id": int(member["id"]),
                "nickname": member["nickname"], "card": member["card"], "sex": "unknown", "age": 0,
                "area": "", "join_time": 0, "last_sent_time": 0, "level": "1", "role": "member",
                "unfriendly": False, "title": "", "title_expire_time": 0, "card_changeable": True}


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json(loads=json.loads)
    except Exception:
        raise web.HTTPBadRequest(text="request body must be JSON")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="JSON body must be an object")
    return payload


def _required_text(payload: dict[str, Any], key: str, max_len: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise web.HTTPBadRequest(text=f"{key} must be non-empty text (max {max_len})")
    return value.strip()


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _serve(args: argparse.Namespace) -> None:
    service = SandboxApp(args.data_dir, args.onebot_url, args.token, args.bind, args.port,
                         args.asset_base_url, args.fault_delay)
    await service.start()
    LOG.info("sandbox UI/API listening on http://%s:%s", args.bind, args.port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--onebot-url", required=True, help="isolated AstrBot reverse-WS endpoint; loopback only")
    parser.add_argument("--token", default=None, help="AstrBot reverse-WebSocket token; kept in memory only")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6221)
    parser.add_argument("--asset-base-url", default=None)
    parser.add_argument("--fault-delay", type=float, default=181.0,
                        help="seconds to delay the next API response for timeout injection (OneBot API timeout is 180s)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    validate_loopback_url(args.onebot_url)
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
