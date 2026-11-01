#!/usr/bin/env python3
"""Exercise owner task management through the real isolated AstrBot Dashboard."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location("console_tasks_base", ROOT / "scripts/check-console.py")
assert _spec and _spec.loader
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
runtime = _base.runtime


class TaskConsoleCheck(_base.ConsoleCheck):
    def _stage(self):
        super()._stage()
        # Workspace tasks use native AstrBot plugin-page discovery, which
        # requires the plugin root to be a physical directory below data/plugins.
        target = Path(self.details["astrbot_root"]) / "data/plugins/v2_tasks"
        source = self.repo / "astrbot-plugins/v2_tasks"
        if target.is_symlink():
            raise RuntimeError("staged v2_tasks plugin must remain a physical directory")
        if not target.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.rmtree(target)
            shutil.copytree(source, target)

    def task_dispatch(self, action, payload=None, *, owner=None, allow_error=False):
        body = {"action": action, **(payload or {})}
        headers = {"Content-Type": "application/json"}
        opener = self.cookie_opener
        if owner is not None:
            import jwt
            config = json.loads((Path(self.details["astrbot_root"]) / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
            token = jwt.encode({"username": owner,
                                "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                               config["dashboard"]["jwt_secret"], algorithm="HS256")
            from urllib.request import build_opener
            opener = build_opener()
            headers["Authorization"] = "Bearer " + token
        status, result = self.dashboard_call("/api/plug/v2_console/dispatch", body,
                                             opener=opener, headers=headers,
                                             allow_error=allow_error or owner is not None)
        if owner is not None and owner != _base.TEST_USERNAME:
            if status < 400 and result.get("status") != "error":
                raise AssertionError("non-owner reached automatic task API")
            return status, result
        if result.get("status") != "ok":
            if allow_error:
                return status, result
            raise AssertionError(f"task console {action} failed: {result!r}")
        return result["data"]

    def task_snapshot(self):
        return self.task_dispatch("tasks.snapshot")

    @staticmethod
    def identity(group_id):
        return ["v2-sandbox", "99000", f"group:{group_id}"]

    def group_tasks(self, snapshot, group_id):
        identity = tuple(self.identity(group_id))
        return next(row for row in snapshot["groups"] if tuple(row["identity"]) == identity)

    def run_checks(self):
        try:
            self.start()
            unauth_status, _ = self.dashboard_call(
                "/api/plug/v2_console/dispatch", {"action": "tasks.snapshot"},
                opener=__import__("urllib.request", fromlist=["build_opener"]).build_opener(),
                allow_error=True)
            assert unauth_status >= 400
            _, outsider = self.task_dispatch("tasks.snapshot", owner="another-dashboard-user")
            assert outsider.get("status") == "error"
            initial = self.task_snapshot()
            assert initial["policy"]["default_enabled"] is False
            first = self.group_tasks(initial, "91000")
            assert first["enabled"] is False and first["available"] is True
            self.case("owner-api-auth-and-default-off", lambda: {
                "anonymous_status": unauth_status,
                "non_owner_denied": True,
                "default_enabled": initial["policy"]["default_enabled"],
                "admitted_groups": len(initial["groups"]),
            })

            policy = self.task_dispatch("tasks.policy", {
                "default_enabled": True,
                "expected_revision": initial["policy"]["revision"],
            })
            stale_status, stale = self.task_dispatch("tasks.policy", {
                "default_enabled": False,
                "expected_revision": initial["policy"]["revision"],
            }, allow_error=True)
            assert stale.get("status") == "error", (stale_status, stale)
            managed_after_enable = self.wait(
                lambda: (row if (row := self.group_tasks(self.task_snapshot(), "91000")).get("managed")
                         else None),
                "workspace task manager adopts the enabled group", timeout=30)
            assert not any(row["task"]["id"] == "daily_memory"
                           for row in managed_after_enable["tasks"]), managed_after_enable
            self.case("hot-policy-switch-and-policy-cas", lambda: {
                "revision": policy["revision"], "default_enabled": True,
                "stale_revision_rejected": True, "builtin_daily_memory_seed_absent": True,
            })

            self.request("POST", "/api/conversations", {"type": "group", "id": "92000",
                                                            "title": "Task console new group"})
            console_snapshot = self.console({"action": "snapshot"})
            self.console({"action": "defaults.save", "expected_revision": console_snapshot["revision"],
                          "settings": {"model": {"provider": "sandbox-scripted", "model": "scripted"},
                                       "enabled": True, "editors": ["90001"]}})
            console_snapshot = self.console({"action": "snapshot"})
            self.console({"action": "group.add", "expected_revision": console_snapshot["revision"],
                          "platform_id": self.details["platform_id"], "bot_id": str(self.details["bot"]),
                          "group_id": "92000", "template_id": console_snapshot["templates"][-1]["id"]})
            scenario = self.request("GET", "/api/scenario")
            self.request("PUT", "/api/scenario", {"revision": scenario["revision"],
                                                     "admitted": [*scenario["admitted"], "group:92000"]})
            self.send_message("92000", "initialize the newly admitted isolated task group", mention=False)
            new_group = self.wait(lambda: self.group_tasks(self.task_snapshot(), "92000"),
                                  "newly admitted group appears in task console", timeout=30)
            assert new_group["enabled"] is True and new_group["available"] is True
            self.case("new-admitted-group-inherits-global-policy-without-env-change", lambda: {
                "identity": new_group["identity"], "enabled": new_group["enabled"],
            })

            identity = self.identity("91000")
            managed = self.group_tasks(self.task_snapshot(), "91000")
            assert managed["revision"] >= 0
            memory_task_id = str(uuid.uuid4())
            memory_definition = {
                "id": memory_task_id, "name": "Owner explicit memory task",
                "enabled": True, "kind": "memory",
                "schedule": {"type": "cron", "expression": "0 23 * * *",
                             "timezone": "Asia/Shanghai"},
                "prompt": None, "parameters": {"min_new_messages": 99},
            }
            memory_created = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "create", "definition": memory_definition,
                "expected_revision": managed["revision"],
            })
            manifest_revision = memory_created["revision"]
            diary_task_id = str(uuid.uuid4())
            diary_definition = {
                "id": diary_task_id, "name": "Owner diary task", "enabled": True,
                "kind": "agent",
                "schedule": {"type": "once", "run_at":
                             (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()},
                "prompt": "Write a diary in the group workspace.", "parameters": {},
            }
            diary_created = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "create", "definition": diary_definition,
                "expected_revision": manifest_revision,
            })
            manifest_revision = diary_created["revision"]
            assert any(row["task"]["id"] == memory_task_id and row["scheduled"]
                       for row in diary_created["tasks"]), diary_created
            assert any(row["task"]["id"] == diary_task_id and row["scheduled"]
                       for row in diary_created["tasks"]), diary_created
            task_id = str(uuid.uuid4())
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
            definition = {"id": task_id, "name": "console immediate-run acceptance",
                          "enabled": True, "kind": "agent",
                          "schedule": {"type": "once", "run_at": run_at},
                          "prompt": "Use qq_send_origin to send exactly: TASK_CONSOLE_ACCEPTANCE_MARKER",
                          "parameters": {}}
            created = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "create", "definition": definition,
                "expected_revision": manifest_revision,
            })
            manifest_revision = created["revision"]
            stale_status, stale = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "update", "task_id": task_id,
                "definition": {"name": "stale update"}, "expected_revision": managed["revision"],
            }, allow_error=True)
            assert stale.get("status") == "error", (stale_status, stale)
            updated = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "update", "task_id": task_id,
                "definition": {"name": "console acceptance updated"},
                "expected_revision": manifest_revision,
            })
            manifest_revision = updated["revision"]
            enabled_off = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "disable", "task_id": task_id,
                "expected_revision": manifest_revision,
            })
            manifest_revision = enabled_off["revision"]
            enabled_on = self.task_dispatch("tasks.manage", {
                "identity": identity, "operation": "enable", "task_id": task_id,
                "expected_revision": manifest_revision,
            })
            manifest_revision = enabled_on["revision"]
            assert any(row["task"]["id"] == task_id and row["registered"]
                       for row in enabled_on["tasks"])
            self.case("task-crud-and-manifest-cas", lambda: {
                "task_id": task_id, "create_update_disable_enable": True,
                "stale_manifest_rejected": True, "revision": manifest_revision,
            })

            self.request("POST", "/api/scripts", {"conversation_key": "group:91000", "kind": "tool",
                                                     "name": "mcp__astrbot__qq_send_origin",
                                                     "arguments": {"text": "TASK_CONSOLE_ACCEPTANCE_MARKER"}})
            request_id = str(uuid.uuid4())
            started = self.task_dispatch("tasks.run", {
                "identity": identity, "task_id": task_id, "expected_revision": manifest_revision,
                "request_id": request_id,
            })
            duplicate = self.task_dispatch("tasks.run", {
                "identity": identity, "task_id": task_id, "expected_revision": manifest_revision,
                "request_id": request_id,
            })
            assert started["run_id"] == duplicate["run_id"]
            def terminal_run():
                rows = self.group_tasks(self.task_snapshot(), "91000")["tasks"]
                task_row = next((row for row in rows if row["task"]["id"] == task_id), None)
                return task_row and next((run for run in task_row["recent_runs"]
                                          if run["status"] in {"completed", "failed", "cancelled",
                                                               "interrupted", "disabled"}), None)
            completed = self.wait(terminal_run,
                                  "immediate task run reaches visible terminal status", timeout=90)
            assert completed["status"] == "completed", completed
            outbound = self.wait(lambda: next((event for event in self.events()
                                               if event["direction"] == "outbound"
                                               and "TASK_CONSOLE_ACCEPTANCE_MARKER" in json.dumps(event)), None),
                                 "simulated OneBot receives task message", timeout=30)
            self.wait(lambda: any(row["direction"] == "outbound"
                                  and str(row["message_id"]) == str(outbound["message_id"])
                                  and row["conversation_key"] == "group:91000" for row in self.rows()),
                      "task output is recorded in canonical Journal", timeout=30)
            assert len([event for event in self.events() if event["direction"] == "outbound"
                        and "TASK_CONSOLE_ACCEPTANCE_MARKER" in json.dumps(event)]) == 1
            self.case("immediate-run-idempotency-visible-status-and-journal", lambda: {
                "run_id": started["run_id"], "status": completed["status"], "onebot_outbound_count": 1,
                "journal_message_id": outbound["message_id"],
            })

            disabled_policy = self.task_dispatch("tasks.policy", {
                "identity": identity, "enabled": False,
                "expected_revision": self.task_snapshot()["policy"]["revision"],
            })
            assert self.group_tasks(self.task_snapshot(), "91000")["enabled"] is False
            def paused_task():
                rows = self.group_tasks(self.task_snapshot(), "91000")["tasks"]
                return next((row for row in rows if row["task"]["id"] == task_id
                             and row["scheduled"] is False), None)
            paused = self.wait(paused_task, "disabled group task removed from scheduler", timeout=15)
            paused_group = self.group_tasks(self.task_snapshot(), "91000")
            paused_rows = {row["task"]["id"]: row for row in paused_group["tasks"]}
            assert "daily_memory" not in paused_rows
            assert paused_rows[memory_task_id]["scheduled"] is False
            assert paused_rows[diary_task_id]["scheduled"] is False
            def marker_count():
                return len([event for event in self.events() if event["direction"] == "outbound"
                            and "TASK_CONSOLE_ACCEPTANCE_MARKER" in json.dumps(event)])
            count_before = marker_count()
            time.sleep(2)
            count_after = marker_count()
            assert count_before == count_after == 1
            assert paused["task"]["enabled"] is True
            self.task_dispatch("tasks.manage", {"identity": identity, "operation": "delete",
                                "task_id": task_id,
                                "expected_revision": self.group_tasks(
                                    self.task_snapshot(), "91000")["revision"]})
            self.case("group-switch-pauses-tasks-without-replay", lambda: {
                "policy_revision": disabled_policy["revision"], "group_disabled": True,
                "task_unscheduled": True, "marker_count_before_after": [count_before, count_after],
                "user_memory_task_and_diary_preserved": True,
            })

            task_state = Path(self.details["astrbot_root"]) / "data/plugin_data/v2_tasks/tasks.sqlite3"
            if task_state.exists():
                with sqlite3.connect(task_state) as db:
                    legacy_runs = db.execute("SELECT COUNT(*) FROM attempts WHERE conversation_key IN (?,?)",
                                             ("group:91000", "group:92000")).fetchone()[0]
                assert legacy_runs == 0
            else:
                legacy_runs = 0
            assert legacy_runs == 0
            self.case("workspace-memory-is-excluded-from-legacy-runner", lambda: {
                "legacy_memory_attempts_for_managed_groups": legacy_runs,
            })

            self.stop()
            if self.launch_log:
                self.launch_log.close()
                self.launch_log = None
            self._launcher()
            self.wait(lambda: self.http("/api/state").get("connected"), "OneBot after restart", timeout=90)
            self.wait(self._dashboard_ready, "Dashboard after restart", timeout=90)
            self._login()
            after_restart = self.task_snapshot()
            assert after_restart["policy"]["default_enabled"] is True
            assert self.group_tasks(after_restart, "91000")["enabled"] is False
            assert self.group_tasks(after_restart, "92000")["enabled"] is True
            self.case("policy-and-new-group-inheritance-survive-restart", lambda: {
                "default_enabled": True, "pilot_override": False, "new_group_inherits": True,
            })
        except Exception as error:
            (self.directory / "failure-traceback.txt").write_text(__import__("traceback").format_exc(), encoding="utf-8")
            self.result_cases.append({"case": "fatal", "passed": False,
                                      "error_type": type(error).__name__, "error": str(error)[:1200]})
            print(f"FAIL {type(error).__name__}: {error}", flush=True)
        finally:
            self.stop()
            if self.launch_log:
                self.launch_log.close()
                self.launch_log = None
        result = {"passed": bool(self.result_cases) and all(item["passed"] for item in self.result_cases),
                  "cases": self.result_cases, "real_qq_connections": 0, "real_model_calls": 0,
                  "real_systemd_restarts": 0, "data_dir": str(self.directory),
                  "staged_astrbot_root": str(self.details["astrbot_root"] if self.details else "")}
        self.save("result.json", result)
        return result


def _child_main(directory: Path, dashboard_port: int) -> int:
    # Reuse check-console's isolated child bootstrap and environment injection.
    return _base._child_main(directory, dashboard_port)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=ROOT / ".runtime" / ("console-tasks-check-" + uuid.uuid4().hex[:12]))
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
    result = TaskConsoleCheck(args.data_dir.absolute()).run_checks()
    print(json.dumps({"passed": result["passed"], "cases": len(result["cases"]),
                      "data_dir": str(args.data_dir)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
