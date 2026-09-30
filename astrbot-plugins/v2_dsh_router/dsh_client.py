"""V2 adapter for pinned DSH's native ACP create/resume/prompt contract."""
from __future__ import annotations
import os
import threading
from pathlib import Path
from typing import Any


class AcpRuntime:
    """Use the pinned Python transport with DSH's native ACP methods."""
    def __init__(self, *, dsh_bin, dsh_home, workspace, patch, instructions):
        from deepseek_harness.client import HarnessClient, HarnessConfig
        from pydantic import BaseModel, ConfigDict

        class ObjectResponse(BaseModel):
            model_config = ConfigDict(extra="allow")

        self.response_model = ObjectResponse
        self.workspace = str(workspace)
        self.client = HarnessClient(HarnessConfig(
            dsh_bin=str(dsh_bin), dsh_home=str(dsh_home), cwd=self.workspace,
            profile="sdk-minimal", patches=(str(patch),),
            env={"DSH_SYSTEM_PROMPT": instructions.read_text(encoding="utf-8")},
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
        }, on_notification=collect)
        if result.get("stopReason") != "end_turn":
            raise RuntimeError(f"DSH turn did not complete: {result.get('stopReason')}")
        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError("DSH completed without a text reply")
        return text

    def close(self, session_id=None):
        try:
            if session_id:
                self.request("session/close", {"sessionId": session_id})
        finally:
            self.client.close()


class DshClient:
    """Keep one isolated ACP process per mapped QQ conversation."""
    def __init__(self, *, dsh_bin: Path, dsh_home: Path, workspace: Path,
                 patch: Path, instructions: Path, harness_factory: Any = None):
        self.settings = {"dsh_bin": Path(dsh_bin).resolve(), "dsh_home": Path(dsh_home).resolve(),
                         "workspace": Path(workspace).resolve(), "patch": Path(patch).resolve(),
                         "instructions": Path(instructions).resolve()}
        for key in ("dsh_bin", "patch", "instructions"):
            if not self.settings[key].is_file():
                raise FileNotFoundError("V2 pinned DSH runtime or profile assets are missing")
        if not os.environ.get("DEEPSEEK_API_KEY") and harness_factory is None:
            raise RuntimeError("DEEPSEEK_API_KEY is required for the V2 DSH runtime")
        for key in ("dsh_home", "workspace"):
            self.settings[key].mkdir(parents=True, exist_ok=True)
        self.factory = harness_factory or AcpRuntime
        self._harnesses = {}
        self._lock = threading.Lock()
        self._session_locks = {}

    def create_session(self):
        runtime = self.factory(**self.settings)
        try:
            session_id = runtime.new_session()
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("DSH did not return a session ID")
            with self._lock:
                self._harnesses[session_id] = runtime
            return session_id
        except BaseException:
            runtime.close()
            raise

    def run(self, session_id, journal_delta):
        if not session_id or not journal_delta:
            raise ValueError("session ID and journal delta are required")
        with self._lock:
            session_lock = self._session_locks.setdefault(session_id, threading.Lock())
        with session_lock:
            with self._lock:
                runtime = self._harnesses.get(session_id)
            if runtime is None:
                runtime = self.factory(**self.settings)
                try:
                    runtime.resume(session_id)
                except BaseException:
                    runtime.close()
                    raise
                with self._lock:
                    self._harnesses[session_id] = runtime
        return runtime.run(session_id, journal_delta)

    def close(self):
        with self._lock:
            entries = tuple(self._harnesses.items())
            self._harnesses.clear()
        failures = []
        for session_id, runtime in entries:
            try:
                runtime.close(session_id)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("DSH runtime shutdown failed", failures)
