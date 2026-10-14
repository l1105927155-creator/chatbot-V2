"""V2 adapter for pinned DSH's native ACP create/resume/prompt contract."""
from __future__ import annotations
import os
import asyncio
import threading
import stat
import copy
from pathlib import Path
from typing import Any
try:  # Support both AstrBot package loading and the isolated contract tests.
    from .turn_source import TurnSourcePublisher
    from .persona_state import PersonaStateStore
    from .scene_state import SceneStateStore
except ImportError:  # pragma: no cover - exercised by tests loading this file directly
    from turn_source import TurnSourcePublisher
    from persona_state import PersonaStateStore
    from scene_state import SceneStateStore


class AcpRuntime:
    """Use the pinned Python transport with DSH's native ACP methods."""
    def __init__(self, *, dsh_bin, dsh_home, workspace, patch, instructions, provider, model,
                 source_dir=None, source_epoch=None, scope_root=None):
        from deepseek_harness.client import HarnessClient, HarnessConfig
        from pydantic import BaseModel, ConfigDict

        class ObjectResponse(BaseModel):
            model_config = ConfigDict(extra="allow")

        self.response_model = ObjectResponse
        self.workspace = str(workspace)
        # Pinned DSH classifies sandbox denials from English OS diagnostics.
        env = {"LC_MESSAGES": "C",
               "DSH_SYSTEM_PROMPT": instructions.read_text(encoding="utf-8"),
               "V2_DSH_PROVIDER": provider, "V2_DSH_MODEL": model}
        if Path(patch).name in {"v2-agent-group.patch.yml", "v2-agent-owner.patch.yml"}:
            env.update({
                "V2_AGENT_SKILLS_DIR": str(Path(patch).resolve().parent.parent / "skills"),
                "V2_PERSONA_STATE_ROOT": str(Path(dsh_home).resolve() / "agent-bot-personas"),
                "V2_SCENE_STATE_ROOT": str(Path(dsh_home).resolve() / "agent-bot-scenes"),
            })
        if Path(patch).name == "v2-agent-owner.patch.yml":
            env["V2_QQ_OWNERS"] = os.environ.get("V2_QQ_OWNERS", "[]")
        if source_dir is not None:
            env.update({"V2_TURN_SOURCE_DIR": str(source_dir),
                        "V2_TURN_SOURCE_EPOCH": source_epoch,
                        "V2_SCOPE_ROOT": str(scope_root)})
        self.client = HarnessClient(HarnessConfig(
            dsh_bin=str(dsh_bin), dsh_home=str(dsh_home), cwd=self.workspace,
            profile="sdk-minimal", patches=(str(patch),),
            env=env,
            request_timeout_seconds=180,
        ))
        try:
            self.client.start()
            initialized = self.request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "chatbot-V2", "version": "0.1.0"},
            })
            if "resume" not in initialized.get("agentCapabilities", {}).get("sessionCapabilities", {}):
                raise RuntimeError("pinned DSH ACP runtime did not advertise session resume")
        except BaseException:
            self.client.close()
            raise

    def request(self, method, params, **kwargs):
        return self.client.request(method, params, response_model=self.response_model,
                                   **kwargs).model_dump()

    def new_session(self, mcp_servers=None):
        return self.request("session/new", {"cwd": self.workspace, "mcpServers": mcp_servers or []})["sessionId"]

    def resume(self, session_id, mcp_servers=None):
        self.request("session/resume", {
            "sessionId": session_id, "cwd": self.workspace, "mcpServers": mcp_servers or [],
        })

    def run(self, session_id, delta):
        chunks = []
        def collect(notification):
            if notification.method != "session/update" or notification.payload.get("sessionId") != session_id:
                return
            update = notification.payload.get("update", {})
            content = update.get("content", {})
            if update.get("sessionUpdate") == "agent_message_chunk" and content.get("type") == "text":
                chunks.append(content["text"])
        result = self.request("session/prompt", {
            "sessionId": session_id, "prompt": [{"type": "text", "text": delta}],
        }, on_notification=collect, notification_filter=lambda n:
           n.method == "session/update" and n.payload.get("sessionId") == session_id)
        if result.get("stopReason") != "end_turn":
            raise RuntimeError(f"DSH turn did not complete: {result.get('stopReason')}")
        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError("DSH completed without a text reply")
        return text

    def close_session(self, session_id):
        self.request("session/close", {"sessionId": session_id})

    def close(self):
        self.client.close()


def _prepare_owner_directory(path: Path) -> None:
    # The host must never give an operation session a world-readable home or
    # a symlinked workspace. Refuse an existing weak directory rather than
    # silently changing its security semantics.
    if path.is_symlink():
        raise ValueError("owner runtime directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or details.st_mode & 0o077):
        raise ValueError("owner runtime directory must belong to this user and be private")


class DshClient:
    """One ACP process with independent, lazily resumed conversation sessions."""
    def __init__(self, *, dsh_bin: Path, dsh_home: Path, workspace: Path,
                 patch: Path, instructions: Path, provider: str | None = None,
                 model: str | None = None, harness_factory: Any = None,
                 enable_authorization: bool = False):
        self.settings = {"dsh_bin": Path(dsh_bin).resolve(), "dsh_home": Path(dsh_home).resolve(),
                         "workspace": Path(workspace).resolve(), "patch": Path(patch).resolve(),
                         "instructions": Path(instructions).resolve()}
        for name, value in (("provider", provider), ("model", model)):
            value = value if value is not None else os.environ.get("V2_DSH_" + name.upper())
            if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
                raise ValueError(f"V2_DSH_{name.upper()} must explicitly select a valid DSH {name}")
            self.settings[name] = value
        for key in ("dsh_bin", "patch", "instructions"):
            if not self.settings[key].is_file():
                raise FileNotFoundError("V2 pinned DSH runtime or profile assets are missing")
        owner_profile = self.settings["patch"].name == "v2-agent-owner.patch.yml"
        if owner_profile:
            if Path(dsh_home).is_symlink() or Path(workspace).is_symlink():
                raise ValueError("owner runtime directory must not be a symlink")
            home, work = self.settings["dsh_home"], self.settings["workspace"]
            if home.is_relative_to(work) or work.is_relative_to(home):
                raise ValueError("owner DSH home and workspace must be disjoint")
            if enable_authorization is not True:
                raise ValueError("owner Profile requires host-issued trusted source authorization")
        for key in ("dsh_home", "workspace"):
            if owner_profile:
                _prepare_owner_directory(Path(dsh_home if key == "dsh_home" else workspace))
            else:
                self.settings[key].mkdir(parents=True, exist_ok=True)
        if not isinstance(enable_authorization, bool):
            raise ValueError("enable_authorization must be a bool")
        self.enable_authorization = enable_authorization
        self.turn_sources = TurnSourcePublisher(self.settings["dsh_home"]) if enable_authorization else None
        self.scope_root = self.settings["dsh_home"] / "v2-conversation-scopes" if enable_authorization else None
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._session_locks = {}
        self._sessions = set()
        self._session_servers = {}
        self._quarantined_sessions = {}
        self._closed = False
        self._runtime_stopped = False
        self._sources_closed = self.turn_sources is None
        self.personas = (PersonaStateStore(self.settings["dsh_home"] / "agent-bot-personas")
                         if self.settings["patch"].name in {"v2-agent-group.patch.yml", "v2-agent-owner.patch.yml"} else None)
        self.scenes = (SceneStateStore(self.settings["dsh_home"] / "agent-bot-scenes")
                       if self.settings["patch"].name in {"v2-agent-group.patch.yml", "v2-agent-owner.patch.yml"} else None)
        # Initialize the shared transport without creating a session or running
        # inference. Provider availability is established by real turns.
        try:
            if self.scope_root is not None:
                self.scope_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.scope_root, 0o700)
                # Only the native profile extension can access this configured root.
                self.settings.update(source_dir=self.turn_sources.directory,
                                     source_epoch=self.turn_sources.epoch,
                                     scope_root=self.scope_root)
            self.runtime = (harness_factory or AcpRuntime)(**self.settings)
        except BaseException as startup_error:
            if self.turn_sources is not None:
                try:
                    self.turn_sources.close()
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "DSH startup and turn source cleanup failed",
                        [startup_error, cleanup_error],
                    )
            raise

    def _session_lock(self, session_id):
        with self._lock:
            if self._closed:
                raise RuntimeError("V2 DSH client is closed")
            return self._session_locks.setdefault(session_id, threading.Lock())

    def create_session(self, mcp_servers=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("V2 DSH client is closed")
        servers = copy.deepcopy(mcp_servers or [])
        session_id = self.runtime.new_session(servers) if servers else self.runtime.new_session()
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("DSH did not return a session ID")
        with self._lock:
            self._sessions.add(session_id)
            self._session_servers[session_id] = servers
        return session_id

    def run(self, session_id, journal_delta, mcp_servers=None, trusted_source=None):
        if not session_id or not journal_delta:
            raise ValueError("session ID and journal delta are required")
        if self.enable_authorization and trusted_source is None:
            raise ValueError("trusted source is required when authorization is enabled")
        if not self.enable_authorization and trusted_source is not None:
            raise ValueError("trusted source requires authorization to be enabled")
        servers = copy.deepcopy(mcp_servers or [])
        with self._session_lock(session_id):
            with self._lock:
                if session_id in self._quarantined_sessions:
                    raise RuntimeError(
                        "DSH session is quarantined after an undrained turn; runtime shutdown is required"
                    ) from self._quarantined_sessions[session_id]
                ready = session_id in self._sessions
                if ready and self._session_servers[session_id] != servers:
                    raise ValueError("MCP binding changed for an active DSH session")
            if not ready:
                # Failure affects only this session; never replace its saved ID
                # or close the shared process used by other conversations.
                if servers:
                    self.runtime.resume(session_id, servers)
                else:
                    self.runtime.resume(session_id)
                with self._lock:
                    self._sessions.add(session_id)
                    self._session_servers[session_id] = servers
            if not self.enable_authorization:
                return self.runtime.run(session_id, journal_delta)
            nonce = self.turn_sources.publish(session_id, trusted_source, journal_delta)
            try:
                result = self.runtime.run(session_id, journal_delta)
            except BaseException:
                # A transport timeout only abandons the Python waiter. The ACP
                # session must be closed to cancel and drain its Agent before
                # the trusted source can disappear.
                try:
                    self.runtime.close_session(session_id)
                except BaseException as drain_error:
                    with self._lock:
                        self._quarantined_sessions[session_id] = drain_error
                    raise RuntimeError(
                        "DSH turn failed and native session drain failed; trusted source retained "
                        "and session quarantined until runtime shutdown"
                    ) from drain_error
                with self._lock:
                    self._sessions.discard(session_id)
                    self._session_servers.pop(session_id, None)
                try:
                    if not self.turn_sources.cleanup(session_id, nonce):
                        raise RuntimeError("trusted source nonce or file did not match during cleanup")
                except BaseException as cleanup_error:
                    with self._lock:
                        self._quarantined_sessions[session_id] = cleanup_error
                    raise RuntimeError(
                        "DSH turn failed and trusted source cleanup failed; session quarantined "
                        "until runtime shutdown"
                    ) from cleanup_error
                raise
            else:
                try:
                    if not self.turn_sources.cleanup(session_id, nonce):
                        raise RuntimeError("trusted source nonce or file did not match during cleanup")
                except BaseException as cleanup_error:
                    with self._lock:
                        self._quarantined_sessions[session_id] = cleanup_error
                    raise RuntimeError(
                        "DSH turn completed but trusted source cleanup failed; session quarantined "
                        "until runtime shutdown"
                    ) from cleanup_error
                return result

    def select_persona(self, session_id: str, persona: str) -> dict:
        """Set the next-turn persona without changing the DSH session or tool set."""
        if self.personas is None:
            raise RuntimeError("persona selection requires the Agent Profile")
        with self._session_lock(session_id):
            return self.personas.select(session_id, persona)

    def select_scene(self, session_id: str, scene: str) -> dict:
        """Set the next-turn skill scene without changing the DSH session or tool set."""
        if self.scenes is None:
            raise RuntimeError("scene selection requires the Agent Profile")
        with self._session_lock(session_id):
            return self.scenes.select(session_id, scene)

    def close_session(self, session_id):
        with self._session_lock(session_id):
            self.runtime.close_session(session_id)
            with self._lock:
                self._sessions.discard(session_id)
                self._session_servers.pop(session_id, None)

    def close(self):
        # Router shutdown drains admitted turns before calling this lifecycle
        # operation. Closing a single session never calls this process shutdown.
        with self._close_lock:
            with self._lock:
                self._closed = True
                entries = tuple(self._sessions)
                runtime_stopped = self._runtime_stopped
            failures = []
            for session_id in entries:
                try:
                    self.runtime.close_session(session_id)
                except Exception as exc:
                    failures.append(exc)
                else:
                    with self._lock:
                        self._sessions.discard(session_id)
                        self._session_servers.pop(session_id, None)
            if not runtime_stopped:
                try:
                    self.runtime.close()
                except Exception as exc:
                    failures.append(exc)
                else:
                    with self._lock:
                        self._runtime_stopped = True
                        self._sessions.clear()
                        self._session_servers.clear()
            with self._lock:
                runtime_stopped = self._runtime_stopped
            if runtime_stopped and self.turn_sources is not None and not self._sources_closed:
                try:
                    self.turn_sources.close()
                except Exception as exc:
                    failures.append(exc)
                else:
                    self._sources_closed = True
            if failures:
                raise ExceptionGroup("DSH runtime shutdown failed", failures)


async def start_client(**settings):
    """Drain an uncancellable startup thread and release its client on cancel."""
    starting = asyncio.create_task(asyncio.to_thread(DshClient, **settings))
    try:
        return await asyncio.shield(starting)
    except asyncio.CancelledError:
        while not starting.done():
            try:
                await asyncio.shield(starting)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not starting.cancelled() and starting.exception() is None:
            closing = asyncio.create_task(asyncio.to_thread(starting.result().close))
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not closing.cancelled():
                closing.result()
        raise
