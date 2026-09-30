"""V2 adapter for pinned DSH's native ACP create/resume/prompt contract."""
from __future__ import annotations
import os
import asyncio
import threading
from pathlib import Path
from typing import Any


class AcpRuntime:
    """Use the pinned Python transport with DSH's native ACP methods."""
    def __init__(self, *, dsh_bin, dsh_home, workspace, patch, instructions, provider, model):
        from deepseek_harness.client import HarnessClient, HarnessConfig
        from pydantic import BaseModel, ConfigDict

        class ObjectResponse(BaseModel):
            model_config = ConfigDict(extra="allow")

        self.response_model = ObjectResponse
        self.workspace = str(workspace)
        self.client = HarnessClient(HarnessConfig(
            dsh_bin=str(dsh_bin), dsh_home=str(dsh_home), cwd=self.workspace,
            profile="sdk-minimal", patches=(str(patch),),
            env={"DSH_SYSTEM_PROMPT": instructions.read_text(encoding="utf-8"),
                 "V2_DSH_PROVIDER": provider, "V2_DSH_MODEL": model},
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

    def new_session(self):
        return self.request("session/new", {"cwd": self.workspace, "mcpServers": []})["sessionId"]

    def resume(self, session_id):
        self.request("session/resume", {
            "sessionId": session_id, "cwd": self.workspace, "mcpServers": [],
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


class DshClient:
    """One ACP process with independent, lazily resumed conversation sessions."""
    def __init__(self, *, dsh_bin: Path, dsh_home: Path, workspace: Path,
                 patch: Path, instructions: Path, provider: str | None = None,
                 model: str | None = None, harness_factory: Any = None):
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
        for key in ("dsh_home", "workspace"):
            self.settings[key].mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._session_locks = {}
        self._sessions = set()
        self._closed = False
        # Initialize the shared transport without creating a session or running
        # inference. Provider availability is established by real turns.
        self.runtime = (harness_factory or AcpRuntime)(**self.settings)

    def _session_lock(self, session_id):
        with self._lock:
            if self._closed:
                raise RuntimeError("V2 DSH client is closed")
            return self._session_locks.setdefault(session_id, threading.Lock())

    def create_session(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("V2 DSH client is closed")
        session_id = self.runtime.new_session()
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("DSH did not return a session ID")
        with self._lock:
            self._sessions.add(session_id)
        return session_id

    def run(self, session_id, journal_delta):
        if not session_id or not journal_delta:
            raise ValueError("session ID and journal delta are required")
        with self._session_lock(session_id):
            with self._lock:
                ready = session_id in self._sessions
            if not ready:
                # Failure affects only this session; never replace its saved ID
                # or close the shared process used by other conversations.
                self.runtime.resume(session_id)
                with self._lock:
                    self._sessions.add(session_id)
            return self.runtime.run(session_id, journal_delta)

    def close_session(self, session_id):
        with self._session_lock(session_id):
            self.runtime.close_session(session_id)
            with self._lock:
                self._sessions.discard(session_id)

    def close(self):
        # Router shutdown drains admitted turns before calling this lifecycle
        # operation. Closing a single session never calls this process shutdown.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._sessions)
            self._sessions.clear()
        failures = []
        for session_id in entries:
            try:
                self.runtime.close_session(session_id)
            except Exception as exc:
                failures.append(exc)
        try:
            self.runtime.close()
        except Exception as exc:
            failures.append(exc)
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
