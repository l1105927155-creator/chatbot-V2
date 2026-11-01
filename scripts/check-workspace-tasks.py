#!/usr/bin/env python3
"""Full-process workspace task acceptance using pinned AstrBot/DSH and fake OneBot."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
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


commands = load("workspace_tasks_commands", ROOT / "scripts/check-agent-commands.py")
expansion = commands.expansion


class WorkspaceTaskCheck(commands.ScriptedCommandCheck):
    group = "group:91000"

    def start(self):
        # sandbox.runtime intentionally strips product-specific variables from
        # child processes. Enable tasks only in this private synthetic group.
        launcher = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "import os\n"
            "from sandbox import runtime\n"
            f"root = Path({str(ROOT)!r}).resolve()\n"
            f"data = Path({str(self.directory)!r}).resolve()\n"
            f"ports = {{'onebot': {self.onebot}, 'ui': {self.ui}, 'mcp': {self.mcp}}}\n"
            "original = runtime._child_environment\n"
            "def child_environment(**kwargs):\n"
            "    result = original(**kwargs)\n"
            "    if kwargs.get('dsh_enabled'):\n"
            "        result['V2_WORKSPACE_TASK_GROUPS'] = 'group:91000'\n"
            "        result['V2_DSH_PROVIDER'] = 'sandbox-scripted'\n"
            "        result['V2_DSH_MODEL'] = 'scripted'\n"
            "    return result\n"
            "runtime._child_environment = child_environment\n"
            "details = runtime.prepare(data, root / '.runtime/astrbot', ports, 'scripted', '90009')\n"
            "raise SystemExit(runtime._run(details, root / '.runtime/astrbot', ports, 'scripted', owner_volume_mib=32))\n"
        )
        self.log_path = ROOT / ".runtime" / (self.directory.name + ".runtime.log")
        self.log = self.log_path.open("ab")
        import subprocess
        import sys
        self.process = subprocess.Popen([sys.executable, "-c", launcher], cwd=ROOT,
                                        stdin=subprocess.DEVNULL, stdout=self.log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"),
                  "isolated OneBot connection", timeout=90)
        self.workspace = self.directory / "repo"
        self.wait(lambda: self.journal_path().exists(), "canonical Journal startup")
        from urllib.request import urlopen
        self.wait(lambda: json.load(urlopen(f"http://127.0.0.1:{self.mcp}/healthz", timeout=3)).get("ready"),
                  "execution MCP and task plugin startup", timeout=90)

    def _group_workspace(self):
        candidates = list((self.workspace / ".runtime/group-storage").glob("*/workspace"))
        if len(candidates) != 1:
            raise AssertionError(f"expected one synthetic group workspace, found {len(candidates)}")
        return candidates[0]

    def _seed_retirement_fixture(self):
        """Insert legacy and user-owned tasks into this synthetic group only."""
        sys.path.insert(0, str(ROOT / "astrbot-plugins"))
        for site_packages in (ROOT / ".runtime/astrbot/.venv/lib").glob(
                "python*/site-packages"):
            sys.path.insert(0, str(site_packages))
        from v2_tasks.workspace_store import RunStore, WorkspaceTaskStore

        class Volume:
            @staticmethod
            def verified_group_workspace(path):
                return os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)

        workspace = self._group_workspace()
        store = WorkspaceTaskStore(workspace, Volume())
        legacy = {
            "id": "daily_memory", "name": "每日记忆整理", "enabled": True,
            "kind": "memory",
            "schedule": {"type": "cron", "expression": "0 22 * * *",
                         "timezone": "Asia/Shanghai"},
            "prompt": None, "parameters": {"min_new_messages": 99},
        }
        memory_id = str(uuid.uuid4())
        memory = {
            "id": memory_id, "name": "Owner created memory task", "enabled": True,
            "kind": "memory",
            "schedule": {"type": "cron", "expression": "0 23 * * *",
                         "timezone": "Asia/Shanghai"},
            "prompt": None, "parameters": {"min_new_messages": 99},
        }
        diary_id = str(uuid.uuid4())
        from datetime import datetime, timedelta, timezone
        diary = {
            "id": diary_id, "name": "Owner created diary", "enabled": True,
            "kind": "agent",
            "schedule": {"type": "once", "run_at":
                         (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()},
            "prompt": "Write the scheduled diary in the group workspace.", "parameters": {},
        }
        run_store = RunStore(self.directory / "astrbot/data/plugin_data/v2_tasks/workspace-runs.sqlite3")
        identity = ("v2-sandbox", "99000", self.group)
        revision = store.read()["revision"]
        for task in (legacy, memory, diary):
            revision = store.put(task, revision)["revision"]
            run_store.register(identity, task, self.owner, "synthetic-migration-fixture")
        assert run_store.claim(identity, legacy, "legacy-history-fixture")
        run_store.finish(identity, legacy, "legacy-history-fixture", "completed",
                         {"status": "completed", "inbound_count": 120})
        return store, run_store, identity, legacy, memory, diary

    def _native_result(self, event, tool_name):
        pairs = self.correlated(event, "mcp__astrbot__" + tool_name)
        if len(pairs) != 1:
            raise AssertionError(f"expected one {tool_name} native call, found {len(pairs)}")
        message = pairs[0]["result"]["message"]
        payload = "".join(block.get("text", "") for block in message.get("content", [])
                          if block.get("type") == "text")
        return json.loads(payload)

    def _scheduled_file(self, filename, expected, *, after=None):
        self.plan(self.group, kind="tool", name="bash", arguments={
            "command": f"mkdir -p diary; printf '%s' '{expected}' > 'diary/{filename}'; cat 'diary/{filename}'",
            "description": "Write and verify the scheduled diary acceptance fixture",
            "workdir": ".",
        }, text_after="SCHEDULED_DIARY_WRITTEN")
        marker = "diary-" + uuid.uuid4().hex[:8]
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            path = self._group_workspace() / "diary" / filename
            if path.is_file() and path.read_text(encoding="utf-8") == expected:
                return marker
            if self.process and self.process.poll() is not None:
                raise AssertionError("runtime exited during scheduled task; inspect runtime log")
            time.sleep(.2)
        raise AssertionError(f"APScheduler did not write {filename}; inspect evidence and runtime log")

    def _create(self, definition):
        self.plan(self.group, kind="tool", name="mcp__astrbot__task_manage", arguments={
            "action": "create", "definition": definition,
        }, text_after="TASK_CREATED")
        event = self.wake(self.group, "请按我说的内容创建这个自动任务。", sender=self.owner)
        output = self._complete(event)
        assert "TASK_CREATED" in json.dumps(output, ensure_ascii=False), output
        payload = self._native_result(event, "task_manage")
        task_id = payload["tasks"][0]["task"]["id"]
        assert payload["tasks"][0]["registered"] and payload["tasks"][0]["scheduled"], payload
        return event, task_id, payload

    def run_checks(self):
        started = time.monotonic()
        try:
            self.start()
            assert self.http("/api/state")["scenario"]["owner_id"] == self.owner
            state = self.state(self.group)
            if state is None:
                self.plan(self.group, kind="reply", text="TASK_FRONT_READY")
                self._complete(self.wake(self.group, "初始化隔离群对话。", sender=self.owner))
                state = self.state(self.group)
            assert state and state.get("session_id")
            front_before = dict(state)
            manifest, run_store, task_identity, legacy, user_memory, user_diary = \
                self._seed_retirement_fixture()

            def migrated_manifest():
                current = manifest.read()
                ids = {task["id"] for task in current["tasks"]}
                if "daily_memory" in ids:
                    return None
                return ids

            remaining_ids = self.wait(migrated_manifest,
                                      "ready incremental memory manager retires the old built-in task",
                                      timeout=45)
            assert remaining_ids == {user_memory["id"], user_diary["id"]}, remaining_ids
            assert run_store.registration(task_identity, legacy) is None
            old_history = run_store.recent(task_identity, "daily_memory")
            assert len(old_history) == 1 and old_history[0]["status"] == "completed", old_history
            assert run_store.registration(task_identity, user_memory)
            assert run_store.registration(task_identity, user_diary)
            self.plan(self.group, kind="tool", name="mcp__astrbot__task_list",
                      arguments={}, text_after="MIGRATED_TASKS_VISIBLE")
            migration_list_event = self.wake(self.group, "查询现有任务清单。", sender=self.owner)
            migration_output = self._complete(migration_list_event)
            assert "MIGRATED_TASKS_VISIBLE" in json.dumps(migration_output, ensure_ascii=False)
            migration_listing = self._native_result(migration_list_event, "task_list")
            migration_rows = {row["task"]["id"]: row for row in migration_listing["tasks"]}
            assert "daily_memory" not in migration_rows
            assert migration_rows[user_memory["id"]]["scheduled"] is True
            assert migration_rows[user_diary["id"]]["scheduled"] is True
            self.case("ready-memory-manager-retires-built-in-and-preserves-user-tasks", lambda: {
                "retired_builtin": True,
                "user_uuid_memory_task_preserved": user_memory["id"],
                "user_diary_task_preserved": user_diary["id"],
                "legacy_run_history_preserved": old_history[0]["status"],
                "both_user_tasks_remain_scheduled": True,
            })

            filename = "acceptance-" + uuid.uuid4().hex[:8] + ".md"
            payload = "DIARY_FIXTURE_" + uuid.uuid4().hex
            # Leave time for AstrBot's five-second reconciliation interval.
            from datetime import datetime, timedelta, timezone
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
            create, task_id, created = self._create({
                "name": "隔离验收日记", "kind": "agent", "enabled": True,
                "schedule": {"type": "once", "run_at": run_at},
                "prompt": "在工作区 diary/ 下写一篇今天的日记，并核验文件内容。不要发送群消息。",
                "parameters": {},
            })
            front_before = dict(self.state(self.group))
            outbound_before_schedule = len([row for row in self.events()
                                            if row["direction"] == "outbound"
                                            and row["conversation"] == {"type": "group", "id": "91000"}])
            self._scheduled_file(filename, payload)
            def scheduled_completed():
                path = self.directory / "astrbot/data/plugin_data/v2_tasks/workspace-runs.sqlite3"
                with sqlite3.connect(path) as db:
                    return db.execute("SELECT status FROM runs WHERE task_id=?", (task_id,)).fetchone() == ("completed",)
            self.wait(scheduled_completed, "durable scheduled completion", timeout=60)
            after = self.state(self.group)
            assert after["session_id"] == front_before["session_id"], (front_before, after)
            for field in ("last_seen_journal_id", "history_after_journal_id", "pending_upper_cursor"):
                assert after.get(field) == front_before.get(field), (field, front_before, after)
            outbound_after = [row for row in self.events()
                              if row["direction"] == "outbound"
                              and row["conversation"] == {"type": "group", "id": "91000"}]
            assert len(outbound_after) == outbound_before_schedule, \
                "scheduled execution sent a QQ message"
            trace_rows = self.evidence()["lifecycle"]
            scheduled_ids = {row.get("session_id") for row in trace_rows
                             if row.get("event") == "model/start"
                             and row.get("session_id") != front_before["session_id"]}
            assert scheduled_ids, "scheduled run did not create an independent DSH session"
            assert (self._group_workspace() / "diary" / filename).read_text(encoding="utf-8") == payload

            # Exercise task_list through real MCP and confirm the one-shot is no
            # longer scheduled after its durable completion.
            self.plan(self.group, kind="tool", name="mcp__astrbot__task_list",
                      arguments={"task_id": task_id}, text_after="TASK_LIST_VISIBLE")
            listed_event = self.wake(self.group, "查询刚才创建的任务状态。", sender=self.owner)
            listed_output = self._complete(listed_event)
            assert "TASK_LIST_VISIBLE" in json.dumps(listed_output, ensure_ascii=False)
            listing = self._native_result(listed_event, "task_list")
            once_row = listing["tasks"][0]
            assert once_row["task"]["id"] == task_id
            assert not once_row["scheduled"] and any(
                run["status"] == "completed" for run in once_row["recent_runs"]), listing

            # A second scheduled invocation is canceled by disabling an interval
            # task before its first slot. This proves MCP management controls
            # the APScheduler job and no delayed file side effect occurs.
            interval_file = "disabled-" + uuid.uuid4().hex[:8] + ".md"
            interval_at = (datetime.now(timezone.utc) + timedelta(seconds=35)).isoformat()
            _, interval_id, _ = self._create({
                "name": "禁用任务验收", "kind": "agent", "enabled": True,
                "schedule": {"type": "interval", "seconds": 10, "start_at": interval_at},
                "prompt": f"写入工作区 diary/{interval_file}。",
                "parameters": {},
            })
            self.plan(self.group, kind="tool", name="mcp__astrbot__task_manage", arguments={
                "action": "disable", "task_id": interval_id,
            }, text_after="TASK_DISABLED")
            disabled_event = self.wake(self.group, "禁用刚才创建的任务。", sender=self.owner)
            disabled_output = self._complete(disabled_event)
            assert "TASK_DISABLED" in json.dumps(disabled_output, ensure_ascii=False)
            disabled = self._native_result(disabled_event, "task_manage")
            row = next(item for item in disabled["tasks"] if item["task"]["id"] == interval_id)
            assert row["task"]["enabled"] is False and row["scheduled"] is False
            models_before_disable_wait = self.model_count()
            time.sleep(37)
            assert self.model_count() == models_before_disable_wait, "disabled task invoked model"
            assert not (self._group_workspace() / "diary" / interval_file).exists()

            # Restart the full isolated runtime. The durable retirement must
            # survive plugin initialization and never seed the built-in task again.
            self.stop()
            self.start()
            restarted_manifest = manifest.read()
            restarted_ids = {task["id"] for task in restarted_manifest["tasks"]}
            assert restarted_ids == {user_memory["id"], user_diary["id"], task_id, interval_id}, \
                restarted_ids
            assert not any(task_id_value == "daily_memory" for task_id_value in restarted_ids)
            assert run_store.recent(task_identity, "daily_memory")[0]["status"] == "completed"
            self.case("retirement-does-not-resurrect-after-full-runtime-restart", lambda: {
                "retired_builtin_absent": True,
                "user_uuid_memory_and_diary_still_present": True,
                "legacy_run_history_preserved": True,
            })
            self.results.append({"case": "workspace-owned-once-and-interval-tasks",
                                 "passed": True,
                                 "details": {
                                     "once_task_id": task_id,
                                     "interval_task_id": interval_id,
                                     "creator_mcp_tool": "task_manage",
                                     "task_list_mcp_visible": True,
                                     "once_ap_scheduler_run_completed": True,
                                     "scheduled_run_used_distinct_dsh_session": True,
                                     "scheduled_bash_created_real_workspace_file": str(Path("diary") / filename),
                                     "scheduled_qq_outbound_count": 0,
                                     "foreground_sid_cursor_and_pending_unchanged": True,
                                     "disabled_interval_has_no_file_side_effect": True,
                                     "synthetic_group": "v2-sandbox/group:91000",
                                     "owner": self.owner,
                                     "once_created_by_message": create["message_id"],
                                 }})
        except Exception as exc:
            self.results.append({"case": "workspace-owned-once-and-interval-tasks",
                                 "passed": False, "error_type": type(exc).__name__,
                                 "error": str(exc), "traceback": traceback.format_exc()})
        finally:
            if self.process:
                try:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                except Exception:
                    pass
            self.stop()
            assert not os.path.ismount(self.directory / "repo/.runtime/dsh-workspace-owner"), \
                "Owner volume remained mounted"
        result = {"passed": bool(self.results) and all(row["passed"] for row in self.results),
                  "mode": "scripted-native-provider", "real_qq_connections": 0,
                  "cases": self.results, "elapsed_seconds": round(time.monotonic() - started, 2),
                  "data_dir": str(self.directory),
                  "coverage_limits": [
                      "Scripted DSH verifies task routing and file effects, not model-quality diary prose.",
                      "OneBot is local and synthetic; no real group or QQ send is used."]}
        self.save("result.json", result)
        return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="isolated runtime output; defaults under .runtime")
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.data_dir or ROOT / ".runtime" / ("workspace-tasks-" + uuid.uuid4().hex[:12])
    runner = WorkspaceTaskCheck(directory)
    result = runner.run_checks()
    print(json.dumps({"passed": result["passed"], "mode": result["mode"],
                      "result": str(directory / "result.json")}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
