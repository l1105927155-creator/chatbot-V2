#!/usr/bin/env python3
"""Isolated full-process acceptance for scheduled QQ file delivery.

Only the synthetic OneBot conversation group:91000 is contacted. Supports a
scripted DSH pass and a live-model pass using the configured private profile.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


send_check = load("workspace_task_file_send_base", ROOT / "scripts/check-workspace-task-send.py")
tasks = send_check.task_acceptance


def live_start(directory, config):
    """Build the same isolated launcher as WorkspaceTaskCheck with live model config."""
    import inspect
    import textwrap

    service_config = load("workspace_task_file_service", ROOT / "scripts/agent-service.py")
    config = service_config._load_config()
    os.environ.update(service_config._credentials(Path(config["credential_env_file"])))
    source = textwrap.dedent(inspect.getsource(tasks.WorkspaceTaskCheck.start))
    source = source.replace("'sandbox-scripted'", repr(config["provider"]))
    source = source.replace("result['V2_DSH_MODEL'] = 'scripted'",
                            "result['V2_DSH_MODEL'] = " + repr(config["model"]))
    source = source.replace("ports, 'scripted', '90009'", "ports, 'live', '90009'")
    source = source.replace("ports, 'scripted', owner_volume_mib", "ports, 'live', owner_volume_mib")
    extra = ""
    if config.get("shared_models_home"):
        models_home = Path(config["shared_models_home"])
        for key, path in (
            ("V2_SHARED_MODELS_PROFILE", models_home / "profiles/web/cordis.patch.yml"),
            ("V2_SHARED_MODELS_CREDENTIALS", models_home / ".credentials.yaml"),
        ):
            extra += '    ' + repr('        result[' + repr(key) + '] = ' + repr(str(path)) + "\n") + "\n"
    source = source.replace('    "    return result\\n"',
                            extra + '    "    return result\\n"')
    scope = dict(tasks.__dict__)
    exec(source, scope)
    return scope["start"], config


class ScheduledFileCheck(send_check.ScheduledSendCheck):
    def run_file_check(self, *, live=False):
        started = time.monotonic()
        result = {"passed": False, "mode": "live-model" if live else "scripted-native-provider",
                  "real_qq_connections": 0}
        try:
            self.start()
            state = self.state(self.group)
            marker = "SCHEDULED_FILE_" + uuid.uuid4().hex
            report_body = "# Scheduled file self-check\n\n" + marker + "\n"
            path = "reports/test.md"
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=150 if live else 75)).isoformat()
            if live:
                prompt = (
                    "请加载 scheduled-tasks Skill，然后创建一个一次性定时任务，执行时间为 " + run_at + "。"
                    "任务触发后，在当前群工作区创建 reports/test.md，写入一份简短自检报告，"
                    "正文必须包含唯一标记 " + marker + "。保存后读回文件核验标记，"
                    "然后用 qq_file_send(path='reports/test.md') 将该文件发到当前群，且只发一次。"
                    "不要用文本消息重复发送文件内容。任务名称为定时文件发送验收；现在仅登记任务，不要提前写文件。"
                )
            else:
                prompt = "请创建一次性定时任务，稍后在本群工作区写入自检文件并发送。"
            task_ids = []
            # Live mode creates one task through natural language and the real
            # Skill/MCP flow. In the deterministic pass, the foreground writes
            # the fixture and the actual scheduled turn exercises file delivery.
            if live:
                create_event = self.wake(self.group, prompt, sender=self.owner)
                create_output = self._complete(create_event, timeout=180)
                created = self._task_result(create_event, "task_manage")
                task = created["tasks"][0]
                assert task["registered"] and task["scheduled"]
                task_ids.append(task["task"]["id"])
            else:
                if state is None:
                    self.plan(self.group, kind="reply", text="SCHEDULED_FILE_FRONT_READY")
                    self._complete(self.wake(self.group, "初始化隔离群前台会话。", sender=self.owner))
                    state = self.state(self.group)
                assert state and state.get("session_id")
                task_prompt = "使用 qq_file_send(path='reports/test.md') 将当前群工作区里的自检报告发到本群；不要发额外文字。"
                self.plan(self.group, kind="tool", name="mcp__astrbot__task_manage", arguments={
                    "action": "create", "definition": {
                        "name": "定时文件交付验收", "kind": "agent", "enabled": True,
                        "schedule": {"type": "once", "run_at": run_at},
                        "prompt": task_prompt, "parameters": {},
                    },
                }, text_after="SCHEDULED_FILE_TASK_CREATED")
                create_event = self.wake(self.group, "请创建一次性自动任务：" + task_prompt, sender=self.owner)
                create_output = self._complete(create_event)
                assert "SCHEDULED_FILE_TASK_CREATED" in json.dumps(create_output, ensure_ascii=False)
                created = self._task_result(create_event, "task_manage")
                task = created["tasks"][0]
                assert task["registered"] and task["scheduled"]
                task_ids.append(task["task"]["id"])

            workspace = self._group_workspace()
            diary_file = workspace / path
            assert not diary_file.exists(), "task wrote the file before its scheduled time"
            if not live:
                self.plan(self.group, kind="tool", name="bash", arguments={
                    "command": "mkdir -p reports; printf '%s' "
                               + base64.b64encode(report_body.encode()).decode()
                               + " | base64 -d > reports/test.md; cat reports/test.md",
                    "description": "Create the file fixture before the scheduled send",
                    "workdir": ".",
                }, text_after="SCHEDULED_FILE_BASH_DONE")
                fixture_event = self.wake(self.group, "请在当前工作区创建并读回自检报告文件。", sender=self.owner)
                self._complete(fixture_event)
                assert diary_file.read_bytes() == report_body.encode("utf-8")
            front_before = dict(self.state(self.group))
            outbound_before = [row for row in self.events() if row["direction"] == "outbound"
                               and row["conversation"] == {"type": "group", "id": "91000"}]
            if not live:
                self.plan(self.group, kind="tool", name="mcp__astrbot__qq_file_send",
                          arguments={"path": path}, text_after="SCHEDULED_FILE_SEND_DONE")

            run_db = self.directory / "astrbot/data/plugin_data/v2_tasks/workspace-runs.sqlite3"
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                with sqlite3.connect(run_db) as db:
                    rows = [db.execute("SELECT status FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                                        (task_id,)).fetchone() for task_id in task_ids]
                terminal = [row[0] for row in rows if row]
                if any(status in {"failed", "cancelled", "interrupted"} for status in terminal):
                    raise AssertionError("scheduled file task ended with status " + repr(terminal))
                if len(terminal) == len(task_ids) and all(status == "completed" for status in terminal):
                    break
                time.sleep(.25)
            else:
                raise TimeoutError("scheduled file task did not complete")

            assert diary_file.is_file(), "scheduled task did not create reports/test.md"
            file_bytes = diary_file.read_bytes()
            assert marker.encode() in file_bytes
            if not live:
                assert file_bytes == report_body.encode("utf-8")
            time.sleep(1)
            outbound = [row for row in self.events() if row["direction"] == "outbound"
                        and row["conversation"] == {"type": "group", "id": "91000"}]
            assert len(outbound) == len(outbound_before) + 1, \
                "expected exactly one scheduled file event and no extra final text message"
            sent = outbound[-1]
            event = sent.get("event") or {}
            file_segments = [seg for seg in event.get("message", [])
                             if isinstance(seg, dict) and seg.get("type") == "file"]
            assert len(file_segments) == 1, ("outbound event lacks exactly one file segment", event)
            segment_data = file_segments[0].get("data") or {}
            assert segment_data.get("name") == "test.md", segment_data
            asset_id = segment_data.get("file_id")
            assert asset_id, segment_data
            asset_path = self.directory / "onebot/assets" / asset_id
            assert asset_path.is_file(), "synthetic OneBot did not persist the delivered file bytes"
            sent_bytes = asset_path.read_bytes()
            assert sent_bytes == file_bytes, "delivered file bytes differ from reports/test.md"

            rows = self.rows()
            journal = [row for row in rows if row["direction"] == "outbound"
                       and row["conversation_key"] == self.group
                       and str(row["message_id"]) == str(sent["message_id"])]
            assert len(journal) == 1, "Canonical Journal missing exactly one outbound file row"
            content = json.loads(journal[0]["content_json"])
            journal_files = [seg for seg in content.get("segments", [])
                             if isinstance(seg, dict) and seg.get("type") == "file"]
            assert len(journal_files) == 1 and journal_files[0].get("name") == "test.md", content
            assert journal[0]["actor_id"] == "99000"
            assert str(journal[0]["message_id"]) == str(sent["message_id"])

            after = self.state(self.group)
            for field in ("session_id", "last_seen_journal_id", "history_after_journal_id", "pending_upper_cursor"):
                assert after.get(field) == front_before.get(field), (field, front_before, after)
            evidence = self.evidence()
            lifecycle = evidence["lifecycle"]
            tool_starts = [row.get("tool") for row in lifecycle if row.get("event") == "tool/start"]
            assert tool_starts.count("mcp__astrbot__qq_file_send") == 1, tool_starts
            if live:
                assert "bash" in tool_starts, "live scheduled model did not use Bash"
            if live:
                assert "skill" in tool_starts, "live model did not load scheduled-tasks Skill"
            sessions = {row.get("session_id") for row in lifecycle if row.get("event") == "model/start"}
            assert len(sessions) >= 2, "scheduled task did not run in an independent DSH Session"
            result.update(
                passed=True, task_ids=task_ids,
                scheduled_sessions=len(sessions), file_tool_calls=tool_starts.count("mcp__astrbot__qq_file_send"),
                sent_message_id=str(sent["message_id"]), journal_message_id=str(journal[0]["message_id"]),
                journal_outbound_file_matches_platform=True, file_name="test.md",
                file_size=len(file_bytes), file_sha256=hashlib.sha256(file_bytes).hexdigest(),
                delivered_bytes_equal_workspace_file=True, scheduled_send_count=1,
                scheduled_final_message_count=0, frontend_sid_and_cursor_unchanged=True,
                synthetic_group="v2-sandbox/group:91000")
        except Exception as exc:
            result.update(error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        finally:
            if self.process:
                try:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                except Exception:
                    pass
            self.stop()
            assert not os.path.ismount(self.directory / "repo/.runtime/dsh-workspace-owner"), \
                "Owner volume remained mounted"
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        result["data_dir"] = str(self.directory)
        result["real_qq_connections"] = 0
        result["coverage_limits"] = [
            "Local fakeOneBot and synthetic group only; no production QQ group is contacted.",
            "Live-model result verifies the configured provider's tool choice only for this sample.",
        ]
        self.save("result.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.data_dir or ROOT / ".runtime" / ("workspace-task-file-" + uuid.uuid4().hex[:12])
    check = ScheduledFileCheck(directory)
    if args.mode == "live":
        start, config = live_start(directory, None)
        check.start = start.__get__(check, ScheduledFileCheck)
        check.provider = config["provider"]
        check.model = config["model"]
    result = check.run_file_check(live=args.mode == "live")
    print(json.dumps({"passed": result["passed"], "mode": args.mode,
                      "result": str(directory / "result.json")}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
