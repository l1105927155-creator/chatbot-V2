#!/usr/bin/env python3
"""Isolated ordinary-member QQ/DSH acceptance for group_app permissions."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE_GROUP = ROOT / ".runtime/group-storage/ed88b2bcc7f24c72d514c12e9cb70853fe5e0fdef9ea555a5417be1983ca87b1/workspace"
RELEASE = ROOT / ".runtime/astrbot/data/plugins/v2_dsh_router"
APP_RELEASE = ROOT / ".runtime/group-app-release-20261022"
OWNER = "1105927155"
MEMBER = "90001"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wake = load("group_app_wake", ROOT / "scripts/check-qq-wake.py")
runtime = wake.runtime
correlation = load("group_app_native_correlation", ROOT / "scripts/check-agent-sandbox.py")
sys.path.insert(0, str(ROOT / "dsh/plugins/agent-profile"))
import group_volume  # noqa: E402


class Check(wake.Check):
    def __init__(self, directory: Path, mode: str, source: Path):
        super().__init__(directory, RELEASE.resolve(), mode)
        self.source = source
        self.results = []
        self.key = "group:91000"
        self.probe_marker = None
        self.group_workspace = None
        self.other_group_workspace = None

    def save(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def setup_fixture(self, details):
        stage = Path(details["staged_repo"])
        # Use the installed Router as the base, preserving its deployed service.py;
        # overlay only the two source files under active review.
        target = stage / "astrbot-plugins/v2_dsh_router"
        shutil.rmtree(target)
        shutil.copytree(RELEASE.resolve(), target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("dsh_client.py", "runtime_audit.py"):
            shutil.copy2(ROOT / "astrbot-plugins/v2_dsh_router" / name, target / name)
        shutil.copytree(ROOT / "astrbot-plugins/v2_console", stage / "astrbot-plugins/v2_console",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        group_volume.prepare_group(stage / ".runtime/group-storage", "v2-sandbox", "99000", "91000")
        self.group_workspace = Path(group_volume.paths(
            stage / ".runtime/group-storage", "v2-sandbox", "99000", "91000")["workspace"])
        group_volume.prepare_group(stage / ".runtime/group-storage", "v2-sandbox", "99000", "91001")
        self.other_group_workspace = Path(group_volume.paths(
            stage / ".runtime/group-storage", "v2-sandbox", "99000", "91001")["workspace"])
        self.copy_workspace()
        self.setup_console(details)
        self.setup_models(stage)
        details.update({"v2_console_state_file": str(self.console_state),
                        "v2_shared_models_profile": str(self.model_profile),
                        "v2_shared_models_credentials": str(self.model_credentials)})
        self.save("sandbox-runtime.json", details)
        return details

    def copy_workspace(self):
        for name in ("AGENTS.md", "persona.md", "Owner.json", "apps/affinity/AGENTS.md",
                     "apps/affinity/affinity.py", "apps/affinity/test_affinity.py"):
            src = self.source / name
            dst = self.group_workspace / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        skill = self.group_workspace / "skills/affinity/SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(APP_RELEASE / "affinity-SKILL.md", skill)
        app = self.group_workspace / "apps/affinity"
        shutil.copy2(APP_RELEASE / "affinity-app.json", app / "app.json")
        data = app / "data"
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        (data / "members.json").write_text('{"version":1,"members":{}}\n', encoding="utf-8")
        (data / "members.json").chmod(0o600)
        # An app operation used to prove that code and sibling workspaces stay read-only.
        probe = self.group_workspace / "apps/scope_probe"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "app.json").write_text(json.dumps({"version": 1, "entrypoint": "probe.py",
            "operations": ["check"]}) + "\n", encoding="utf-8")
        (probe / "probe.py").write_text('''import json, pathlib, sys
op, marker = sys.argv[1:]
code = pathlib.Path("/workspace/apps/affinity/affinity.py")
before = code.read_bytes()
try:
    code.write_bytes(b"tampered")
    code_denied = False
except OSError:
    code_denied = True
other = pathlib.Path(marker)
try:
    other.write_text("escaped")
    cross_denied = False
except OSError:
    cross_denied = True
print(json.dumps({"code_denied": code_denied, "cross_denied": cross_denied,
                  "code_unchanged": code.read_bytes() == before}))
''', encoding="utf-8")
        self.code_hash = self.digest(app / "affinity.py")
        self.probe_marker = self.other_group_workspace / ".private-cross-group-sentinel"
        self.probe_marker.write_text("private-sentinel", encoding="utf-8")
        self.marker_hash = self.digest(self.probe_marker)

    def setup_console(self, details):
        store_path = Path(details["astrbot_root"]) / "data/plugin_data/v2_console/state.json"
        store_module = load("group_app_console_store", ROOT / "astrbot-plugins/v2_console/store.py")
        store = store_module.ConsoleStore(store_path)
        snapshot = store.read()
        route = ({"provider": "sandbox-scripted", "model": "scripted"} if self.mode == "scripted"
                 else {"provider": "wb", "model": "deepseek-v4.1-flash"})
        state = store.update_defaults({"model": route,
                                       "enabled": True, "editors": []},
                                      expected_revision=snapshot["revision"])
        state = store.create_session("v2-sandbox", "99000", self.key,
                                     expected_revision=state["revision"])["state"]
        self.console_state = store_path
        session = state["sessions"][store_module.ConsoleStore.session_key(
            {"platform_id": "v2-sandbox", "bot_id": "99000", "conversation_key": self.key})]["settings"]
        assert session == {"model": route, "enabled": True, "editors": []}

    def setup_models(self, stage):
        import yaml
        shared_home = Path(json.loads((ROOT / ".runtime/agent-service.json").read_text())["shared_models_home"])
        target = stage / ".runtime/group-app-models"
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(shared_home / "profiles/web/cordis.patch.yml", target / "cordis.patch.yml")
        source = yaml.safe_load((shared_home / ".credentials.yaml").read_text())
        private = {"version": source["version"], "records": {},
                   "refs": {"WB_API_KEY": source["refs"]["WB_API_KEY"]}}
        cred = target / ".credentials.yaml"
        cred.write_text(yaml.safe_dump(private), encoding="utf-8")
        cred.chmod(0o600)
        self.model_profile = target / "cordis.patch.yml"
        self.model_credentials = cred

    def start(self):
        ports = {"onebot": self.onebot, "ui": self.ui, "mcp": self.mcp}
        details = runtime.prepare(self.directory, ROOT / ".runtime/astrbot", ports,
                                  self.mode, OWNER)
        self.setup_fixture(details)
        self.log = (self.directory / "runtime.log").open("ab")
        self.process = subprocess.Popen([str(ROOT / ".runtime/astrbot/.venv/bin/python"),
            str(Path(__file__).resolve()), "--launch-private", str(self.directory), "--mode", self.mode],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"), "isolated OneBot connected", timeout=90)
        def ready():
            from urllib.request import urlopen
            with urlopen(f"http://127.0.0.1:{self.mcp}/healthz", timeout=3) as response:
                return json.load(response)["ready"]
        self.wait(ready, "isolated DSH ready", timeout=90)
        self.workspace = Path(details["staged_repo"])

    def complete(self, event):
        self.wait(lambda: any(r["event"] in {"turn/completed", "turn/failed"} for r in self.trace(event)),
                  "turn terminal", timeout=210)
        outs = self.outputs(event)
        ids = {str(r["message_id"]) for r in outs}
        self.wait(lambda: ids <= {str(r["message_id"]) for r in self.rows()}, "canonical outbound journal")
        assert outs, "turn emitted no QQ response"
        return outs

    def affinity_data(self):
        return json.loads((self.group_workspace / "apps/affinity/data/members.json").read_text())

    def outputs(self, event):
        return [row for row in self.events() if row["direction"] == "outbound"
                and row["conversation"] == event["conversation"]
                and int(row["message_id"]) > int(event["message_id"])]

    def successful_group_app_calls(self, event):
        helper = correlation.Acceptance.__new__(correlation.Acceptance)
        helper.directory = self.directory
        helper.trace = self.trace
        return helper.native_tools(event, "group_app")

    def case(self, name, action):
        started = time.monotonic()
        try:
            details = action() or {}
            item = {"case": name, "passed": True, "details": details}
        except Exception as exc:
            item = {"case": name, "passed": False, "error_type": type(exc).__name__,
                    "error": str(exc), "traceback": traceback.format_exc()}
        item["elapsed_seconds"] = round(time.monotonic() - started, 2)
        self.results.append(item)
        self.save("progress.json", self.results)
        try:
            evidence_dir = self.directory / "evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.save(f"evidence/{name}.json", self.request("GET", "/api/evidence/export"))
        except Exception as exc:
            item["evidence_error"] = f"{type(exc).__name__}: {exc}"
            item["passed"] = False
            self.save("progress.json", self.results)
        print(json.dumps({k: v for k, v in item.items() if k != "traceback"}, ensure_ascii=False), flush=True)
        return item["passed"]

    def scripted(self):
        before = self.digest(self.group_workspace / "apps/affinity/affinity.py")
        def write_and_verify():
            self.plan(self.key, kind="tool", name="group_app", arguments={"app": "affinity", "operation": "record",
                "args": [MEMBER, "--name", "普通成员", "--delta", "2", "--reason", "示例互动", "--evidence", "隔离验收"]},
                text_after="已完成本轮记录。")
            event = self.wake(self.key, "请帮我看看这个文件是否需要更新。", sender=MEMBER)
            out = self.complete(event)
            data = self.affinity_data()
            member = data["members"].get(MEMBER)
            assert member and int(member.get("score", 0)) == 2, data
            assert self.digest(self.group_workspace / "apps/affinity/affinity.py") == before
            pairs = self.successful_group_app_calls(event)
            assert len(pairs) == 1, pairs
            result_text = "".join(block.get("text", "") for block in pairs[0]["result"]["message"].get("content", [])
                                  if block.get("type") == "text")
            lines = result_text.strip().splitlines()
            result = json.loads(lines[0])
            assert lines[-1] == "exitCode=0" and result["ok"] is True, result_text
            return {"sender": MEMBER, "editors": [], "app_persisted": True, "score": 2,
                    "successful_native_result": True, "canonical_reply": True, "outbound_count": len(out)}
        self.case("ordinary-member-business-data-write", write_and_verify)

        def denied_reset():
            before_data = self.affinity_data()
            self.plan(self.key, kind="tool", name="group_app", arguments={"app": "affinity", "operation": "reset", "args": [MEMBER]},
                      text_after="未执行未开放的重置操作。")
            event = self.wake(self.key, "请检查是否可以重置数据。", sender=MEMBER)
            outs = self.complete(event)
            assert self.affinity_data() == before_data
            trace = self.trace(event)
            assert any(r["event"] == "tool/start" and r.get("tool") == "group_app" for r in trace), trace
            return {"undeclared_reset_rejected": True, "data_unchanged": True, "canonical_reply": True, "outbound_count": len(outs)}
        self.case("undeclared-reset-is-rejected", denied_reset)

        def scope_probe():
            data_before = self.affinity_data()
            code_before = self.digest(self.group_workspace / "apps/affinity/affinity.py")
            marker_before = self.digest(self.probe_marker)
            self.plan(self.key, kind="tool", name="group_app", arguments={"app": "scope_probe", "operation": "check",
                      "args": [str(self.probe_marker)]}, text_after="隔离检查完成。")
            event = self.wake(self.key, "请做一次应用隔离检查。", sender=MEMBER)
            outs = self.complete(event)
            # The test tool's result may have been delivered; the on-disk invariants are authoritative.
            assert self.digest(self.group_workspace / "apps/affinity/affinity.py") == code_before
            assert self.digest(self.probe_marker) == marker_before
            assert self.affinity_data() == data_before
            tools = [r for r in self.trace(event) if r["event"] == "tool/start" and r.get("tool") == "group_app"]
            assert tools, self.trace(event)
            return {"app_code_unchanged": True, "sibling_group_path_unwritable": True,
                    "data_unchanged": True, "canonical_reply": True, "outbound_count": len(outs)}
        self.case("group-app-cannot-write-app-code-or-sibling-path", scope_probe)

    def live_observation(self):
        texts = ["小v，今天有点累，不过跟你聊了几句心情轻松多了，谢谢你愿意听我说。"]
        observed = []
        for text in texts:
            event = self.wake(self.key, text, sender=MEMBER)
            outs = self.complete(event)
            native = self.successful_group_app_calls(event)
            app_calls = []
            for pair in native:
                arguments = json.loads(pair["call"]["arguments"])
                result_text = "".join(block.get("text", "") for block in pair["result"]["message"].get("content", [])
                                      if block.get("type") == "text")
                lines = result_text.strip().splitlines()
                result = json.loads(lines[0])
                assert lines[-1] == "exitCode=0" and result["ok"] is True, result_text
                app_calls.append({"operation": arguments["operation"], "args": arguments["args"],
                                  "result": result})
            observed.append({"message": text, "outbound_count": len(outs), "group_app_calls": app_calls,
                             "data": self.affinity_data()})
        operations = [call["operation"] for row in observed for call in row["group_app_calls"]]
        assert "get" in operations and "record" in operations, observed
        member = observed[-1]["data"]["members"].get(MEMBER)
        records = [call["result"] for row in observed for call in row["group_app_calls"]
                   if call["operation"] == "record" and call["result"].get("action") == "record"]
        assert member and records and int(records[-1]["score_after"]) == int(member["score"]), observed
        self.case("live-normal-chat-autonomous-affinity-maintenance", lambda: {
            "provider": "wb", "model": "deepseek-v4.1-flash", "ordinary_member": MEMBER,
            "editors": [], "autonomous_get_and_record": True, "persisted_score": member["score"],
            "successful_group_app_calls": len(operations), "observations": observed})

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def run_checks(self):
        try:
            self.start()
            if self.mode == "scripted":
                self.scripted()
            else:
                self.live_observation()
        except Exception as exc:
            self.results.append({"case": "fatal", "passed": False, "error_type": type(exc).__name__,
                                 "error": str(exc), "traceback": traceback.format_exc()})
        finally:
            if self.process:
                try:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                except Exception:
                    pass
            self.stop()
            if self.other_group_workspace and os.path.ismount(self.other_group_workspace):
                group_volume.unmount_group(self.other_group_workspace)
            if self.group_workspace and os.path.ismount(self.group_workspace):
                group_volume.unmount_group(self.group_workspace)
        result = {"passed": bool(self.results) and all(item["passed"] for item in self.results),
                  "mode": self.mode, "cases": self.results, "real_qq_connections": 0,
                  "ordinary_sender": MEMBER, "workspace_editors": [], "release": str(RELEASE.resolve())}
        self.save("result.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--source-workspace", type=Path, default=SOURCE_GROUP)
    parser.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    parser.add_argument("--launch-private", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "scripted":
        os.environ["V2_DSH_PROVIDER"] = "sandbox-scripted"
        os.environ["V2_DSH_MODEL"] = "scripted"
    else:
        os.environ["V2_DSH_PROVIDER"] = "wb"
        os.environ["V2_DSH_MODEL"] = "deepseek-v4.1-flash"
    if args.launch_private:
        details = json.loads((args.launch_private / "sandbox-runtime.json").read_text())
        original = runtime._child_environment
        def child_environment(*, dsh_enabled, **kwargs):
            env = original(dsh_enabled=dsh_enabled, **kwargs)
            if dsh_enabled:
                env.update({"V2_CONSOLE_STATE_FILE": details["v2_console_state_file"],
                            "V2_SHARED_MODELS_PROFILE": details["v2_shared_models_profile"],
                            "V2_SHARED_MODELS_CREDENTIALS": details["v2_shared_models_credentials"],
                            "V2_DSH_PROVIDER": "sandbox-scripted" if args.mode == "scripted" else "wb",
                            "V2_DSH_MODEL": "scripted" if args.mode == "scripted" else "deepseek-v4.1-flash"})
                env.pop("DEEPSEEK_API_KEY", None)
            return env
        runtime._child_environment = child_environment
        return runtime._run(details, ROOT / ".runtime/astrbot", details["ports"], args.mode, owner_volume_mib=32)
    directory = args.data_dir or ROOT / ".runtime" / ("qq-group-app-" + uuid.uuid4().hex[:12])
    result = Check(directory.absolute(), args.mode, args.source_workspace.resolve()).run_checks()
    print(json.dumps({"passed": result["passed"], "mode": args.mode, "cases": len(result["cases"]),
                      "directory": str(directory)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
