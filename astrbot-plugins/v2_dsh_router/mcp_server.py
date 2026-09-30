"""Conversation-scoped MCP tools hosted by the V2 AstrBot plugin."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, TypedDict

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError


@dataclass
class _Turn:
    conversation: Any
    send: Callable[[str], Awaitable[Any]]
    get_group: Callable[[], Awaitable[Any]] | None
    token: str


class JournalEntry(TypedDict):
    journal_id: int
    direction: str
    actor_id: str
    actor_name: str | None
    source: str
    message_id: str | None
    reply_to: str | None
    content: dict[str, Any]
    created_at: int | None


class HistoryResult(TypedDict):
    entries: list[JournalEntry]


class GroupInfo(TypedDict):
    applicable: bool
    partial: bool
    id: str | None
    name: str | None
    member_count: int | None
    reason: str | None


class SendSubmission(TypedDict):
    submitted: bool
    platform_confirmed: bool


class _AstrBotUvicornServer(uvicorn.Server):
    """Leave process-wide signal handlers under AstrBot's control."""

    def capture_signals(self):
        return contextlib.nullcontext()


class _BearerAuth:
    """Reject every unauthenticated HTTP request before MCP dispatch."""

    def __init__(self, app, server: "McpServer") -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin1")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not token or token not in self.server._active_tokens:
            body = b'{"error":"unauthorized"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class McpServer:
    """A loopback Streamable HTTP endpoint with turn-scoped authority."""

    def __init__(self, journal: Any, logger: logging.Logger | None = None) -> None:
        self.journal = journal
        self.logger = logger or logging.getLogger(__name__)
        self._tokens: dict[tuple[str, str, str], str] = {}
        self._active_tokens: dict[str, _Turn] = {}
        self._active_conversations: set[tuple[str, str, str]] = set()
        self._lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._socket: socket.socket | None = None
        self._url: str | None = None
        self._mcp = self._create_mcp()

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("MCP server has not started")
        return self._url

    @staticmethod
    def _identity(conversation: Any) -> tuple[str, str, str]:
        return (str(conversation.platform_id), str(conversation.bot_id), str(conversation.key))

    def _create_mcp(self) -> FastMCP:
        server = FastMCP(
            name="AstrBot",
            instructions=("Tools are scoped to the authenticated QQ conversation for this turn. "
                          "History tools read only that conversation. qq_send_origin sends only "
                          "to the current origin conversation."),
            json_response=True,
            stateless_http=True,
            streamable_http_path="/mcp",
            host="127.0.0.1",
        )

        @server.tool(structured_output=True)
        async def history_recent(limit: int = 50, ctx: Context | None = None) -> HistoryResult:
            """Read the newest canonical journal entries from this conversation."""
            turn = self._turn(ctx)
            self._validate_limit(limit)
            rows = await asyncio.to_thread(self.journal.recent, turn.conversation, limit)
            return {"entries": [self._entry(row) for row in rows]}

        @server.tool(structured_output=True)
        async def history_before(before_id: int, limit: int = 100,
                                 ctx: Context | None = None) -> HistoryResult:
            """Read journal entries before an exclusive positive journal ID."""
            turn = self._turn(ctx)
            self._validate_limit(limit)
            self._validate_id(before_id, "before_id")
            rows = await asyncio.to_thread(self.journal.before, turn.conversation, before_id, limit)
            return {"entries": [self._entry(row) for row in rows]}

        @server.tool(structured_output=True)
        async def history_search(query: str, before_id: int | None = None,
                                 limit: int = 100,
                                 ctx: Context | None = None) -> HistoryResult:
            """Search normalized message text within this conversation only."""
            turn = self._turn(ctx)
            self._validate_limit(limit)
            if not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ToolError("query must contain 1 to 200 characters")
            if before_id is not None:
                self._validate_id(before_id, "before_id")
            rows = await asyncio.to_thread(
                self.journal.search, turn.conversation, query,
                before_id=before_id, limit=limit,
            )
            return {"entries": [self._entry(row) for row in rows]}

        @server.tool(structured_output=True)
        async def current_group_info(ctx: Context | None = None) -> GroupInfo:
            """Read current group metadata when the origin is a group conversation."""
            turn = self._turn(ctx)
            if not turn.conversation.key.startswith("group:"):
                return {"applicable": False, "partial": False, "id": None,
                        "name": None, "member_count": None,
                        "reason": "origin is a private conversation"}
            if turn.get_group is None:
                return {"applicable": False, "partial": True,
                        "id": turn.conversation.key.removeprefix("group:"),
                        "name": None, "member_count": None,
                        "reason": "group metadata callback is unavailable"}
            result = await turn.get_group()
            if result is None:
                return {"applicable": False, "partial": True,
                        "id": turn.conversation.key.removeprefix("group:"),
                        "name": None, "member_count": None,
                        "reason": "AstrBot returned no group metadata"}
            if isinstance(result, dict):
                data = result
            else:
                data = {
                    "name": getattr(result, "group_name", None),
                    "member_count": getattr(result, "member_count", None),
                }
            name = data.get("name", data.get("group_name"))
            member_count = data.get("member_count")
            return {
                "applicable": True,
                "partial": bool(data.get("partial", False) or name is None or member_count is None),
                "id": turn.conversation.key.removeprefix("group:"),
                "name": name,
                "member_count": member_count,
                "reason": None,
            }

        @server.tool(structured_output=True)
        async def qq_send_origin(text: str, ctx: Context | None = None) -> SendSubmission:
            """Send plain text through AstrBot to this turn's origin conversation."""
            turn = self._turn(ctx)
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise ToolError("text must contain 1 to 4000 characters")
            try:
                await turn.send(text)
            except Exception as exc:
                self.logger.exception("AstrBot origin send failed")
                raise ToolError(f"AstrBot send failed: {type(exc).__name__}") from exc
            # AstrBot sends asynchronously; platform confirmation and the real
            # message ID come later through the canonical journal capture path.
            return {"submitted": True, "platform_confirmed": False}

        return server

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ToolError("limit must be an integer from 1 to 100")

    @staticmethod
    def _validate_id(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ToolError(f"{name} must be a positive integer")

    @staticmethod
    def _entry(row: Any) -> JournalEntry:
        message = row.message
        return {
            "journal_id": row.journal_id,
            "direction": message.direction,
            "actor_id": message.actor_id,
            "actor_name": message.actor_name,
            "source": message.source,
            "message_id": message.message_id,
            "reply_to": message.reply_to,
            "content": message.content,
            "created_at": message.created_at,
        }

    def _turn(self, ctx: Context | None) -> _Turn:
        if ctx is None:
            raise ToolError("MCP request context is unavailable")
        request = ctx.request_context.request
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        turn = self._active_tokens.get(token)
        if not turn:
            raise ToolError("MCP turn is no longer active")
        return turn

    async def start(self, port: int = 6210) -> None:
        if self._task is not None:
            raise RuntimeError("MCP server already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            self._socket = None
            raise
        actual_port = sock.getsockname()[1]
        app = _BearerAuth(self._mcp.streamable_http_app(), self)
        config = uvicorn.Config(app, host="127.0.0.1", port=actual_port,
                                log_config=None, access_log=False, lifespan="on")
        self._server = _AstrBotUvicornServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]),
                                         name="v2-mcp-uvicorn")
        try:
            for _ in range(500):
                if self._server.started:
                    self._url = f"http://127.0.0.1:{actual_port}/mcp"
                    return
                if self._task.done():
                    await self._task
                    raise RuntimeError("MCP HTTP server exited during startup")
                await asyncio.sleep(0.01)
            raise TimeoutError("MCP HTTP server did not start within 5 seconds")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._shutdown_task is None:
            server, serve_task, sock = self._server, self._task, self._socket
            self._url = None
            if server is not None:
                server.should_exit = True
            self._shutdown_task = asyncio.create_task(
                self._finish_close(server, serve_task, sock), name="v2-mcp-shutdown"
            )
        await asyncio.shield(self._shutdown_task)

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
                    with contextlib.suppress(asyncio.CancelledError):
                        await serve_task
        except BaseException as exc:
            failure = exc
        finally:
            if sock is not None:
                sock.close()
            async with self._lock:
                self._active_tokens.clear()
                self._active_conversations.clear()
            self._task = None
            self._server = None
            self._socket = None
            self._shutdown_task = None
        if failure is not None:
            raise failure

    @asynccontextmanager
    async def turn(self, conversation: Any, send: Callable[[str], Awaitable[Any]],
                   get_group: Callable[[], Awaitable[Any]] | None = None
                   ) -> AsyncIterator[list[dict[str, Any]]]:
        if self._task is None or self._task.done() or self._shutdown_task is not None:
            raise RuntimeError("MCP server is not running")
        identity = self._identity(conversation)
        async with self._lock:
            if identity in self._active_conversations:
                raise RuntimeError("MCP turn is already active for this conversation")
            token = self._tokens.setdefault(identity, secrets.token_urlsafe(32))
            turn = _Turn(conversation, send, get_group, token)
            self._active_conversations.add(identity)
            self._active_tokens[token] = turn
        try:
            yield [{"type": "http", "name": "astrbot", "url": self.url,
                    "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}]
        finally:
            async with self._lock:
                self._active_tokens.pop(token, None)
                self._active_conversations.discard(identity)
