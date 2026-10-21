"""V2 adapter for pinned DSH's native ACP create/resume/prompt contract."""
from __future__ import annotations
import os
from collections import Counter
import re
import asyncio
import threading
import stat
import copy
import time
import json
import inspect
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any

# The pinned fetch provider skips DNS validation on proxy routes. Keep both
# Agent Profiles on direct transport so its public-address/pinning checks run.
DIRECT_NETWORK_ENV = {name: "" for name in (
    "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
)} | {"NODE_USE_ENV_PROXY": "0"}
try:  # Support both AstrBot package loading and the isolated contract tests.
    from .model_selection import model_route as validate_model_route, model_view
    from .shared_models import SharedModels
    from .turn_source import TurnSourcePublisher
    from .persona_state import PersonaStateStore
    from .scene_state import SceneStateStore
    from .runtime_audit import CURRENT_TRACE, call_id
except ImportError:  # pragma: no cover - exercised by tests loading this file directly
    from model_selection import model_route as validate_model_route, model_view
    from shared_models import SharedModels
    from turn_source import TurnSourcePublisher
    from persona_state import PersonaStateStore
    from scene_state import SceneStateStore
    from runtime_audit import CURRENT_TRACE, call_id


class AcpRuntime:
    """Use the pinned Python transport with DSH's native ACP methods."""
    def __init__(self, *, dsh_bin, dsh_home, workspace, patch, instructions, provider, model,
                 source_dir=None, source_epoch=None, scope_root=None,
                 execution_profile=None, owner_shell_env=None, group_runtime=None,
                 background_callback_url=None, background_callback_token=None):
        from deepseek_harness.client import HarnessClient, HarnessConfig
        from pydantic import BaseModel, ConfigDict

        class ObjectResponse(BaseModel):
            model_config = ConfigDict(extra="allow")

        self.response_model = ObjectResponse
        self.workspace = str(workspace)
        self.model_route = (provider, model)
        self._control_lock = threading.RLock()
        self._active_prompts = {}

        class PromptWriteClient(HarnessClient):
            """Serialize prompt dispatch with cancellation notifications.

            The pinned SDK has no nonblocking request handle. Its narrow
            _write_message hook lets us ensure a latched cancel is written only
            after the prompt line has reached stdin.
            """
            def _write_message(inner_self, message):
                if message.get("method") != "session/prompt":
                    return super(PromptWriteClient, inner_self)._write_message(message)
                session_id = message.get("params", {}).get("sessionId")
                with self._control_lock:
                    token = self._active_prompts.get(session_id)
                    if token is not None and (token["requested"] or (
                            token.get("cancel_event") is not None and token["cancel_event"].is_set())):
                        raise DshTurnCancelled("DSH turn was cancelled before prompt write")
                    super(PromptWriteClient, inner_self)._write_message(message)
                    if token is not None:
                        token["dispatched"] = True
                        if token.get("trace"):
                            token["trace"].emit("dsh/dispatched")
                        if token["requested"]:
                            inner_self.notify("session/cancel", {"sessionId": session_id})

        # Pinned DSH classifies sandbox denials from English OS diagnostics.
        env = {"LC_MESSAGES": "C",
               "DSH_SYSTEM_PROMPT": instructions.read_text(encoding="utf-8"),
               "V2_DSH_PROVIDER": provider, "V2_DSH_MODEL": model,
               "V2_DSH_HOME": str(Path(dsh_home).resolve())}
        self._console_control_dir = None
        if os.environ.get("V2_CONSOLE_STATE_FILE"):
            self._console_control_dir = tempfile.TemporaryDirectory(prefix="v2-control-")
            self._console_control_path = str(Path(self._console_control_dir.name) / "control.sock")
            self._console_control_token = secrets.token_hex(32)
            env.update(V2_CONSOLE_CONTROL_SOCKET=self._console_control_path,
                       V2_CONSOLE_CONTROL_TOKEN=self._console_control_token)
        if background_callback_url is not None or background_callback_token is not None:
            if (not isinstance(background_callback_url, str) or
                    not background_callback_url.startswith("http://127.0.0.1:") or
                    not background_callback_url.endswith("/background/events") or
                    not isinstance(background_callback_token, str) or
                    len(background_callback_token) < 32):
                raise ValueError("background callback endpoint and token must be configured together")
            env.update({"V2_BACKGROUND_CALLBACK_URL": background_callback_url,
                        "V2_BACKGROUND_CALLBACK_TOKEN": background_callback_token})
        self.shared_models = None
        if os.environ.get('V2_SHARED_MODELS_PROFILE'):
            status = Path(dsh_home).resolve() / '.model-catalog-sync.json'
            epoch = secrets.token_hex(16)
            env.update({key: os.environ[key] for key in
                        ('V2_SHARED_MODELS_PROFILE', 'V2_SHARED_MODELS_CREDENTIALS')})
            env.update(V2_SHARED_MODELS_STATUS_FILE=str(status), V2_SHARED_MODELS_EPOCH=epoch)
            self.shared_models = SharedModels(env['V2_SHARED_MODELS_PROFILE'], status, epoch)
        self._catalog_lock = threading.RLock()
        if execution_profile:
            env["V2_EXECUTION_PROFILE"] = execution_profile
        if execution_profile == "owner-host":
            env["V2_OWNER_PROJECT_GUIDE_FILE"] = str(Path(workspace).resolve() / "AGENTS.md")
        if Path(patch).name in {"v2-agent-group.patch.yml", "v2-agent-owner.patch.yml",
                                "v2-agent-group-sandbox.patch.yml"}:
            env.update(DIRECT_NETWORK_ENV)
            env.update({
                "V2_AGENT_SKILLS_DIR": str(Path(patch).resolve().parent.parent / "skills"),
                "V2_AGENT_INSTRUCTIONS_FILE": str(instructions.resolve()),
                "V2_PERSONA_STATE_ROOT": str(Path(dsh_home).resolve() / "agent-bot-personas"),
                "V2_SCENE_STATE_ROOT": str(Path(dsh_home).resolve() / "agent-bot-scenes"),
            })
        if Path(patch).name == "v2-agent-owner.patch.yml":
            env["V2_QQ_OWNERS"] = os.environ.get("V2_QQ_OWNERS", "[]")
            if os.environ.get("V2_CONSOLE_STATE_FILE"):
                env["V2_OWNER_WORKSPACE_LOCK_FILE"] = str(Path(dsh_home) / "workspace-file.lock")
        if owner_shell_env is not None:
            env["V2_OWNER_SHELL_ENV_JSON"] = json.dumps(owner_shell_env, separators=(",", ":"))
        if group_runtime:
            if os.environ.get("V2_CONSOLE_STATE_FILE"):
                env["V2_CONSOLE_STATE_FILE"] = os.environ["V2_CONSOLE_STATE_FILE"]
                env["V2_QQ_OWNERS"] = os.environ.get("V2_QQ_OWNERS", "[]")
            env.update({
                "V2_GROUP_WORKSPACE_ROOT": str(group_runtime["workspaceRoot"]),
                "V2_GROUP_PLATFORM_ID": str(group_runtime["platformId"]),
                "V2_GROUP_BOT_ID": str(group_runtime["botId"]),
                "V2_GROUP_ID": str(group_runtime["groupId"]),
            })
        if source_dir is not None:
            env.update({"V2_TURN_SOURCE_DIR": str(source_dir),
                        "V2_TURN_SOURCE_EPOCH": source_epoch})
            if scope_root is not None:
                env["V2_SCOPE_ROOT"] = str(scope_root)
        # The pinned CLI loads layered .env files from cwd before starting the
        # isolated child runtime. Group workspaces are untrusted, so launch the
        # outer DSH process from its private home; ACP sessions still receive
        # the verified group workspace as cwd in new_session/resume below.
        launch_cwd = str(dsh_home) if execution_profile == "group-sandbox" else self.workspace
        self.client = PromptWriteClient(HarnessConfig(
            dsh_bin=str(dsh_bin), dsh_home=str(dsh_home), cwd=launch_cwd,
            profile="sdk-minimal", patches=(str(patch),),
            env=env,
            # Owner commands can run up to 600 s in the pinned native Bash.
            # None lets ACP use its native per-request timeout semantics.
            request_timeout_seconds=None if execution_profile == "owner-host" else 180,
        ))
        self._model_notifications = (self.client.subscribe_notifications(
            lambda n: n.method == 'session/update' and
            (n.payload or {}).get('update', {}).get('sessionUpdate') == 'config_option_update'
        ) if self.shared_models is not None else None)
        try:
            self.client.start()
            initialized = self.request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "chatbot-V2", "version": "0.1.0"},
            })
            if "resume" not in initialized.get("agentCapabilities", {}).get("sessionCapabilities", {}):
                raise RuntimeError("pinned DSH ACP runtime did not advertise session resume")
            self.image_prompts = (initialized.get("agentCapabilities", {})
                                  .get("promptCapabilities", {}).get("image") is True)
            if self.shared_models is not None:
                self.shared_models.snapshot()
        except BaseException:
            self.client.close()
            raise

    def request(self, method, params, **kwargs):
        return self.client.request(method, params, response_model=self.response_model,
                                   **kwargs).model_dump()

    def _remember_models(self, session_id, options):
        self.__dict__.setdefault('_session_model_configs', {})[session_id] = options
        return options

    def new_session(self, mcp_servers=None):
        result = self.request('session/new', {'cwd': self.workspace, 'mcpServers': mcp_servers or []})
        self._remember_models(result['sessionId'], result.get('configOptions', []))
        return result['sessionId']

    def _set_native_model(self, session_id, value):
        uncertain = self.__dict__.setdefault('_uncertain_model_sessions', set())
        uncertain.add(session_id)
        expected = validate_model_route(json.loads(value))
        options = self.request('session/set_config_option', {
            'sessionId': session_id, 'configId': 'model', 'value': value,
        }).get('configOptions', [])
        view = model_view(options)
        if (view['current']['provider'], view['current']['model']) != expected:
            raise RuntimeError('DSH model switch was not confirmed')
        self._remember_models(session_id, options)
        uncertain.discard(session_id)
        return options

    def _apply_model_route(self, session_id, options, route):
        if not route:
            return options
        option = next((item for item in options if item.get('id') == 'model'), None)
        if option is None:
            raise RuntimeError('DSH did not expose the deployment model configuration')
        def matches(value):
            try:
                return json.loads(value) == list(route)
            except (TypeError, ValueError):
                return False
        if (matches(option.get('currentValue'))
                and session_id not in self.__dict__.get('_uncertain_model_sessions', set())):
            return options
        choices = []
        for entry in option.get('options', []):
            choices.extend(entry.get('options', []) if 'group' in entry else [entry])
        selected = next((item.get('value') for item in choices if matches(item.get('value'))), None)
        if selected is None:
            raise RuntimeError('configured DSH model is not available in the native ACP catalog')
        updated = self._set_native_model(session_id, selected)
        if not any(item.get('id') == 'model' and matches(item.get('currentValue')) for item in updated):
            raise RuntimeError('DSH model switch was not confirmed')
        return updated

    def resume(self, session_id, mcp_servers=None, *, model_route=None):
        resumed = self.request('session/resume', {
            'sessionId': session_id, 'cwd': self.workspace, 'mcpServers': mcp_servers or [],
        })
        route = model_route if model_route is not None else getattr(self, 'model_route', None)
        options = self._apply_model_route(session_id, resumed.get('configOptions', []), route)
        self._remember_models(session_id, options)
        return {**resumed, 'configOptions': options}

    def model_status(self, session_id, mcp_servers=None, *, model_route=None):
        configs = self.__dict__.setdefault('_session_model_configs', {})
        route = model_route if model_route is not None else self.model_route
        if session_id not in configs:
            self.resume(session_id, mcp_servers, model_route=model_route)
        self._refresh_shared_catalog(session_id)
        # Session/new, resume and set_config_option are the authoritative ACP
        # config snapshots. Active Sessions cannot be resumed again; reading this
        # snapshot also avoids resetting explicit reasoning effort with a no-op SET.
        options = self._apply_model_route(session_id, configs[session_id], route)
        self._remember_models(session_id, options)
        view = model_view(options)
        if getattr(self, 'shared_models', None) is not None:
            view['current_available'] = any((c['provider'], c['model']) ==
                                           (view['current']['provider'], view['current']['model'])
                                           for c in view['choices'])
        return view

    def _refresh_shared_catalog(self, session_id):
        shared = getattr(self, 'shared_models', None)
        if shared is None:
            return
        with self._catalog_lock:
            fingerprint, catalog = shared.snapshot()
            published = {(row['provider'], model): name
                         for row in catalog
                         for model, name in zip(row['models'], row.get('names', row['models']))}
            deadline = time.monotonic() + 10

            def collect(notification):
                params = notification.payload or {}
                sid = params.get('sessionId')
                existing = self._session_model_configs.get(sid)
                if existing is None:
                    return
                incoming = next((item for item in params.get('update', {}).get('configOptions', [])
                                 if item.get('id') == 'model'), None)
                if incoming is not None:
                    # Topology notifications can predate a later successful SET.
                    # Refresh choices only; keep confirmed model/reasoning state.
                    current = next(item for item in existing if item.get('id') == 'model')
                    current['options'] = copy.deepcopy(incoming.get('options', []))

            while True:
                self._model_notifications.drain(collect)
                options = copy.deepcopy(self._session_model_configs[session_id])
                view = model_view(options)
                advertised = {(c['provider'], c['model']): c['name'] for c in view['choices']}
                if all(advertised.get(route) == name for route, name in published.items()):
                    current = next(item for item in options if item['id'] == 'model')
                    for group in current.get('options', []):
                        if 'group' in group:
                            group['options'] = [entry for entry in group.get('options', [])
                                                if validate_model_route(json.loads(entry['value'])) in published]
                    latest_fingerprint, _ = shared.snapshot()
                    if latest_fingerprint != fingerprint:
                        return self._refresh_shared_catalog(session_id)
                    self._remember_models(session_id, options)
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError('DSH原生模型目录尚未刷新，请稍后重试。')
                time.sleep(0.025)

    def select_model(self, session_id, provider, model, mcp_servers=None, *, previous_route=None):
        route = validate_model_route((provider, model))
        previous = self.model_status(session_id, mcp_servers, model_route=previous_route)
        if not any((choice['provider'], choice['model']) == route for choice in previous['choices']):
            raise ValueError('该模型不在DSH当前公开目录中，请先查看/provider list。')
        selected = next(c['value'] for c in previous['choices'] if (c['provider'], c['model']) == route)
        return model_view(self._set_native_model(session_id, selected))

    def run(self, session_id, delta, images=None, *, cancel_event=None):
        if images is not None:
            if not self.image_prompts:
                raise RuntimeError("pinned DSH ACP connection did not advertise image prompt capability")
            if not isinstance(images, list) or not images:
                raise ValueError("ACP image prompt must contain an ordered non-empty image list")
            prompt = [{"type": "text", "text": delta}, *copy.deepcopy(images)]
        else:
            prompt = [{"type": "text", "text": delta}]
        # Register this exact turn before any catalog work. A cancellation can
        # then latch while refresh is blocking, and it cannot leak to a later
        # prompt that happens to reuse this ACP session ID.
        token = {"requested": bool(cancel_event and cancel_event.is_set()),
                 "dispatched": False, "trace": CURRENT_TRACE.get(),
                 "cancel_event": cancel_event}
        with self._control_lock:
            if session_id in self._active_prompts:
                raise RuntimeError("DSH session already has an active prompt")
            self._active_prompts[session_id] = token
        if token["requested"]:
            with self._control_lock:
                if self._active_prompts.get(session_id) is token:
                    self._active_prompts.pop(session_id, None)
            raise DshTurnCancelled("DSH turn was cancelled before model preparation")
        try:
            if getattr(self, "shared_models", None) is not None:
                self._refresh_shared_catalog(session_id)
            with self._control_lock:
                if token["requested"] or (cancel_event is not None and cancel_event.is_set()):
                    raise DshTurnCancelled("DSH turn was cancelled before prompt dispatch")
        except BaseException:
            with self._control_lock:
                if self._active_prompts.get(session_id) is token:
                    self._active_prompts.pop(session_id, None)
            raise
        final_message_id = None
        final_chunks = []
        protocol_errors = []
        trace = CURRENT_TRACE.get()
        tool_starts = {}
        token["trace"] = trace
        def collect(notification):
            nonlocal final_message_id
            if notification.method != "session/update" or notification.payload.get("sessionId") != session_id:
                return
            update = notification.payload.get("update", {})
            content = update.get("content", {})
            update_type = update.get("sessionUpdate")
            if trace and update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                raw_id = update.get("toolCallId")
                if isinstance(raw_id, str):
                    if update["sessionUpdate"] == "tool_call":
                        tool_starts[raw_id] = time.monotonic()
                        trace.emit("tool/start", call_id=call_id(raw_id), tool=update.get("title"),
                                   status=update.get("status"))
                    else:
                        before = tool_starts.pop(raw_id, None)
                        trace.emit("tool/end", call_id=call_id(raw_id), status=update.get("status"),
                                   **({"elapsed_ms": (time.monotonic() - before) * 1000} if before else {}))
            if update_type == "tool_call":
                # Any text before a tool call describes that step, not the final reply.
                final_message_id = None
                final_chunks.clear()
                return
            if update_type in {"agent_message_chunk", "agent_thought_chunk"}:
                message_id = update.get("messageId")
                if not isinstance(message_id, str) or not message_id.strip():
                    # The SDK may contain callback exceptions; report malformed upstream
                    # updates only after request() has returned to its caller.
                    protocol_errors.append("assistant update has no valid messageId")
                    final_message_id = None
                    final_chunks.clear()
                    return
                if message_id != final_message_id:
                    final_message_id = message_id
                    final_chunks.clear()
                if (update_type == "agent_message_chunk" and content.get("type") == "text"
                        and isinstance(content.get("text"), str)):
                    final_chunks.append(content["text"])
        try:
            result = self.request("session/prompt", {
                "sessionId": session_id, "prompt": prompt,
            }, on_notification=collect, notification_filter=lambda n:
               n.method == "session/update" and n.payload.get("sessionId") == session_id)
        except BaseException as error:
            if trace:
                trace.emit("dsh/error", error_type=type(error).__name__)
            raise
        finally:
            with self._control_lock:
                if self._active_prompts.get(session_id) is token:
                    self._active_prompts.pop(session_id, None)
        if result.get("stopReason") == "cancelled":
            if trace:
                trace.emit("dsh/stop", stop_reason="cancelled")
            raise DshTurnCancelled("DSH turn was cancelled")
        if trace:
            trace.emit("dsh/stop", stop_reason=result.get("stopReason"))
        if result.get("stopReason") != "end_turn":
            raise RuntimeError(f"DSH turn did not complete: {result.get('stopReason')}")
        if protocol_errors:
            raise RuntimeError("DSH ACP protocol error: " + "; ".join(protocol_errors))
        text = "".join(final_chunks).strip()
        if not text:
            raise RuntimeError("DSH completed without a text reply")
        return text

    def cancel_session(self, session_id, cancel_event=None):
        """Request cancellation of this session's current prompt without waiting."""
        with self._control_lock:
            token = self._active_prompts.get(session_id)
            if token is None:
                return bool(cancel_event is not None and cancel_event.is_set())
            if cancel_event is not None and token.get("cancel_event") is not cancel_event:
                return False
            token["requested"] = True
            if cancel_event is not None:
                cancel_event.set()
            trace = token.get("trace")
            control = CURRENT_TRACE.get()
            if trace:
                trace.emit("cancel/requested", **({"related_turn_id": control.id} if control else {}))
            if token["dispatched"]:
                self.client.notify("session/cancel", {"sessionId": session_id})
            return True

    def close_session(self, session_id):
        self.request("session/close", {"sessionId": session_id})
        self.__dict__.get('_session_model_configs', {}).pop(session_id, None)
        self.__dict__.get('_uncertain_model_sessions', set()).discard(session_id)

    def transport_alive(self):
        # The pinned SDK has no non-generating public ping. Check its actual
        # process and reader thread; provider availability requires a real turn.
        process = self.client._proc
        reader = self.client._reader_thread
        return bool(process is not None and process.poll() is None
                    and reader is not None and reader.is_alive())

    def close(self):
        subscription = getattr(self, '_model_notifications', None)
        if subscription is not None:
            subscription.close()
        self.client.close()
        directory = getattr(self, "_console_control_dir", None)
        if directory is not None:
            directory.cleanup()

    def compact_session(self, session_id):
        path = getattr(self, "_console_control_path", None)
        if not path:
            raise RuntimeError("DSH console control is not enabled")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(300)
            connection.connect(path)
            connection.sendall((json.dumps({"action": "compact", "session_id": session_id,
                                           "token": self._console_control_token}) + "\n").encode())
            response = b""
            while b"\n" not in response:
                chunk = connection.recv(4096)
                if not chunk or len(response) + len(chunk) > 4096:
                    raise RuntimeError("DSH compaction receipt was lost; inspect session before retrying")
                response += chunk
            receipt = json.loads(response.split(b"\n", 1)[0])
            if receipt.get("ok") is not True:
                raise RuntimeError("DSH manual compaction did not complete")
            return {"compacted": receipt["compacted"]}


class DshTurnCancelled(RuntimeError):
    """The pinned ACP runtime confirmed that the active turn was cancelled."""


class DshTurnFailure(RuntimeError):
    """A bounded terminal classification backed by a native turn receipt."""

    def __init__(self, kind, code="UNKNOWN", *, tools_started=(), tools_finished=(), unfinished_tools=()):
        if kind not in {"model", "infrastructure", "unknown"}:
            kind = "unknown"
        self.kind = kind
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "UNKNOWN"
        self.tools_started = tuple(tools_started) if isinstance(tools_started, (list, tuple)) else ()
        self.tools_finished = tuple(tools_finished) if isinstance(tools_finished, (list, tuple)) else ()
        self.unfinished_tools = tuple(unfinished_tools) if isinstance(unfinished_tools, (list, tuple)) else ()
        self.fault_details = {
            "confirmed_tools": list(self.tools_finished)[:30],
            "unknown_tools": list(self.unfinished_tools)[:30],
        }
        super().__init__(f"DSH turn failed ({self.kind}:{self.code})")


class DshModelTurnFailure(DshTurnFailure):
    """Native model failure with every requested tool conclusively settled."""

    def __init__(self, code="UNKNOWN", **evidence):
        super().__init__("model", code, **evidence)


class DshInfrastructureFailure(DshTurnFailure):
    """The DSH runtime or transport could not complete the turn reliably."""

    def __init__(self, code="UNKNOWN", **evidence):
        super().__init__("infrastructure", code, **evidence)


class DshTurnOutcomeUnknown(DshTurnFailure):
    """The available evidence cannot establish whether side effects completed."""

    def __init__(self, code="UNKNOWN", **evidence):
        super().__init__("unknown", code, **evidence)


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
                 enable_authorization: bool = False, execution_profile: str | None = None,
                 owner_shell_env: dict | None = None, group_runtime: dict | None = None,
                 background_callback_url: str | None = None,
                 background_callback_token: str | None = None):
        self.settings = {"dsh_bin": Path(dsh_bin).resolve(), "dsh_home": Path(dsh_home).resolve(),
                         "workspace": Path(workspace).resolve(), "patch": Path(patch).resolve(),
                         "instructions": Path(instructions).resolve()}
        if execution_profile is not None:
            if execution_profile not in {"owner-host", "group-sandbox", "safe-chat"}:
                raise ValueError("unknown execution profile")
            self.settings["execution_profile"] = execution_profile
        if owner_shell_env is not None:
            self.settings["owner_shell_env"] = dict(owner_shell_env)
        if group_runtime is not None:
            self.settings["group_runtime"] = dict(group_runtime)
        if background_callback_url is not None or background_callback_token is not None:
            if (not isinstance(background_callback_url, str) or
                    not background_callback_url.startswith("http://127.0.0.1:") or
                    not background_callback_url.endswith("/background/events") or
                    not isinstance(background_callback_token, str) or
                    len(background_callback_token) < 32):
                raise ValueError("background callback endpoint and token must be configured together")
            self.settings.update(background_callback_url=background_callback_url,
                                 background_callback_token=background_callback_token)
        for name, value in (("provider", provider), ("model", model)):
            value = value if value is not None else os.environ.get("V2_DSH_" + name.upper())
            if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
                raise ValueError(f"V2_DSH_{name.upper()} must explicitly select a valid DSH {name}")
            self.settings[name] = value
        for key in ("dsh_bin", "patch", "instructions"):
            if not self.settings[key].is_file():
                raise FileNotFoundError("V2 pinned DSH runtime or profile assets are missing")
        owner_profile = (self.settings["patch"].name == "v2-agent-owner.patch.yml"
                         or execution_profile == "owner-host")
        if owner_profile:
            if Path(dsh_home).is_symlink() or (execution_profile is None and Path(workspace).is_symlink()):
                raise ValueError("owner runtime directory must not be a symlink")
            if enable_authorization is not True:
                raise ValueError("owner Profile requires host-issued trusted source authorization")
            if execution_profile is None:
                home, work = self.settings["dsh_home"], self.settings["workspace"]
                if home.is_relative_to(work) or work.is_relative_to(home):
                    raise ValueError("owner DSH home and workspace must be disjoint")
        if enable_authorization and (execution_profile is not None or owner_profile):
            _prepare_owner_directory(self.settings["dsh_home"])
        else:
            self.settings["dsh_home"].mkdir(parents=True, exist_ok=True)
        if owner_profile and execution_profile is None:
            _prepare_owner_directory(self.settings["workspace"])
        elif not owner_profile and self.settings["patch"].name != "v2-agent-group-sandbox.patch.yml":
            self.settings["workspace"].mkdir(parents=True, exist_ok=True)
        elif owner_profile:
            # For owner-host cwd is the real project root, not a private DSH home.
            if self.settings["workspace"].is_symlink():
                raise ValueError("owner working directory must not be a symlink")
        elif self.settings["patch"].name == "v2-agent-group-sandbox.patch.yml":
            if self.settings["workspace"].is_symlink():
                raise ValueError("group workspace must not be a symlink")
        if not isinstance(enable_authorization, bool):
            raise ValueError("enable_authorization must be a bool")
        self.enable_authorization = enable_authorization
        self.turn_sources = TurnSourcePublisher(self.settings["dsh_home"]) if enable_authorization else None
        # Legacy scoped executor configuration is not carried into the new
        # owner-host or group-sandbox generations.
        self.scope_root = (self.settings["dsh_home"] / "v2-conversation-scopes"
                           if enable_authorization and execution_profile is None else None)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._session_locks = {}
        self._turn_tokens = {}
        self._sessions = set()
        self._session_servers = {}
        self._confirmed_model_routes = {}
        self._quarantined_sessions = {}
        self._closed = False
        self._runtime_stopped = False
        self._sources_closed = self.turn_sources is None
        profile_patch_names = {"v2-agent-group.patch.yml", "v2-agent-owner.patch.yml",
                               "v2-agent-group-sandbox.patch.yml"}
        self.personas = (PersonaStateStore(self.settings["dsh_home"] / "agent-bot-personas")
                         if self.settings["patch"].name in profile_patch_names else None)
        self.scenes = (SceneStateStore(self.settings["dsh_home"] / "agent-bot-scenes")
                       if self.settings["patch"].name in profile_patch_names else None)
        # Initialize the shared transport without creating a session or running
        # inference. Provider availability is established by real turns.
        try:
            if self.scope_root is not None:
                self.scope_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.scope_root, 0o700)
                self.settings["scope_root"] = self.scope_root
            if self.turn_sources is not None:
                # Only the trusted native profile extension can access these facts.
                self.settings.update(source_dir=self.turn_sources.directory,
                                     source_epoch=self.turn_sources.epoch)
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
            self._confirmed_model_routes[session_id] = (self.settings['provider'], self.settings['model'])
        return session_id

    def model_status(self, session_id, mcp_servers=None, *, model_route=None):
        return self._model_control(session_id, mcp_servers, model_route=model_route)

    def compact_session(self, session_id, mcp_servers=None, *, model_route=None):
        self._model_control(session_id, mcp_servers, model_route=model_route)
        with self._session_lock(session_id):
            return self.runtime.compact_session(session_id)

    def select_model(self, session_id, provider, model, mcp_servers=None, *, previous_route=None):
        return self._model_control(session_id, mcp_servers, selection=(provider, model), model_route=previous_route)

    def _model_control(self, session_id, mcp_servers, *, selection=None, model_route=None):
        servers = copy.deepcopy(mcp_servers or [])
        with self._session_lock(session_id):
            with self._lock:
                if session_id in self._quarantined_sessions:
                    raise RuntimeError('DSH session is quarantined')
                if session_id in self._sessions and self._session_servers[session_id] != servers:
                    raise ValueError('MCP binding changed for an active DSH session')
            try:
                if selection is None:
                    result = self.runtime.model_status(session_id, servers or None, model_route=model_route)
                else:
                    result = self.runtime.select_model(session_id, *selection, servers or None, previous_route=model_route)
            except BaseException:
                with self._lock:
                    self._confirmed_model_routes.pop(session_id, None)
                raise
            with self._lock:
                self._sessions.add(session_id)
                self._session_servers[session_id] = servers
                self._confirmed_model_routes[session_id] = (result['current']['provider'], result['current']['model'])
            return result

    def _runtime_run(self, session_id, delta, *, images=None, cancel_token):
        run = self.runtime.run
        parameters = inspect.signature(run).parameters.values()
        names = {item.name for item in parameters}
        options = {}
        if "cancel_event" in names:
            options["cancel_event"] = cancel_token
        if images is not None:
            options["images"] = images
        return run(session_id, delta, **options)

    def reserve_turn(self, session_id):
        """Synchronously reserve cancellation identity before a worker starts."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session ID is required")
        with self._lock:
            if self._closed:
                raise RuntimeError("DSH client is closed")
            if session_id in self._quarantined_sessions:
                raise RuntimeError("DSH session is quarantined")
            if session_id in self._turn_tokens:
                raise RuntimeError("DSH session already has a reserved turn")
            token = threading.Event()
            self._turn_tokens[session_id] = token
            return token

    def run(self, session_id, journal_delta, mcp_servers=None, trusted_source=None, *, images=None, model_route=None, cancel_token=None):
        if not session_id or not journal_delta:
            raise ValueError("session ID and journal delta are required")
        if cancel_token is None:
            # Direct callers historically queue on the per-session lock.
            # Reserve only when the preceding turn has released its token.
            with self._lock:
                condition = self.__dict__.setdefault("_turn_condition", threading.Condition(self._lock))
                while session_id in self._turn_tokens:
                    condition.wait()
                if self._closed:
                    raise RuntimeError("DSH client is closed")
                if session_id in self._quarantined_sessions:
                    raise RuntimeError("DSH session is quarantined")
                cancel_token = threading.Event()
                self._turn_tokens[session_id] = cancel_token
        else:
            with self._lock:
                if self._turn_tokens.get(session_id) is not cancel_token:
                    raise RuntimeError("DSH turn cancellation token is stale or not reserved")
        try:
            return self._run_reserved(session_id, journal_delta, mcp_servers, trusted_source,
                                      images=images, model_route=model_route,
                                      cancel_token=cancel_token)
        finally:
            with self._lock:
                if self._turn_tokens.get(session_id) is cancel_token:
                    self._turn_tokens.pop(session_id, None)
                condition = self.__dict__.get("_turn_condition")
                if condition is not None:
                    condition.notify_all()

    def _check_prepared_cancellation(self, session_id, cancel_token):
        if not cancel_token.is_set():
            return
        # Preparation may already have loaded the native SID. Drain it while
        # owning the run lock, keeping it tracked until close is confirmed.
        with self._lock:
            loaded = session_id in self._sessions
        if loaded:
            try:
                self.runtime.close_session(session_id)
            except BaseException as error:
                with self._lock:
                    self._quarantined_sessions[session_id] = error
                raise DshInfrastructureFailure("SESSION_DRAIN_FAILED") from error
            with self._lock:
                self._sessions.discard(session_id)
                self._session_servers.pop(session_id, None)
                self._confirmed_model_routes.pop(session_id, None)
        raise DshTurnCancelled("DSH turn was cancelled during preparation")

    def _run_reserved(self, session_id, journal_delta, mcp_servers=None, trusted_source=None, *, images=None, model_route=None, cancel_token):
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
                transport_alive = getattr(self.runtime, "transport_alive", None)
                if callable(transport_alive) and not transport_alive():
                    raise DshInfrastructureFailure("RUNTIME_UNAVAILABLE")
            self._check_prepared_cancellation(session_id, cancel_token)
            if ready and self._session_servers[session_id] != servers:
                raise ValueError("MCP binding changed for an active DSH session")
            if model_route is not None:
                model_route = validate_model_route(model_route)
            if not ready:
                # Failure affects only this session; never replace its saved ID
                # or close the shared process used by other conversations.
                options = {'model_route': model_route} if model_route is not None else {}
                if servers:
                    self.runtime.resume(session_id, servers, **options)
                else:
                    self.runtime.resume(session_id, **options)
                with self._lock:
                    self._sessions.add(session_id)
                    self._session_servers[session_id] = servers
                    self._confirmed_model_routes[session_id] = model_route or (self.settings['provider'], self.settings['model'])
                self._check_prepared_cancellation(session_id, cancel_token)
            else:
                desired = model_route or (self.settings['provider'], self.settings['model'])
                with self._lock:
                    confirmed = self._confirmed_model_routes.get(session_id)
                if confirmed != desired:
                    self.runtime.model_status(session_id, servers or None, model_route=model_route)
                    self._check_prepared_cancellation(session_id, cancel_token)
                    with self._lock:
                        self._confirmed_model_routes[session_id] = desired
            if not self.enable_authorization:
                self._check_prepared_cancellation(session_id, cancel_token)
                return self._runtime_run(session_id, journal_delta, images=images,
                                         cancel_token=cancel_token)
            self._check_prepared_cancellation(session_id, cancel_token)
            nonce = self.turn_sources.publish(session_id, trusted_source, journal_delta, images=images)
            try:
                if cancel_token.is_set():
                    raise DshTurnCancelled("DSH turn was cancelled before prompt dispatch")
                result = self._runtime_run(session_id, journal_delta, images=images,
                                           cancel_token=cancel_token)
            except BaseException as failure:
                receipt = None
                if self.turn_sources is not None:
                    try:
                        receipt = self.turn_sources.terminal_evidence(session_id, nonce)
                    except Exception:
                        receipt = None
                classified = None
                if isinstance(failure, DshTurnCancelled):
                    classified = failure
                elif isinstance(receipt, dict) and receipt.get("terminal") is True:
                    kind = receipt.get("kind")
                    code = receipt.get("code", "UNKNOWN")
                    started = receipt.get("tools_started", [])
                    finished = receipt.get("tools_finished", [])
                    unfinished = receipt.get("unfinished_tools", [])
                    if kind == "infrastructure" and not started and not unfinished:
                        classified = DshInfrastructureFailure(code)
                    elif kind == "infrastructure":
                        classified = DshTurnOutcomeUnknown(
                            code, tools_started=started, tools_finished=finished,
                            unfinished_tools=unfinished,
                        )
                    elif kind == "model" and not unfinished and Counter(started) == Counter(finished):
                        classified = DshModelTurnFailure(code, tools_started=started, tools_finished=finished)
                    else:
                        classified = DshTurnOutcomeUnknown(
                            code, tools_started=started, tools_finished=finished,
                            unfinished_tools=unfinished,
                        )
                # A transport timeout only abandons the Python waiter. The ACP
                # session must be closed to cancel and drain its Agent before
                # the trusted source can disappear.
                try:
                    self.runtime.close_session(session_id)
                except BaseException as drain_error:
                    with self._lock:
                        self._quarantined_sessions[session_id] = drain_error
                    raise DshInfrastructureFailure("SESSION_DRAIN_FAILED",
                        tools_started=(receipt or {}).get("tools_started", []),
                        tools_finished=(receipt or {}).get("tools_finished", []),
                        unfinished_tools=(receipt or {}).get("unfinished_tools", [])) from drain_error
                with self._lock:
                    self._sessions.discard(session_id)
                    self._session_servers.pop(session_id, None)
                    self._confirmed_model_routes.pop(session_id, None)
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
                if classified is not None:
                    raise classified from failure
                raise DshTurnOutcomeUnknown("NO_TERMINAL_RECEIPT") from failure
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
    def cancel_session(self, session_id, cancel_token=None):
        """Cancel an active turn without acquiring its per-session run lock."""
        with self._lock:
            if self._closed or session_id in self._quarantined_sessions:
                return False
            runtime = self.runtime
            current = self._turn_tokens.get(session_id)
            if cancel_token is not None and current is not cancel_token:
                return False
            if current is not None:
                current.set()
            elif cancel_token is not None:
                return False
        cancel = getattr(runtime, "cancel_session", None)
        if cancel is not None:
            try:
                accepted = bool(cancel(session_id, current))
            except TypeError:
                accepted = bool(cancel(session_id))
        else:
            accepted = False
        return accepted or current is not None

    def health(self):
        with self._lock:
            closed = self._closed or self._runtime_stopped
            quarantined = len(self._quarantined_sessions)
        return {"ready": not closed and self.runtime.transport_alive(),
                "quarantined_sessions": quarantined}

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
            try:
                self.runtime.close_session(session_id)
            except BaseException as drain_error:
                # ACP close may report an error after the native Session has
                # already been removed. Never retry resume/close against an
                # ambiguous lifecycle; keep it tracked for process shutdown.
                with self._lock:
                    self._quarantined_sessions[session_id] = drain_error
                raise RuntimeError(
                    "DSH session drain failed; session is quarantined until runtime shutdown"
                ) from drain_error
            with self._lock:
                self._sessions.discard(session_id)
                self._session_servers.pop(session_id, None)
                self._confirmed_model_routes.pop(session_id, None)

    def drain_session(self, session_id, mcp_servers=None):
        """Resume and close one persisted Session without running a prompt."""
        if not session_id:
            raise ValueError("session ID is required")
        servers = copy.deepcopy(mcp_servers or [])
        with self._session_lock(session_id):
            with self._lock:
                if session_id in self._quarantined_sessions:
                    raise RuntimeError(
                        "DSH session is quarantined after an undrained turn; runtime shutdown is required"
                    ) from self._quarantined_sessions[session_id]
                loaded = session_id in self._sessions
            if not loaded:
                if servers:
                    self.runtime.resume(session_id, servers)
                else:
                    self.runtime.resume(session_id)
                # If close fails, shutdown must still retry this exact Session.
                with self._lock:
                    self._sessions.add(session_id)
                    self._session_servers[session_id] = servers
            try:
                self.runtime.close_session(session_id)
            except BaseException as drain_error:
                # Pinned ACP deletes its session record even on failed close.
                # An uncertain drain must not be retried against that record.
                with self._lock:
                    self._quarantined_sessions[session_id] = drain_error
                raise RuntimeError(
                    "DSH session drain failed; session is quarantined until runtime shutdown"
                ) from drain_error
            with self._lock:
                self._sessions.discard(session_id)
                self._session_servers.pop(session_id, None)
                self._confirmed_model_routes.pop(session_id, None)

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
                        self._confirmed_model_routes.pop(session_id, None)
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
                        self._confirmed_model_routes.clear()
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
