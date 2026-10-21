#!/usr/bin/env python3
"""Exercise the V2 console over isolated AstrBot HTTP and simulated OneBot."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = ROOT / ".runtime/astrbot"
PYTHON = ASTRBOT_SOURCE / ".venv/bin/python"
TEST_USERNAME = "v2-console-isolated-owner"
TEST_PASSWORD = "ConsoleTest123!"
GROUP_ID = "92000"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load("console_sandbox_smoke", ROOT / "scripts/check-sandbox.py")
runtime = load("console_sandbox_runtime", ROOT / "sandbox/runtime.py")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


class ConsoleCheck(base.Smoke):
    owner = "90009"

    def __init__(self, directory: Path):
        super().__init__(directory, live=True)
        self.dashboard_port = _free_port()
        self.dashboard_base = f"http://127.0.0.1:{self.dashboard_port}"
        self.cookie_opener = None
        self.dashboard_token = None
        self.details = None
        self.launch_log = None
        self.result_cases = []

    @property
    def repo(self) -> Path:
        return Path(self.details["staged_repo"])

    @property
    def console_store_path(self) -> Path:
        return (Path(self.details["astrbot_root"]) / "data/plugin_data/v2_console/state.json")

    def _stage(self):
        ports = {"onebot": self.onebot, "ui": self.ui, "mcp": self.mcp}
        self.details = runtime.prepare(self.directory, ASTRBOT_SOURCE, ports, "scripted", self.owner)
        staged = self.repo
        shutil.copy2(ROOT / "upstream.lock.json", staged / "upstream.lock.json")

        # Sandbox's normal DSH tree is a symlink. Replace it with a private
        # snapshot of current profile/plugin/assets; keep the pinned runtime
        # itself on the existing .runtime/dsh-runtime symlink.
        dsh_target = staged / "dsh"
        if dsh_target.is_symlink():
            dsh_target.unlink()
        elif dsh_target.exists():
            shutil.rmtree(dsh_target)
        shutil.copytree(ROOT / "dsh", dsh_target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules",
                                                       "dist", ".git", ".pytest_cache"))
        (dsh_target / "node_modules").symlink_to(
            ROOT / ".runtime/dsh-runtime/node_modules", target_is_directory=True)
        probe = staged / "sandbox/probe.mjs"
        if not probe.is_file():
            raise RuntimeError("sandbox scripted provider fixture was not staged")
        for profile_name in ("v2-agent-group.patch.yml", "v2-agent-owner.patch.yml",
                             "v2-agent-group-sandbox.patch.yml"):
            profile = dsh_target / "profile" / profile_name
            if profile.is_file():
                with profile.open("a", encoding="utf-8") as output:
                    output.write(
                        "\n- insert:\n"
                        "    - id: v2-sandbox-probe\n"
                        f"      name: {json.dumps(str(probe))}\n"
                        "      config:\n"
                        "        controlUrl: !!js process.env.SANDBOX_CONTROL_URL\n"
                        "        token: !!js process.env.SANDBOX_CONTROL_TOKEN\n"
                        "        mode: !!js process.env.SANDBOX_DSH_MODE\n"
                    )

        router_source = ROOT / "astrbot-plugins/v2_dsh_router"
        router_target = staged / "astrbot-plugins/v2_dsh_router"
        if router_target.is_symlink():
            raise RuntimeError("staged Router unexpectedly became a symlink")
        shutil.rmtree(router_target)
        shutil.copytree(router_source, router_target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))

        console_source = ROOT / "astrbot-plugins/v2_console"
        console_target = staged / "astrbot-plugins/v2_console"
        shutil.copytree(console_source, console_target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "node_modules", "dist"))
        plugin_link = Path(self.details["astrbot_root"]) / "data/plugins/v2_console"
        plugin_link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if plugin_link.exists() or plugin_link.is_symlink():
            if plugin_link.is_symlink():
                plugin_link.unlink()
            else:
                raise RuntimeError("refusing to replace a non-symlink staged console plugin")
        # Pinned plugin Page discovery rejects symlinked plugin roots outside
        # plugin_store_path. Install this UI plugin as a real directory.
        shutil.copytree(console_target, plugin_link)

        runtime_dir = staged / ".runtime"
        service_config = runtime_dir / "agent-service.json"
        service_config.write_text(json.dumps({"allowed_conversations": [
            "group:91000", "private:90001", "private:90002", "private:" + self.owner,
        ]}) + "\n", encoding="utf-8")
        service_config.chmod(0o600)

        # The staged dashboard uses a fixed test-only identity and a fresh JWT
        # secret; no production config or credential is copied.
        config_path = Path(self.details["astrbot_root"]) / "data/cmd_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        dashboard = config["dashboard"]
        dashboard.update({"enable": True, "host": "127.0.0.1", "port": self.dashboard_port,
                          "username": TEST_USERNAME, "password": "",
                          "pbkdf2_password": _hash_password(TEST_PASSWORD),
                          "password_storage_upgraded": True,
                          "password_change_required": False,
                          "jwt_secret": secrets.token_urlsafe(40)})
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        config_path.chmod(0o600)

        # Shared catalog fixture maps the zero-cost sandbox provider to the
        # exact choices exposed by the native DSH scripted adapter.
        profile = [{"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {
            "providers": {"sandbox-scripted": {
                "api": "openai-completions", "baseURL": "https://example.invalid/v1",
                "models": [{"id": "scripted", "name": "scripted"},
                           {"id": "scripted-alt", "name": "scripted-alt"}],
            }}
        }}]
        self.save("models.patch.yml", profile)
        credentials = self.directory / "credentials.yaml"
        credentials.write_text("version: 1\nrefs: {}\n", encoding="utf-8")
        credentials.chmod(0o600)

    def save(self, name, value):
        path = self.directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _launcher(self):
        script = Path(__file__).resolve()
        cmd = [str(PYTHON), str(script), "--launch-private", str(self.directory),
               "--dashboard-port", str(self.dashboard_port)]
        self.launch_log = (self.directory / "runtime.log").open("ab")
        self.process = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=self.launch_log, stderr=subprocess.STDOUT,
                                        start_new_session=True)

    def start(self):
        if self.details is None:
            self._stage()
        self._launcher()
        self.wait(lambda: self.http("/api/state").get("connected"), "isolated OneBot connected", timeout=90)
        self.wait(lambda: self.console_store_path.exists(), "console and Router shared store created", timeout=90)
        self.wait(self._dashboard_ready, "isolated AstrBot Dashboard ready", timeout=90)
        self._login()
        self.workspace = self.repo

    def _dashboard_ready(self):
        try:
            req = Request(self.dashboard_base + "/api/v1/auth/setup-status")
            with urlopen(req, timeout=3) as response:
                return response.status == 200
        except (URLError, OSError, HTTPError):
            return False

    def _login(self):
        self.cookie_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, result = self.dashboard_call("/api/v1/auth/login", {
            "username": TEST_USERNAME, "password": TEST_PASSWORD,
        }, opener=self.cookie_opener)
        if status != 200 or result.get("status") != "ok":
            raise AssertionError("isolated Dashboard login failed: " + repr(result)[:500])
        self.dashboard_token = result["data"]["token"]

    def dashboard_call(self, path, payload=None, *, opener=None, headers=None, allow_error=False):
        selected_headers = {"Content-Type": "application/json"}
        if self.dashboard_token and (opener is None or opener is self.cookie_opener):
            selected_headers["Authorization"] = "Bearer " + self.dashboard_token
        selected_headers.update(headers or {})
        req = Request(self.dashboard_base + path,
                      data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                      headers=selected_headers,
                      method="POST" if payload is not None else "GET")
        client = opener or self.cookie_opener or build_opener()
        try:
            with client.open(req, timeout=25) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            raw = error.read()
            if allow_error:
                try:
                    return error.code, json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    return error.code, {"body": raw.decode("utf-8", "replace")[:500]}
            raise AssertionError(f"Dashboard {path} HTTP {error.code}: {raw.decode('utf-8', 'replace')[:800]}") from error

    def console(self, payload, *, owner=None):
        headers = {}
        opener = self.cookie_opener
        if owner is not None:
            import jwt
            config = json.loads((Path(self.details["astrbot_root"]) / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
            token = jwt.encode({"username": owner,
                                "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                               config["dashboard"]["jwt_secret"], algorithm="HS256")
            opener = build_opener()
            headers["Authorization"] = "Bearer " + token
        status, result = self.dashboard_call("/api/plug/v2_console/dispatch", payload,
                                             opener=opener, headers=headers,
                                             allow_error=owner is not None)
        if owner is not None and owner != TEST_USERNAME:
            if status >= 400 or result.get("status") != "error":
                raise AssertionError("non-owner Dashboard identity reached V2 console management")
            return result
        if status != 200 or result.get("status") != "ok":
            raise AssertionError("console dispatch failed: " + repr(result)[:800])
        return result["data"]

    def request(self, method, path, payload=None, *, allow_error=False):
        req = Request(self.base + path,
                      data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                      headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(req, timeout=35) as response:
                return json.load(response)
        except HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            if allow_error:
                return error.code, raw
            raise AssertionError(f"sandbox {method} {path} HTTP {error.code}: {raw[:500]}") from error

    def case(self, name, action):
        value = action()
        self.result_cases.append({"case": name, "passed": True, "evidence": value})
        print(f"PASS {name}", flush=True)

    def rows(self):
        return super().rows()

    def send_message(self, conversation_id, text, *, sender="90001", mention=True):
        message = [{"type": "at", "data": {"qq": "99000"}},
                   {"type": "text", "data": {"text": text}}] if mention else [
                       {"type": "text", "data": {"text": text}}]
        result = self.request("POST", "/api/send", {
            "conversation": {"type": "group", "id": str(conversation_id)},
            "sender_id": str(sender), "message": message,
        })
        event = result["event"]
        message_id = str(event["message_id"])
        self.wait(lambda: any(str(row["message_id"]) == message_id and
                              row["conversation_key"] == f"group:{conversation_id}"
                              for row in self.rows()), "canonical group input")
        return event

    def outputs(self, event):
        return [row for row in self.events()
                if row["direction"] == "outbound"
                and row["conversation"] == event["conversation"]
                and int(row["message_id"]) > int(event["message_id"])]

    def add_template(self):
        snapshot = self.console({"action": "snapshot"})
        created = self.console({"action": "template.create", "name": "Integration base",
                                "expected_revision": snapshot["revision"]})
        template_id = created["templates"][-1]["id"]
        self.console({"action": "files.mkdir", "template_id": template_id, "path": "notes"})
        self.console({"action": "files.save", "template_id": template_id,
                      "path": "AGENTS.md", "content": "isolated console template\n"})
        contents = self.console({"action": "files.read", "template_id": template_id,
                                 "path": "AGENTS.md"})
        assert contents["content"] == "isolated console template\n"
        return template_id

    def run_checks(self):
        try:
            self.start()
            self.case("owner-dashboard-login-and-snapshot", lambda: self._check_auth_snapshot())
            self.case("real-dashboard-iframe-and-local-editor", self._check_browser)
            template_id = self.add_template()
            self.case("template-files-save-and-read", lambda: {"template_id": template_id})
            self._set_defaults()
            self.case("defaults-model-enabled-and-independent-copy", lambda: self._check_defaults())
            self._add_sandbox_group()
            self.case("group-add-syncs-both-admission-layers-and-copies-template",
                      lambda: self._check_group_gates(template_id))
            self.case("disable-blocks-qq-wake-enable-resumes-new-wake", lambda: self._check_enable_flow())
            self.case("native-compact-and-new-context-preserve-workspace", lambda: self._check_context_actions())
            self.case("router-model-command-shares-console-store", lambda: self._check_model_command())
            self.case("isolated-stop-start-retains-console-session-and-template",
                      lambda: self._check_restart_persistence(template_id))
        except Exception as error:
            (self.directory / "failure-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
            self.result_cases.append({"case": "fatal", "passed": False,
                                      "error_type": type(error).__name__, "error": str(error)[:1200]})
            print(f"FAIL {type(error).__name__}: {error}", flush=True)
        finally:
            if self.process:
                try:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                except Exception:
                    pass
            self.stop()
            if self.launch_log:
                self.launch_log.close()
                self.launch_log = None

        result = {"passed": bool(self.result_cases) and all(item["passed"] for item in self.result_cases),
                  "cases": self.result_cases, "real_qq_connections": 0,
                  "real_model_calls": 0, "real_systemd_restarts": 0,
                  "data_dir": str(self.directory), "staged_astrbot_root": str(
                      self.details["astrbot_root"] if self.details else "")}
        self.save("result.json", result)
        return result

    def _check_auth_snapshot(self):
        status, _body = self.dashboard_call("/api/plug/v2_console/dispatch", {"action": "snapshot"},
                                            opener=build_opener(), allow_error=True)
        if status < 400:
            raise AssertionError("console API accepted an unauthenticated dashboard request")
        outsider = self.console({"action": "snapshot"}, owner="another-dashboard-user")
        assert outsider["status"] == "error"
        snapshot = self.console({"action": "snapshot"})
        assert {"revision", "defaults", "conversations", "templates"} <= set(snapshot)
        export_status, _body = self.dashboard_call("/api/plug/v2_console/export",
                                                  opener=build_opener(), allow_error=True)
        assert export_status == 401
        import jwt
        config = json.loads((Path(self.details["astrbot_root"]) / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
        outsider_token = jwt.encode({"username": "another-dashboard-user",
                                     "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                                    config["dashboard"]["jwt_secret"], algorithm="HS256")
        export_path = "/api/plug/v2_console/export?" + urlencode({"conversation_id": snapshot["conversations"][0]["id"]})
        denied_status, _body = self.dashboard_call(export_path, opener=build_opener(),
                          headers={"Authorization": "Bearer " + outsider_token}, allow_error=True)
        assert denied_status == 403
        unknown_status, _body = self.dashboard_call("/api/plug/v2_console/export?conversation_id=unknown",
                                                   allow_error=True)
        assert unknown_status == 404
        return {"owner_login": TEST_USERNAME, "anonymous_denied": status,
                "export_anonymous_denied": export_status, "export_other_user_denied": denied_status,
                "export_unknown_session_denied": unknown_status,
                "non_owner_jwt_denied": True, "conversation_count": len(snapshot["conversations"])}

    def _check_browser(self):
        selected = self.directory / "browser-private-input.json"
        self.save(selected.name, {"base": self.dashboard_base, "token": self.dashboard_token,
                                 "username": TEST_USERNAME, "directory": str(self.directory)})
        try:
            result = subprocess.run(["node", str(ROOT / "scripts/check-console-browser.mjs"), str(selected)],
                                    cwd=ROOT, text=True, capture_output=True, timeout=65)
            if result.returncode:
                raise AssertionError("Dashboard browser check failed; see browser-result.json: " + result.stdout[-200:])
            report = json.loads((self.directory / "browser-result.json").read_text())
            assert report["passed"]
            return {"checks": report["checks"], "page_errors": report["page_errors"]}
        finally:
            selected.unlink(missing_ok=True)

    def _set_defaults(self):
        snapshot = self.console({"action": "snapshot"})
        self.console({"action": "defaults.save", "expected_revision": snapshot["revision"],
                      "settings": {"model": {"provider": "sandbox-scripted", "model": "scripted"},
                                   "enabled": True, "editors": ["90001"]}})

    def _check_defaults(self):
        snapshot = self.console({"action": "snapshot"})
        assert snapshot["defaults"] == {"model": {"provider": "sandbox-scripted", "model": "scripted"},
                                         "enabled": True, "editors": ["90001"]}
        owner = next(row for row in snapshot["conversations"]
                     if row["conversation_key"] == "private:" + self.owner)
        assert owner["settings"]["model"] == {"provider": "sandbox-scripted", "model": "scripted"}
        return {"model": snapshot["defaults"]["model"], "enabled": snapshot["defaults"]["enabled"]}

    def _add_sandbox_group(self):
        self.request("POST", "/api/conversations", {"type": "group", "id": GROUP_ID,
                                                       "title": "Console integration group"})
        snapshot = self.console({"action": "snapshot"})
        self.console({"action": "group.add", "expected_revision": snapshot["revision"],
                      "platform_id": self.details["platform_id"], "bot_id": str(self.details["bot"]),
                      "group_id": GROUP_ID,
                      "template_id": snapshot["templates"][-1]["id"]})
        # The sandbox fixture has its own test-only scenario gate and refreshes
        # it before every event. Keep that gate aligned after independently
        # verifying the production console's durable two-layer update.
        persisted = json.loads((self.repo / ".runtime/agent-service.json").read_text())
        assert "group:" + GROUP_ID in persisted["allowed_conversations"]
        scenario = self.request("GET", "/api/scenario")
        self.request("PUT", "/api/scenario", {"revision": scenario["revision"],
                     "admitted": [*scenario["admitted"], "group:" + GROUP_ID]})

    def _group_row(self):
        snapshot = self.console({"action": "snapshot"})
        return snapshot, next(row for row in snapshot["conversations"]
                              if row["conversation_key"] == "group:" + GROUP_ID)

    def _check_group_gates(self, template_id):
        snapshot, row = self._group_row()
        whitelist = json.loads((Path(self.details["astrbot_root"]) / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
        entry = f"{self.details['platform_id']}:GroupMessage:{GROUP_ID}"
        assert entry in whitelist["platform_settings"]["id_whitelist"]
        service_config = json.loads((self.repo / ".runtime/agent-service.json").read_text())
        assert "group:" + GROUP_ID in service_config["allowed_conversations"]
        assert row["template_id"] == template_id
        copied = self.console({"action": "files.read", "conversation_id": row["id"],
                               "path": "AGENTS.md"})
        assert copied["content"] == "isolated console template\n"
        return {"revision": snapshot["revision"], "group": GROUP_ID,
                "platform_gate": entry, "agent_gate": "group:" + GROUP_ID,
                "workspace_template_copied": True}

    def _check_enable_flow(self):
        snapshot, row = self._group_row()
        settings = copy.deepcopy(row["settings"])
        settings["enabled"] = False
        self.console({"action": "conversation.save", "conversation_id": row["id"],
                      "expected_revision": snapshot["revision"], "settings": settings})
        before_models = sum(item.get("event") == "model/start"
                            for item in self.request("GET", "/api/evidence?conversation_key=group:" + GROUP_ID)["lifecycle"])
        disabled = self.send_message(GROUP_ID, "disabled message must not wake Agent")
        time.sleep(0.6)
        assert not self.outputs(disabled)
        after_disabled = sum(item.get("event") == "model/start"
                             for item in self.request("GET", "/api/evidence?conversation_key=group:" + GROUP_ID)["lifecycle"])
        assert after_disabled == before_models

        snapshot, row = self._group_row()
        settings = copy.deepcopy(row["settings"])
        settings["enabled"] = True
        self.console({"action": "conversation.save", "conversation_id": row["id"],
                      "expected_revision": snapshot["revision"], "settings": settings})
        self.request("POST", "/api/scripts", {"conversation_key": "group:" + GROUP_ID,
                                                 "kind": "reply", "text": "CONSOLE_ENABLED_REPLY"})
        wake = self.send_message(GROUP_ID, "恢复后请回应这一条消息")
        output = self.wait(lambda: next((event for event in self.outputs(wake)), None),
                           "enabled group wake reply", timeout=60)
        self.wait(lambda: any(row["direction"] == "outbound" and
                              row["message_id"] == str(output["message_id"]) for row in self.rows()),
                  "enabled reply canonical in Journal")
        assert "CONSOLE_ENABLED_REPLY" in json.dumps(output, ensure_ascii=False)
        return {"disabled_no_model_or_reply": True, "new_wake_replied": True,
                "old_message_not_replayed": True}

    def _check_context_actions(self):
        _snapshot, row = self._group_row()
        before = self.request("GET", "/api/evidence?conversation_key=group:" + GROUP_ID)["states"]
        prior = next(item["session_id"] for item in before if item["conversation_key"] == "group:" + GROUP_ID)
        compact = self.console({"action": "compact", "conversation_id": row["id"]})
        assert compact.get("available") is True
        reset = self.console({"action": "new", "conversation_id": row["id"]})
        assert reset.get("reset") is True and reset["session_id"] != prior
        workspace = self.console({"action": "files.read", "conversation_id": row["id"],
                                  "path": "AGENTS.md"})
        assert workspace["content"] == "isolated console template\n"
        return {"compact": True, "new_session": reset["session_id"], "workspace_retained": True}

    def _check_model_command(self):
        event = self.request("POST", "/api/send", {
            "conversation": {"type": "private", "id": self.owner}, "sender_id": self.owner,
            "message": [{"type": "text", "data": {"text": "/provider sandbox-scripted scripted-alt"}}],
        })["event"]
        output = self.wait(lambda: next((item for item in self.outputs(event)), None),
                           "Owner /provider command", timeout=35)
        assert "已将本会话模型切换" in json.dumps(output, ensure_ascii=False)
        snapshot = self.console({"action": "snapshot"})
        owner = next(row for row in snapshot["conversations"]
                     if row["conversation_key"] == "private:" + self.owner)
        assert owner["settings"]["model"] == {"provider": "sandbox-scripted", "model": "scripted-alt"}
        return {"qq_provider_saved_to_console_store": True, "model": owner["settings"]["model"]}

    def _check_restart_persistence(self, template_id):
        _snapshot, row = self._group_row()
        self.stop()
        if self.launch_log:
            self.launch_log.close()
            self.launch_log = None
        self._launcher()
        self.wait(lambda: self.http("/api/state").get("connected"), "OneBot after isolated restart", timeout=90)
        self.wait(self._dashboard_ready, "Dashboard after isolated restart", timeout=90)
        self._login()
        snapshot = self.console({"action": "snapshot"})
        group = next(item for item in snapshot["conversations"]
                     if item["conversation_key"] == "group:" + GROUP_ID)
        assert group["template_id"] == template_id
        assert group["settings"]["model"] == {"provider": "sandbox-scripted", "model": "scripted"}
        assert group["settings"]["enabled"] is True
        file = self.console({"action": "files.read", "conversation_id": group["id"],
                             "path": "AGENTS.md"})
        assert file["content"] == "isolated console template\n"
        return {"session_settings_persisted": True, "template_and_group_home_persisted": True,
                "session_id": next(item["session_id"] for item in self.request(
                    "GET", "/api/evidence?conversation_key=group:" + GROUP_ID)["states"]
                                    if item["conversation_key"] == "group:" + GROUP_ID)}


def _child_main(directory: Path, dashboard_port: int) -> int:
    details = json.loads((directory / "sandbox-runtime.json").read_text(encoding="utf-8"))
    original = runtime._child_environment
    state_path = Path(details["astrbot_root"]) / "data/plugin_data/v2_console/state.json"
    service_config = Path(details["staged_repo"]) / ".runtime/agent-service.json"

    def child_environment(*, dsh_enabled: bool, **kwargs):
        env = original(dsh_enabled=dsh_enabled, **kwargs)
        if dsh_enabled:
            env["V2_CONSOLE_STATE_FILE"] = str(state_path)
            env["V2_CONSOLE_REPO_ROOT"] = details["staged_repo"]
            config = json.loads(service_config.read_text(encoding="utf-8"))
            admitted = config.get("allowed_conversations", [])
            if not isinstance(admitted, list) or any(not isinstance(item, str) for item in admitted):
                raise RuntimeError("staged Agent service admissions are invalid")
            env["V2_QQ_ALLOWED_CONVERSATIONS"] = ",".join(admitted)
            env["V2_SHARED_MODELS_PROFILE"] = str(directory / "models.patch.yml")
            env["V2_SHARED_MODELS_CREDENTIALS"] = str(directory / "credentials.yaml")
        return env

    runtime._child_environment = child_environment
    # Sandbox's supervisor intentionally supplies a fixed initial admission
    # string after building its environment. For this console persistence test
    # use the same freshly-loaded service config as production's launcher.
    original_popen = subprocess.Popen

    def launch(*args, **kwargs):
        env = kwargs.get("env")
        if env is not None and env.get("ASTRBOT_ROOT") == details["astrbot_root"]:
            config = json.loads(service_config.read_text(encoding="utf-8"))
            kwargs["env"] = {**env, "V2_QQ_ALLOWED_CONVERSATIONS": ",".join(config["allowed_conversations"])}
        return original_popen(*args, **kwargs)

    runtime.subprocess.Popen = launch
    return runtime._run(details, ASTRBOT_SOURCE, details["ports"], "scripted", owner_volume_mib=32)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=ROOT / ".runtime" / ("console-check-" + uuid.uuid4().hex[:12]))
    parser.add_argument("--launch-private", type=Path)
    parser.add_argument("--dashboard-port", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["V2_DSH_PROVIDER"] = "sandbox-scripted"
    os.environ["V2_DSH_MODEL"] = "scripted"
    if args.launch_private:
        if args.dashboard_port is None:
            raise SystemExit("--dashboard-port is required with --launch-private")
        return _child_main(args.launch_private, args.dashboard_port)
    result = ConsoleCheck(args.data_dir.absolute()).run_checks()
    print(json.dumps({"passed": result["passed"], "cases": len(result["cases"]),
                      "data_dir": str(args.data_dir)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
