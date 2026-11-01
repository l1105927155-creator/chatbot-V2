"""Local-only public web app for the game hub.

The app binds 127.0.0.1 and is exposed by Tailscale Serve/Funnel. It is
deliberately separate from AstrBot's authenticated dashboard: player traffic
must never carry a dashboard JWT.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .service import GameError, InvalidReplay, ScoreMismatch, normalize_qq

COOKIE_NAME = "v2_games_player"
COOKIE_MAX_AGE = 90 * 24 * 3600
BODY_LIMIT = 64 * 1024


class _NoSignalUvicornServer(uvicorn.Server):
    """Keep process-wide signal handlers under AstrBot's control."""

    def capture_signals(self):
        import contextlib
        return contextlib.nullcontext()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class PlayerCookie:
    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def issue(self, *, group_id: str, qq: str) -> str:
        payload = json.dumps({"g": group_id, "q": qq,
                              "exp": int(time.time()) + COOKIE_MAX_AGE},
                             separators=(",", ":")).encode()
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return _b64encode(payload) + "." + _b64encode(signature)

    def read(self, value: str | None) -> tuple[str, str] | None:
        if not value or "." not in value:
            return None
        payload_part, signature_part = value.split(".", 1)
        try:
            payload = _b64decode(payload_part)
            signature = _b64decode(signature_part)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        group_id = data.get("g")
        qq = data.get("q")
        exp = data.get("exp")
        if not isinstance(group_id, str) or not isinstance(qq, str):
            return None
        if type(exp) is not int or exp < int(time.time()):
            return None
        try:
            normalize_qq(qq)
        except GameError:
            return None
        return group_id, qq


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}

    def allow(self, action: str, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault((action, key), deque())
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(self._hits) > 5000:
            stale = [name for name, rows in self._hits.items()
                     if not rows or rows[-1] <= now - 3600]
            for name in stale:
                self._hits.pop(name, None)
        return True


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first[:64]
    client = request.client
    return (client.host if client else "unknown")[:64]


class GameWebApp:
    def __init__(self, service: Any, *, secret: bytes, web_dir: Path) -> None:
        self.service = service
        self.cookies = PlayerCookie(secret)
        self.web_dir = web_dir
        self.limiter = RateLimiter()

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _json(data: Any, status: int = 200,
              headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers=headers)

    @staticmethod
    def _error(message: str, status: int, code: str) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    def _secure_cookie(self) -> bool:
        base = getattr(self.service, "public_base_url", "")
        return str(base).startswith("https://")

    def _set_cookie(self, response: Response, *, group_id: str, qq: str) -> None:
        response.set_cookie(
            COOKIE_NAME, self.cookies.issue(group_id=group_id, qq=qq),
            max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
            secure=self._secure_cookie(), path="/",
        )

    def _clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/",
                               secure=self._secure_cookie(), httponly=True,
                               samesite="lax")

    def _identity(self, request: Request, group_id: str) -> tuple[str, str] | None:
        value = request.cookies.get(COOKIE_NAME)
        resolved = self.cookies.read(value)
        if not resolved:
            return None
        cookie_group, qq = resolved
        if cookie_group != group_id:
            return None
        return cookie_group, qq

    async def _group(self, request: Request) -> str:
        token = request.path_params.get("token", "")
        return await asyncio.to_thread(self.service.resolve_group_token, token)

    async def _body(self, request: Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type:
            raise GameError("请求格式无效。", code="invalid_content_type")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > BODY_LIMIT:
                    raise GameError("请求内容过大。", code="body_too_large")
            except ValueError:
                raise GameError("请求格式无效。", code="invalid_content_length")
        raw = await request.body()
        if len(raw) > BODY_LIMIT:
            raise GameError("请求内容过大。", code="body_too_large")
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise GameError("请求格式无效。", code="invalid_json")
        if not isinstance(data, dict):
            raise GameError("请求格式无效。", code="invalid_json")
        return data

    def _origin_ok(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin:
            return True
        if origin == "null":
            return False
        try:
            origin_host = urlsplit(origin).netloc
        except ValueError:
            return False
        host = request.headers.get("host", "")
        allowed = {host} if host else set()
        base = urlsplit(self.service.public_base_url)
        if base.netloc:
            allowed.add(base.netloc)
        return bool(origin_host) and origin_host in allowed

    # ---- pages --------------------------------------------------------

    async def landing(self, request: Request) -> Response:
        return FileResponse(self.web_dir / "index.html",
                            headers={"Cache-Control": "no-store"})

    async def hub(self, request: Request) -> Response:
        try:
            await self._group(request)
        except GameError:
            return HTMLResponse(self._not_found_html(), status_code=404)
        return FileResponse(self.web_dir / "index.html",
                            headers={"Cache-Control": "no-store"})

    async def game_page(self, request: Request) -> Response:
        try:
            await self._group(request)
        except GameError:
            return HTMLResponse(self._not_found_html(), status_code=404)
        return FileResponse(self.web_dir / "game2048.html",
                            headers={"Cache-Control": "no-store"})

    @staticmethod
    def _not_found_html() -> str:
        return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
                "<title>链接无效</title></head><body style='font-family:system-ui;"
                "padding:40px;background:#10101a;color:#e2e2ee'>"
                "<h1>游戏链接无效或已失效</h1><p>请回到 QQ 群重新发送 /games 获取链接。</p>"
                "</body></html>")

    # ---- API ----------------------------------------------------------

    async def health(self, request: Request) -> JSONResponse:
        return self._json({"status": "ok", "web": True})

    async def games(self, request: Request) -> Response:
        return self._json({"games": await asyncio.to_thread(self.service.list_games)})

    async def meta(self, request: Request) -> Response:
        try:
            group_id = await self._group(request)
        except GameError as error:
            return self._error(error.public_message, 404, error.code)
        identity = self._identity(request, group_id)
        games = await asyncio.to_thread(self.service.list_games)
        return self._json({
            "games": games,
            "identity": {"qq_masked": self._mask(identity[1])} if identity else None,
            "public_base_url": self.service.public_base_url,
        })

    @staticmethod
    def _mask(qq: str) -> str:
        from .service import mask_qq
        return mask_qq(qq)

    async def identity(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return self._error("请求来源无效。", 403, "invalid_origin")
        if not self.limiter.allow("identity", _client_key(request), 10, 60):
            return self._error("操作过于频繁，请稍后再试。", 429, "rate_limited")
        try:
            group_id = await self._group(request)
            data = await self._body(request)
            qq = normalize_qq(data.get("qq"))
            me = await asyncio.to_thread(self.service.me, group_id=group_id, qq=qq)
        except GameError as error:
            return self._error(error.public_message, 400, error.code)
        response = self._json({"ok": True, "me": me})
        self._set_cookie(response, group_id=group_id, qq=qq)
        return response

    async def me(self, request: Request) -> Response:
        try:
            group_id = await self._group(request)
        except GameError as error:
            return self._error(error.public_message, 404, error.code)
        identity = self._identity(request, group_id)
        if not identity:
            return self._error("请先填写 QQ 号。", 401, "identity_required")
        try:
            data = await asyncio.to_thread(
                self.service.me, group_id=group_id, qq=identity[1])
        except GameError as error:
            return self._error(error.public_message, 400, error.code)
        return self._json(data)

    async def create_run(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return self._error("请求来源无效。", 403, "invalid_origin")
        try:
            group_id = await self._group(request)
        except GameError as error:
            return self._error(error.public_message, 404, error.code)
        identity = self._identity(request, group_id)
        if not identity:
            return self._error("请先填写 QQ 号。", 401, "identity_required")
        if not self.limiter.allow("runs", f"{group_id}:{identity[1]}", 6, 60):
            return self._error("开局过于频繁，请稍后再试。", 429, "rate_limited")
        try:
            run = await asyncio.to_thread(
                self.service.create_web_run, group_id=group_id, qq=identity[1])
        except GameError as error:
            return self._error(error.public_message, 400, error.code)
        return self._json(run)

    async def submit_run(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return self._error("请求来源无效。", 403, "invalid_origin")
        try:
            group_id = await self._group(request)
        except GameError as error:
            return self._error(error.public_message, 404, error.code)
        identity = self._identity(request, group_id)
        if not identity:
            return self._error("请先填写 QQ 号。", 401, "identity_required")
        if not self.limiter.allow("submit", f"{group_id}:{identity[1]}", 12, 60):
            return self._error("保存过于频繁，请稍后再试。", 429, "rate_limited")
        run_id = request.path_params.get("run_id", "")
        if not run_id or len(run_id) > 96:
            return self._error("对局不存在。", 404, "run_not_found")
        try:
            data = await self._body(request)
            result = await asyncio.to_thread(
                self.service.submit_web_run, run_id=run_id, group_id=group_id,
                qq=identity[1], moves=data.get("moves"),
                client_score=data.get("client_score"),
                max_tile=data.get("max_tile"),
                duration_ms=data.get("duration_ms"),
            )
        except ScoreMismatch as error:
            return self._error(error.public_message, 409, error.code)
        except InvalidReplay as error:
            return self._error(error.public_message, 422, error.code)
        except GameError as error:
            status = 404 if error.code == "run_not_found" else 403 if error.code == "run_forbidden" else 409 if error.code == "run_closed" else 400
            return self._error(error.public_message, status, error.code)
        return self._json({"ok": True, "result": result})

    async def abandon_run(self, request: Request) -> Response:
        if not self._origin_ok(request):
            return self._error("请求来源无效。", 403, "invalid_origin")
        try:
            group_id = await self._group(request)
        except GameError as error:
            return self._error(error.public_message, 404, error.code)
        identity = self._identity(request, group_id)
        if not identity:
            return self._error("请先填写 QQ 号。", 401, "identity_required")
        run_id = request.path_params.get("run_id", "")
        try:
            done = await asyncio.to_thread(
                self.service.abandon_web_run, run_id=run_id,
                group_id=group_id, qq=identity[1])
        except GameError as error:
            return self._error(error.public_message, 400, error.code)
        return self._json({"ok": bool(done)})

    async def leaderboard(self, request: Request) -> Response:
        try:
            group_id = await self._group(request)
        except GameError as error:
            return self._error(error.public_message, 404, error.code)
        identity = self._identity(request, group_id)
        period = request.query_params.get("period", "all")
        kind = request.query_params.get("kind", "human")
        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            limit = 20
        try:
            board = await asyncio.to_thread(
                self.service.leaderboard, group_id=group_id, period=period,
                kind=kind, limit=limit,
                viewer_key=(f"group:{group_id}:qq:{identity[1]}" if identity else None))
        except GameError as error:
            return self._error(error.public_message, 400, error.code)
        return self._json(board)

    # ---- app ----------------------------------------------------------

    def app(self) -> Starlette:
        routes = [
            Route("/", self.landing),
            Route("/g/{token}", self.hub),
            Route("/g/{token}/2048", self.game_page),
            Route("/api/g/{token}/meta", self.meta),
            Route("/api/games", self.games),
            Route("/api/g/{token}/identity", self.identity, methods=["POST"]),
            Route("/api/g/{token}/me", self.me),
            Route("/api/g/{token}/runs", self.create_run, methods=["POST"]),
            Route("/api/g/{token}/runs/{run_id}/submit", self.submit_run, methods=["POST"]),
            Route("/api/g/{token}/runs/{run_id}/abandon", self.abandon_run, methods=["POST"]),
            Route("/api/g/{token}/leaderboard", self.leaderboard),
            Route("/healthz", self.health),
            Mount("/assets", StaticFiles(directory=str(self.web_dir / "assets"))),
        ]
        app = Starlette(routes=routes)
        app.add_middleware(SecurityHeadersMiddleware)
        return app


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"content-security-policy",
                     b"default-src 'self'; img-src 'self' data:; "
                     b"style-src 'self' 'unsafe-inline'; script-src 'self'; "
                     b"connect-src 'self'; frame-ancestors 'none'"),
                ])
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


class GameWebServer:
    def __init__(self, app, *, port: int, host: str = "127.0.0.1",
                 logger: Any = None) -> None:
        self.app = app
        self._host = host
        self._task: asyncio.Task | None = None
        self._server = None
        self._socket = None
        self._shutdown: asyncio.Task | None = None
        self._port = port
        self.logger = logger

    @property
    def port(self) -> int | None:
        return self._socket.getsockname()[1] if self._socket else None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("game web server already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.listen(128)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            self._socket = None
            raise
        config = uvicorn.Config(
            self.app, host=self._host, port=sock.getsockname()[1],
            log_config=None, access_log=False, lifespan="on",
        )
        self._server = _NoSignalUvicornServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]),
                                         name="v2-games-web")
        try:
            for _ in range(500):
                if self._server.started:
                    return
                if self._task.done():
                    await self._task
                    raise RuntimeError("game web server exited during startup")
                await asyncio.sleep(0.01)
            raise TimeoutError("game web server did not start within 5 seconds")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._shutdown is None:
            server, serve_task, sock = self._server, self._task, self._socket
            if server is not None:
                server.should_exit = True
            self._shutdown = asyncio.create_task(
                self._finish_close(server, serve_task, sock),
                name="v2-games-web-shutdown")
        await asyncio.shield(self._shutdown)

    async def _finish_close(self, server, serve_task, sock) -> None:
        failure: BaseException | None = None
        try:
            if serve_task is not None:
                try:
                    await asyncio.wait_for(serve_task, timeout=10)
                except asyncio.TimeoutError:
                    if server is not None:
                        server.force_exit = True
                    serve_task.cancel()
                    import contextlib
                    with contextlib.suppress(asyncio.CancelledError):
                        await serve_task
        except BaseException as exc:
            failure = exc
        finally:
            if sock is not None:
                sock.close()
            self._task = None
            self._server = None
            self._socket = None
            self._shutdown = None
        if failure is not None:
            raise failure


__all__ = ["GameWebApp", "GameWebServer", "PlayerCookie", "SecurityHeadersMiddleware"]
