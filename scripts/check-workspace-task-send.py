#!/usr/bin/env python3
"""Verify a scheduled task may send one message into its synthetic QQ group."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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


task_acceptance = load("workspace_task_send_base", ROOT / "scripts/check-workspace-tasks.py")


class ScheduledSendCheck(task_acceptance.WorkspaceTaskCheck):
    def _task_result(self, event, tool):
        pairs = self.correlated(event, "mcp__astrbot__" + tool)
        assert len(pairs) == 1, (tool, len(pairs))
        payload = "".join(block.get("text", "")
                          for block in pairs[0]["result"]["message"].get("content", [])
                          if block.get("type") == "text")
        return json.loads(payload)

    def run_send_check(self):
        started = time.monotonic()
        result = {"passed": False, "mode": "scripted-native-provider", "real_qq_connections": 0}
        try:
            self.start()
            state = self.state(self.group)
            if state is None:
                self.plan(self.group, kind="reply", text="SCHEDULED_SEND_FRONT_READY")
                self._complete(self.wake(self.group, "初始化隔离群前台会话。", sender=self.owner))
                state = self.state(self.group)
            assert state and state.get("session_id")
            text = "SCHEDULED_SEND_" + uuid.uuid4().hex
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            self.plan(self.group, kind="tool", name="mcp__astrbot__task_manage", arguments={
                "action": "create", "definition": {
                    "name": "隔离群发验收", "kind": "agent", "enabled": True,
                    "schedule": {"type": "once", "run_at": run_at},
                    "prompt": "向当前群发送任务完成通知。",
                    "parameters": {},
                },
            }, text_after="SCHEDULED_SEND_TASK_CREATED")
            created_event = self.wake(self.group, "创建隔离群发验收任务。", sender=self.owner)
            created_reply = self._complete(created_event)
            assert "SCHEDULED_SEND_TASK_CREATED" in json.dumps(created_reply, ensure_ascii=False)
            created = self._task_result(created_event, "task_manage")
            task_id = created["tasks"][0]["task"]["id"]
            assert created["tasks"][0]["registered"] and created["tasks"][0]["scheduled"]
            front_before = dict(self.state(self.group))
            outbound_before = [row for row in self.events() if row["direction"] == "outbound"
                               and row["conversation"] == {"type": "group", "id": "91000"}]

            # Queue the send for the next actual DSH model turn. The scheduled
            # task receives a fresh SID and a no-send flag on its MCP turn.
            self.plan(self.group, kind="tool", name="mcp__astrbot__qq_send_origin",
                      arguments={"text": text}, text_after="SCHEDULED_SEND_TOOL_DONE")
            run_db = self.directory / "astrbot/data/plugin_data/v2_tasks/workspace-runs.sqlite3"
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                outbound = [row for row in self.events() if row["direction"] == "outbound"
                            and row["conversation"] == {"type": "group", "id": "91000"}]
                if len(outbound) > len(outbound_before):
                    break
                if self.process and self.process.poll() is not None:
                    raise AssertionError("isolated runtime exited before scheduled send")
                time.sleep(.2)
            else:
                raise TimeoutError("APScheduler did not send the scheduled MCP message")
            time.sleep(2)
            outbound = [row for row in self.events() if row["direction"] == "outbound"
                        and row["conversation"] == {"type": "group", "id": "91000"}]
            assert len(outbound) == len(outbound_before) + 1, outbound
            sent = outbound[-1]
            assert text in json.dumps(sent, ensure_ascii=False), sent
            journal = self.rows()
            matching = [row for row in journal if row["direction"] == "outbound"
                        and row["conversation_key"] == self.group
                        and text in json.dumps(row, ensure_ascii=False)]
            assert len(matching) == 1, matching
            assert str(matching[0]["message_id"]) == str(sent["message_id"]), (matching[0], sent)
            assert matching[0]["actor_id"] == "99000"
            after = self.state(self.group)
            for field in ("session_id", "last_seen_journal_id", "history_after_journal_id",
                          "pending_upper_cursor"):
                assert after.get(field) == front_before.get(field), (field, front_before, after)
            with sqlite3.connect(run_db) as db:
                run = db.execute("SELECT status FROM runs WHERE task_id=?", (task_id,)).fetchone()
            assert run == ("completed",), run
            # Scheduled sessions deliberately have no frontend conversation
            # state row, so inspect the global native lifecycle stream.
            evidence = self.evidence()
            scheduled_ids = {row.get("session_id") for row in evidence["lifecycle"]
                             if row.get("event") == "model/start"
                             and row.get("session_id") != front_before["session_id"]}
            assert scheduled_ids, "scheduled send did not use an independent DSH Session"
            result.update(passed=True, provider="sandbox-scripted", model="scripted",
                          task_id=task_id, scheduled_dsh_sessions=len(scheduled_ids),
                          sent_message_id=str(sent["message_id"]),
                          journal_message_id=str(matching[0]["message_id"]),
                          journal_outbound_matches_platform_event=True,
                          scheduled_send_count=1, scheduled_final_message_count=0,
                          frontend_sid_and_cursor_unchanged=True,
                          synthetic_group="v2-sandbox/group:91000")
        except Exception as exc:
            result.update(error_type=type(exc).__name__, error=str(exc),
                          traceback=traceback.format_exc())
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
        result["coverage_limits"] = [
            "Scripted DSH verifies scheduled tool delivery and Journal consistency, not model choice.",
            "OneBot is local and synthetic; no real QQ group or recipient is contacted.",
        ]
        self.save("result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="isolated runtime output; defaults under .runtime")
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.data_dir or ROOT / ".runtime" / ("workspace-task-send-" + uuid.uuid4().hex[:12])
    check = ScheduledSendCheck(directory)
    report = check.run_send_check()
    print(json.dumps({"passed": report["passed"], "result": str(directory / "result.json")}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)
