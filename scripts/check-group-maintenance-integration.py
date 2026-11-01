#!/usr/bin/env python3
"""Exercise Owner group maintenance through isolated AstrBot, Router, MCP and tasks."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "group_maintenance_tasks_base", ROOT / "scripts/check-console-tasks.py")
assert _spec and _spec.loader
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

TaskConsoleCheck = _base.TaskConsoleCheck
GROUP_ID = "91000"
GROUP_KEY = "group:" + GROUP_ID
OWNER_KEY = "private:90009"
MAINTENANCE_TOOL = "mcp__astrbot__group_maintenance"


class GroupMaintenanceCheck(TaskConsoleCheck):
    def private_owner_message(self, text: str):
        result = self.request("POST", "/api/send", {
            "conversation": {"type": "private", "id": self.owner},
            "sender_id": self.owner,
            "message": [{"type": "text", "data": {"text": text}}],
        })
        event = result["event"]
        message_id = str(event["message_id"])
        self.wait(lambda: any(str(row["message_id"]) == message_id and
                              row["conversation_key"] == OWNER_KEY for row in self.rows()),
                  "Owner request entered canonical Journal")
        return event

    def invoke_owner_maintenance(self, action: str, group_id: str = GROUP_ID):
        marker = "MAINTENANCE_" + action.upper() + "_TOOL_RETURN"
        self.request("POST", "/api/scripts", {
            "conversation_key": OWNER_KEY,
            "kind": "tool",
            "name": MAINTENANCE_TOOL,
            "arguments": {"action": action, "group_id": group_id},
            "text_after": marker,
        })
        inbound = self.private_owner_message(
            f"Run the configured maintenance action {action} for isolated group {group_id}.")
        output = self.wait(
            lambda: next((item for item in self.outputs(inbound)), None),
            f"Owner scripted {action} call completed", timeout=45)
        self.wait(lambda: marker in json.dumps(output, ensure_ascii=False),
                  f"Owner scripted {action} final response")
        evidence = self.request("GET", "/api/evidence?conversation_key=" + OWNER_KEY)
        tool_rows = [row for row in evidence.get("lifecycle", [])
                     if row.get("event") in {"tool/start", "tool/end"}
                     and row.get("tool") == MAINTENANCE_TOOL]
        self.wait(lambda: len([row for row in self.request(
            "GET", "/api/evidence?conversation_key=" + OWNER_KEY).get("lifecycle", [])
            if row.get("event") == "tool/end" and row.get("tool") == MAINTENANCE_TOOL]) >= 1,
            f"actual MCP {action} tool reached terminal event", timeout=20)
        return {"owner_message_id": str(inbound["message_id"]),
                "mcp_tool_events": len(tool_rows), "final_marker": marker}

    def owner_group_command(self, text: str, expected: str, *, timeout: int = 20):
        """Run a deterministic slash command as the configured Owner inside the group."""
        before = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
        model_starts = sum(row.get("event") == "model/start"
                           for row in before.get("lifecycle", []))
        inbound = self.send_message(GROUP_ID, text, sender=self.owner, mention=False)
        output = self.wait(lambda: next(iter(self.outputs(inbound)), None),
                           f"Owner group command {text}", timeout=timeout)
        rendered = json.dumps(output, ensure_ascii=False)
        assert expected in rendered, (text, expected, output)
        after = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
        model_starts_after = sum(row.get("event") == "model/start"
                                 for row in after.get("lifecycle", []))
        assert model_starts_after == model_starts, f"{text} unexpectedly dispatched a model turn"
        assert any(str(row["message_id"]) == str(output["message_id"])
                   and row["direction"] == "outbound" for row in self.rows())
        return inbound, output, rendered

    def group_console_row(self):
        snapshot = self.console({"action": "snapshot"})
        row = next(item for item in snapshot["conversations"]
                   if item["conversation_key"] == GROUP_KEY)
        return snapshot, row

    def task_group(self, snapshot):
        return self.group_tasks(snapshot, GROUP_ID)

    def ensure_running_configuration(self):
        snapshot, row = self.group_console_row()
        settings = dict(row["settings"])
        if settings.get("enabled") is not True:
            settings["enabled"] = True
            self.console({"action": "conversation.save", "conversation_id": row["id"],
                          "expected_revision": snapshot["revision"], "settings": settings})
        tasks = self.task_snapshot()
        group = self.task_group(tasks)
        if group["enabled"] is not True:
            self.task_dispatch("tasks.policy", {
                "identity": list(group["identity"]), "enabled": True,
                "expected_revision": tasks["policy"]["revision"],
            })
        # The policy may have just enrolled this group's task manifest.
        return self.task_group(self.task_snapshot())

    def maintenance_state(self):
        path = Path(self.details["astrbot_root"]) / "data/plugin_data/v2_dsh_router/group-maintenance.json"
        if not path.exists():
            return {"path": str(path), "groups": {}}
        return {"path": str(path), **json.loads(path.read_text(encoding="utf-8"))}

    def run_checks(self):
        try:
            self.start()
            group = self.ensure_running_configuration()
            assert group["available"] is True
            self.case("isolated-stack-ready", lambda: {
                "owner": OWNER_KEY, "group": GROUP_KEY,
                "router_plugin": True, "mcp_endpoint": True,
                "workspace_tasks_available": group["available"],
                "real_qq_connections": 0,
            })

            # First prove a real foreground turn is cancelled and drained by the
            # Owner→MCP maintenance operation. The injected delay is in the
            # sandbox provider and is cancelled through the actual DSH signal.
            self.request("POST", "/api/faults", {
                "target": "model", "kind": "timeout", "conversation_key": GROUP_KEY,
                "phase": "before", "nth": 1, "delay_ms": 45000,
            })
            cursor = self.request("GET", "/api/events")["cursor"]
            group_input = self.send_message(GROUP_ID, "start a deliberately delayed foreground turn")
            started = self.wait(lambda: next((row for row in self.request(
                "GET", "/api/evidence?conversation_key=" + GROUP_KEY).get("lifecycle", [])
                if row.get("event") == "model/start" and row.get("conversation_key") == GROUP_KEY), None),
                "foreground model entered delayed fixture", timeout=25)
            paused_call = self.invoke_owner_maintenance("pause")
            paused_snapshot, paused_row = self.group_console_row()
            paused_tasks = self.task_snapshot()
            paused_group_tasks = self.task_group(paused_tasks)
            persisted_maintenance = self.maintenance_state()
            assert paused_row["settings"]["enabled"] is False
            assert paused_group_tasks["enabled"] is False
            assert persisted_maintenance["groups"]
            record = next(iter(persisted_maintenance["groups"].values()))
            assert "agent_disabled_and_drained" in record["completed"]
            assert "workspace_tasks_disabled_and_drained" in record["completed"]
            assert record["pause_journal_id"] >= 0
            journal_rows = self.rows()
            assert any(str(row["message_id"]) == str(group_input["message_id"])
                       and row["conversation_key"] == GROUP_KEY for row in journal_rows)
            group_evidence = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
            group_state = next((row for row in group_evidence.get("states", [])
                                if row.get("conversation_key") == GROUP_KEY), None)
            assert group_state is not None
            cancellations = [row for row in group_evidence.get("lifecycle", [])
                             if row.get("event") in {"cancel/requested", "dsh/stop"}]
            assert any(row.get("event") == "cancel/requested" for row in cancellations)
            assert any(row.get("event") == "dsh/stop" and row.get("stop_reason") == "cancelled"
                       for row in cancellations)
            assert record["in_progress"] == []
            self.case("owner-mcp-pause-persists-and-drains-foreground", lambda: {
                **paused_call, "model_session_id": started.get("session_id"),
                "agent_enabled": paused_row["settings"]["enabled"],
                "task_policy_enabled": paused_group_tasks["enabled"],
                "foreground_cancellation_events": [row["event"] for row in cancellations],
                "foreground_pending_cursor": group_state.get("pending_upper_cursor"),
                "pause_record_completed": record["completed"],
                "console_revision": paused_snapshot["revision"],
                "task_policy_revision": paused_tasks["policy"]["revision"],
                "pause_journal_id": record["pause_journal_id"],
            })

            # Disabled traffic remains in Journal but does not dispatch a model.
            model_starts_before = len([row for row in group_evidence.get("lifecycle", [])
                                       if row.get("event") == "model/start"])
            ignored = self.send_message(GROUP_ID, "this paused message must not dispatch")
            time.sleep(0.8)
            after = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
            model_starts_after = len([row for row in after.get("lifecycle", [])
                                      if row.get("event") == "model/start"])
            assert model_starts_after == model_starts_before
            assert any(str(row["message_id"]) == str(ignored["message_id"])
                       and row["conversation_key"] == GROUP_KEY for row in self.rows())
            self.case("paused-group-journals-without-agent-dispatch", lambda: {
                "journaled_inbound": str(ignored["message_id"]),
                "model_starts_before_after": [model_starts_before, model_starts_after],
            })

            blocked_resume = self.invoke_owner_maintenance("resume")
            blocked_snapshot, blocked_row = self.group_console_row()
            blocked_tasks = self.task_snapshot()
            blocked_task_group = self.task_group(blocked_tasks)
            blocked_record = next(iter(self.maintenance_state()["groups"].values()))
            assert blocked_row["settings"]["enabled"] is False
            assert blocked_task_group["enabled"] is False
            assert "agent_state_restored" not in blocked_record["completed"]
            status_message, status_output, status_text = self.owner_group_command(
                "/v2agent status", "待核对 Journal：")
            cursor_match = re.search(r"待核对 Journal[：:]\s*(\d+)", status_text)
            assert cursor_match, status_text
            pending_cursor = int(cursor_match.group(1))
            group_evidence = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
            group_state = next((row for row in group_evidence.get("states", [])
                                if row.get("conversation_key") == GROUP_KEY), None)
            assert group_state and group_state.get("pending_upper_cursor") == pending_cursor
            self.case("resume-stays-blocked-until-owner-reviews-pending", lambda: {
                **blocked_resume,
                "agent_remains_disabled": blocked_row["settings"]["enabled"] is False,
                "tasks_remain_disabled": blocked_task_group["enabled"] is False,
                "maintenance_record_retained": bool(self.maintenance_state()["groups"]),
                "pending_journal_cursor": pending_cursor,
                "status_message_id": str(status_message["message_id"]),
                "status_output_id": str(status_output["message_id"]),
                "console_revision": blocked_snapshot["revision"],
                "task_policy_revision": blocked_tasks["policy"]["revision"],
            })
            recover_message, recover_output, _ = self.owner_group_command(
                f"/v2agent recover {pending_cursor}", "已解除暂停并跳过截至 Journal " + str(pending_cursor))
            self.case("owner-exact-recovery-clears-pending-before-resume", lambda: {
                "recovered_cursor": pending_cursor,
                "recover_message_id": str(recover_message["message_id"]),
                "recover_output_id": str(recover_output["message_id"]),
            })

            resumed_call = self.invoke_owner_maintenance("resume")
            resumed_snapshot, resumed_row = self.group_console_row()
            resumed_tasks = self.task_snapshot()
            resumed_group_tasks = self.task_group(resumed_tasks)
            assert resumed_row["settings"]["enabled"] is True
            assert resumed_group_tasks["enabled"] is True
            assert self.maintenance_state()["groups"] == {}
            assert resumed_tasks["policy"]["default_enabled"] is False
            self.case("owner-mcp-resume-restores-prior-persistent-state", lambda: {
                **resumed_call, "agent_enabled": resumed_row["settings"]["enabled"],
                "group_task_enabled": resumed_group_tasks["enabled"],
                "default_task_policy_unchanged": resumed_tasks["policy"]["default_enabled"],
                "maintenance_record_cleared": True,
                "console_revision": resumed_snapshot["revision"],
                "task_policy_revision": resumed_tasks["policy"]["revision"],
            })

            # A fresh wake after resume must be handled. This also proves old
            # paused inputs were not replayed by the maintenance operation.
            self.request("POST", "/api/scripts", {"conversation_key": GROUP_KEY,
                "kind": "reply", "text": "MAINTENANCE_RESUMED_NEW_WAKE"})
            wake = self.send_message(GROUP_ID, "respond to the new post-resume message")
            try:
                output = self.wait(lambda: next((row for row in self.outputs(wake)), None),
                                   "new post-resume group wake", timeout=12)
                assert "MAINTENANCE_RESUMED_NEW_WAKE" in json.dumps(output, ensure_ascii=False)
                self.case("resume-processes-new-wake-only", lambda: {
                    "new_inbound_id": str(wake["message_id"]),
                    "output_id": str(output["message_id"]), "paused_messages_replayed": False,
                })
            except Exception as error:
                failed_turns = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
                failed = next((row for row in failed_turns.get("lifecycle", [])
                               if row.get("event") == "turn/failed"
                               and str(row.get("inbound_message_id")) == str(wake["message_id"])), None)
                self.result_cases.append({"case": "resume-processes-new-wake-only", "passed": False,
                    "error": str(error)[:500], "inbound_id": str(wake["message_id"]),
                    "turn_failure": failed})
                print("FAIL resume-processes-new-wake-only: " + str(error), flush=True)

            # Restart only this isolated sandbox and verify durable console and
            # task state survives an actual process restart.
            self.stop()
            if self.launch_log:
                self.launch_log.close()
                self.launch_log = None
            self._launcher()
            self.wait(lambda: self.http("/api/state").get("connected"),
                      "isolated OneBot after restart", timeout=90)
            self.wait(self._dashboard_ready, "isolated dashboard after restart", timeout=90)
            self._login()
            after_restart, restart_row = self.group_console_row()
            restart_tasks = self.task_snapshot()
            assert restart_row["settings"]["enabled"] is True
            assert self.task_group(restart_tasks)["enabled"] is True
            self.case("resumed-console-and-task-policies-survive-restart", lambda: {
                "agent_enabled": restart_row["settings"]["enabled"],
                "task_policy_enabled": self.task_group(restart_tasks)["enabled"],
                "default_policy": restart_tasks["policy"]["default_enabled"],
                "console_revision": after_restart["revision"],
            })

            self.request("POST", "/api/scripts", {"conversation_key": GROUP_KEY,
                "kind": "reply", "text": "MAINTENANCE_RESTARTED_WAKE"})
            wake_after_restart = self.send_message(GROUP_ID, "respond after isolated restart")
            try:
                output_after_restart = self.wait(
                    lambda: next((row for row in self.outputs(wake_after_restart)), None),
                    "post-restart group wake", timeout=45)
                assert "MAINTENANCE_RESTARTED_WAKE" in json.dumps(output_after_restart, ensure_ascii=False)
                self.case("resumed-group-processes-new-wake-after-restart", lambda: {
                    "new_inbound_id": str(wake_after_restart["message_id"]),
                    "output_id": str(output_after_restart["message_id"]),
                })
            except Exception as error:
                failed_turns = self.request("GET", "/api/evidence?conversation_key=" + GROUP_KEY)
                failed = next((row for row in failed_turns.get("lifecycle", [])
                               if row.get("event") == "turn/failed"
                               and str(row.get("inbound_message_id")) == str(wake_after_restart["message_id"])), None)
                self.result_cases.append({"case": "resumed-group-processes-new-wake-after-restart",
                    "passed": False, "error": str(error)[:500],
                    "inbound_id": str(wake_after_restart["message_id"]), "turn_failure": failed})
                print("FAIL resumed-group-processes-new-wake-after-restart: " + str(error), flush=True)

            # An idle group has no foreground run to cancel. Its pause and
            # resume path should be direct and still preserve the saved state.
            idle_pause = self.invoke_owner_maintenance("pause")
            idle_snapshot, idle_row = self.group_console_row()
            idle_tasks = self.task_snapshot()
            idle_group_tasks = self.task_group(idle_tasks)
            assert idle_row["settings"]["enabled"] is False
            assert idle_group_tasks["enabled"] is False
            idle_resume = self.invoke_owner_maintenance("resume")
            idle_after, idle_after_row = self.group_console_row()
            idle_after_tasks = self.task_snapshot()
            assert idle_after_row["settings"]["enabled"] is True
            assert self.task_group(idle_after_tasks)["enabled"] is True
            assert self.maintenance_state()["groups"] == {}
            self.case("idle-group-direct-pause-resume", lambda: {
                "pause": idle_pause, "resume": idle_resume,
                "paused_console_revision": idle_snapshot["revision"],
                "paused_task_revision": idle_tasks["policy"]["revision"],
                "resumed_console_revision": idle_after["revision"],
                "resumed_task_revision": idle_after_tasks["policy"]["revision"],
            })

            # The owner tool is the actual native MCP tool in both calls; check
            # isolated Router logs for the only remaining scenario-specific
            # cancellation limitation rather than treating scripted markers as
            # proof of tool result.
            log = (self.directory / "runtime.log").read_text(errors="replace")
            assert "Failed to load plugin v2_tasks" not in log
            assert "failed to load plugin v2_dsh_router" not in log
        except Exception as error:
            (self.directory / "failure-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
            self.result_cases.append({"case": "fatal", "passed": False,
                                      "error_type": type(error).__name__, "error": str(error)[:1200]})
            print(f"FAIL {type(error).__name__}: {error}", flush=True)
        finally:
            self.stop()
            if self.launch_log:
                self.launch_log.close()
                self.launch_log = None
        result = {"passed": bool(self.result_cases) and all(item["passed"] for item in self.result_cases),
                  "cases": self.result_cases, "real_qq_connections": 0,
                  "real_model_calls": 0, "real_systemd_restarts": 0,
                  "data_dir": str(self.directory),
                  "staged_astrbot_root": str(self.details["astrbot_root"] if self.details else "")}
        self.save("result.json", result)
        return result


def _child_main(directory: Path, dashboard_port: int) -> int:
    return _base._child_main(directory, dashboard_port)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=ROOT / ".runtime" / ("group-maintenance-check-" + uuid.uuid4().hex[:12]))
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
    result = GroupMaintenanceCheck(args.data_dir.absolute()).run_checks()
    print(json.dumps({"passed": result["passed"], "cases": len(result["cases"]),
                      "data_dir": str(args.data_dir)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
