#!/usr/bin/env python3
"""Load v2_tasks in isolated real AstrBot/DSH scripted processes; no QQ/model calls."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run():
    spec = importlib.util.spec_from_file_location(
        "tasks_startup_expansion", ROOT / "scripts/check-sandbox-expansion.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = ROOT / ".runtime" / ("tasks-startup-" + uuid.uuid4().hex[:10])
    check = module.Expansion(directory, False)
    result = {"passed": False, "directory": str(directory)}
    try:
        check.start()
        path = directory / "astrbot/data/plugin_data/v2_tasks/workspace-runs.sqlite3"
        legacy_state = directory / "astrbot/data/plugin_data/v2_tasks/tasks.sqlite3"

        def loaded():
            log = (directory / "runtime.log").read_text(errors="replace")
            if "Failed to load plugin v2_tasks" in log:
                raise AssertionError("native task plugin loading failed; see isolated runtime log")
            if not path.exists():
                return False
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "task_policy" not in tables:
                    return False
                return db.execute("SELECT revision FROM task_policy WHERE singleton=1").fetchone()

        check.wait(loaded, "actual workspace task manager initialized", timeout=30)
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            policy = db.execute("SELECT default_enabled,legacy_imported FROM task_policy WHERE singleton=1").fetchone()
            runs = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert policy is not None and policy[0] == 0 and policy[1] == 1, policy
        assert runs == 0
        assert not legacy_state.exists(), "retired global daily runner recreated its state database"
        assert not [event for event in check.events() if event["direction"] == "outbound"]
        # Startup creates native runtimes, not cognitive sessions.
        state_root = directory / "astrbot/data/plugin_data/v2_dsh_router"
        for state_file in state_root.glob("state*.sqlite3"):
            with sqlite3.connect(state_file.resolve().as_uri() + "?mode=ro", uri=True) as db:
                assert db.execute("SELECT COUNT(*) FROM conversation_state").fetchone()[0] == 0
        result.update(passed=True, workspace_task_manager_initialized=True,
                      default_policy_off=policy[0] == 0,
                      retired_legacy_state_db_absent=True,
                      workspace_task_runs=runs, outbound_messages=0, cognitive_sessions=0)
    finally:
        check.stop()
        result["stopped"] = check.process is None
        result["mounts_released"] = str(directory) not in Path("/proc/self/mountinfo").read_text()
        if not result["mounts_released"]:
            result["passed"] = False
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
