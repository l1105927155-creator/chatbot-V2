#!/usr/bin/env python3
"""Isolated full-process acceptance for V2 slash commands; simulated QQ only."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


expansion = load("agent_commands_expansion", ROOT / "scripts/check-sandbox-expansion.py")
live_helpers = load("agent_commands_live_owner", ROOT / "scripts/check-sandbox-live-owner.py")


def history_payload(pair: dict) -> dict:
    """Decode one correlated persisted MCP result from the native session log."""
    message = pair["result"]["message"]
    text = "".join(block.get("text", "") for block in message.get("content", [])
                   if block.get("type") == "text")
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise AssertionError("history MCP result has no entries array")
    return value


def assert_history_floor(payload: dict, floor: int, forbidden: str | None = None) -> list[dict]:
    entries = payload["entries"]
    if any(type(row.get("journal_id")) is not int or row["journal_id"] <= floor for row in entries):
        raise AssertionError("history MCP returned a row at or before the session floor")
    if forbidden and forbidden in json.dumps(entries, ensure_ascii=False):
        raise AssertionError("history MCP returned content from before the session floor")
    return entries


class ScriptedCommandCheck(live_helpers.LiveOwner):
    """Real pinned AstrBot/DSH process with scripted provider and local OneBot."""

    def start(self):
        expansion.Expansion.start(self)

    def _journal_row(self, message_id, key):
        found = [row for row in self.rows() if row["message_id"] == str(message_id)
                 and row["conversation_key"] == key]
        assert len(found) == 1, ("expected one canonical row", message_id, key, len(found))
        return found[0]

    def _complete(self, event, timeout=50):
        self.wait(lambda: any(item.get("event") == "turn/completed" for item in self.trace(event)),
                  "actual DSH turn completion", timeout=timeout)
        outputs = [row for row in self.events() if row["direction"] == "outbound"
                   and row["conversation"] == event["conversation"]
                   and int(row["message_id"]) > int(event["message_id"])]
        assert len(outputs) == 1, ("expected one conversational reply", len(outputs))
        self.wait(lambda: any(row["direction"] == "outbound"
                              and row["message_id"] == str(outputs[0]["message_id"])
                              for row in self.rows()), "confirmed canonical Bot reply")
        return outputs[0]

    def _deterministic(self, text, expected, *, key=None, sender=None):
        key = key or self.key
        sender = sender or (self.owner if key == self.key or key.startswith("group:") else key.split(":", 1)[1])
        before_models = self.model_count()
        event = self.wake(key, text, sender=sender)
        output = self.wait(lambda: next((row for row in self.events()
                            if row["direction"] == "outbound"
                            and row["conversation"] == event["conversation"]
                            and int(row["message_id"]) > int(event["message_id"])), None),
                           "deterministic command response", timeout=25)
        self.wait(lambda: any(row["message_id"] == str(output["message_id"])
                              and row["direction"] == "outbound" for row in self.rows()),
                  "command reply canonical Journal")
        time.sleep(.2)
        outputs = [row for row in self.events() if row["direction"] == "outbound"
                   and row["conversation"] == event["conversation"]
                   and int(row["message_id"]) > int(event["message_id"])]
        assert len(outputs) == 1, ("command must produce exactly one reply", text, len(outputs))
        assert expected in json.dumps(output, ensure_ascii=False), output
        assert self.model_count() == before_models, "deterministic command invoked DSH"
        assert not any(row.get("event") == "dsh/dispatched" for row in self.trace(event))
        return event, output

    def _scripted_turn(self, text, expected, *, key=None, sender=None):
        key = key or self.key
        sender = sender or (self.owner if key == self.key or key.startswith("group:") else key.split(":", 1)[1])
        self.plan(key, kind="reply", text=expected)
        event = self.wake(key, text, sender=sender)
        output = self._complete(event)
        assert expected in json.dumps(output, ensure_ascii=False), output
        return event, output

    def _history_tool(self, tool, arguments, prompt, *, key=None, sender=None):
        key = key or self.key
        marker = "HISTORY_TOOL_COMPLETED_" + uuid.uuid4().hex[:10]
        self.plan(key, kind="tool", name="mcp__astrbot__" + tool,
                  arguments=arguments, text_after=marker)
        event = self.wake(key, prompt, sender=sender or self.owner)
        output = self._complete(event)
        assert marker in json.dumps(output, ensure_ascii=False), "scripted native tool did not complete"
        name = "mcp__astrbot__" + tool
        pairs = self.wait(lambda: (found if len(found := self.correlated(event, name)) == 1 else None),
                          "one persisted native call/result correlated to completed history tool", timeout=8)
        assert len(pairs) == 1, ("history tool must have one successful correlated native call/result",
                                 event["message_id"], len(pairs))
        return event, output, history_payload(pairs[0])

    def _bash_tool(self, arguments, prompt, expected_result=None):
        marker = "BASH_TOOL_COMPLETED_" + uuid.uuid4().hex[:10]
        self.plan(self.key, kind="tool", name="bash", arguments=arguments, text_after=marker)
        event = self.wake(self.key, prompt, sender=self.owner, mention=False)
        output = self._complete(event)
        assert marker in json.dumps(output, ensure_ascii=False)
        pairs = self.wait(lambda: (found if len(found := self.correlated(event, "bash")) == 1 else None),
                          "one persisted native Bash call/result", timeout=8)
        returned = "".join(block.get("text", "") for block in pairs[0]["result"]["message"].get("content", [])
                           if block.get("type") == "text")
        if expected_result is not None:
            assert expected_result in returned, ("Bash result does not contain expected file bytes", returned)
        return event, returned

    def _command_journal_id(self, event, key):
        return self._journal_row(event["message_id"], key)["journal_id"]

    def _check_private_new(self):
        old_marker = "OLD_CONTEXT_" + uuid.uuid4().hex.upper()
        old_text = "旧轮次正文唯一标记：" + old_marker
        first, _ = self._scripted_turn(old_text + "，请确认记住本轮内容。", "OLD_CONTEXT_ACCEPTED")
        before = self.state(self.key)
        assert before and before["session_id"]
        old_sid = before["session_id"]
        old_row = self._journal_row(first["message_id"], self.key)
        old_model_inputs = [row.get("input", "") for row in self.evidence(self.key)["lifecycle"]
                            if row.get("event") == "model/start" and row.get("session_id") == old_sid]
        assert old_model_inputs and any(old_marker in value for value in old_model_inputs), \
            "old marker never entered the actual native DSH session"

        self._deterministic("/v2agent persona developer", "人格已切换为 developer")
        self._deterministic("/v2agent scene research", "场景已切换为 research")
        self._deterministic("/v2agent status", "人格：developer；场景：research")
        workspace_file = "preserved-workspace-" + uuid.uuid4().hex[:10] + ".txt"
        workspace_payload = "WORKSPACE_BYTES_" + uuid.uuid4().hex.upper()
        write_event, _ = self._bash_tool({
            "command": f"printf '%s' '{workspace_payload}' > {workspace_file}; cat {workspace_file}",
            "description": "Write and read a private-session workspace fixture before reset",
            "workdir": ".",
        }, "请按计划写入并读取当前工作区文件。", workspace_payload)
        assert (self.workspace / workspace_file).read_bytes() == workspace_payload.encode("ascii")

        command, _ = self._deterministic("/new", "已开始新的 Agent 对话")
        command_id = self._command_journal_id(command, self.key)
        reset_state = self.state(self.key)
        assert reset_state["session_id"] != old_sid
        assert reset_state["last_seen_journal_id"] == reset_state["history_after_journal_id"] == command_id
        assert any(row["journal_id"] == old_row["journal_id"] for row in self.rows()), \
            "reset deleted the canonical Journal"

        _, _ = self._deterministic("/v2agent status", "人格：developer；场景：research")

        fresh_text = "新会话正文 CLEAN_SESSION_INPUT"
        fresh, _ = self._scripted_turn(fresh_text, "CLEAN_SESSION_REPLY")
        fresh_sid = self.state(self.key)["session_id"]
        assert fresh_sid == reset_state["session_id"]
        fresh_inputs = [row.get("input", "") for row in self.evidence(self.key)["lifecycle"]
                        if row.get("event") == "model/start" and row.get("session_id") == fresh_sid]
        assert fresh_inputs and any("CLEAN_SESSION_INPUT" in value for value in fresh_inputs)
        assert all(old_marker not in value and old_text not in value for value in fresh_inputs), \
            "old conversation content reached the new native model context"
        developer_text = (ROOT / "dsh/plugins/agent-profile/personas/developer.md").read_text(encoding="utf-8").strip()
        assert any(developer_text in value for value in fresh_inputs), \
            "the fresh SID model input did not include the selected developer persona"

        read_event, _ = self._bash_tool({
            "command": f"cat {workspace_file}",
            "description": "Read the pre-reset workspace fixture from the new native session",
            "workdir": ".",
        }, "请读取reset之前工作区中的测试文件。", workspace_payload)
        assert (self.workspace / workspace_file).read_bytes() == workspace_payload.encode("ascii")

        recent_event, _, recent = self._history_tool(
            "history_recent", {"limit": 50}, "请查询近期历史并简短说明。")
        recent_entries = assert_history_floor(recent, command_id, old_marker)
        before_event, _, before = self._history_tool(
            "history_before", {"before_id": old_row["journal_id"] + 1, "limit": 10},
            "请查找旧轮次之前的历史。")
        before_entries = assert_history_floor(before, command_id, old_marker)
        search_event, _, searched = self._history_tool(
            "history_search", {"query": old_marker, "limit": 10},
            "请搜索旧轮次唯一标记。")
        search_entries = assert_history_floor(searched, command_id, old_marker)
        assert before_entries == [] and search_entries == []
        assert old_marker not in json.dumps(recent_entries, ensure_ascii=False)

        return {
            "old_session_id": old_sid, "new_session_id": fresh_sid,
            "old_marker_entered_native_session": True,
            "new_native_input_excludes_old_turn": True,
            "persona_and_scene_selected_before_reset_and_preserved": True,
            "new_sid_input_uses_developer_persona": True,
            "workspace_written_before_reset_and_read_after": workspace_file,
            "floor_journal_id": command_id, "old_journal_preserved": old_row["journal_id"],
            "history_recent_old_marker_absent": True,
            "history_before_old_marker_empty": True,
            "history_search_old_marker_empty": True,
            "history_tool_turns": [recent_event["message_id"], before_event["message_id"],
                                   search_event["message_id"]],
            "fresh_turn": fresh["message_id"], "workspace_write_turn": write_event["message_id"],
            "workspace_read_turn": read_event["message_id"],
        }

    def _check_group_ownership(self):
        group = "group:91000"
        old_marker = "GROUP_OLD_" + uuid.uuid4().hex.upper()
        first, _ = self._scripted_turn("群旧轮次唯一标记：" + old_marker, "GROUP_BEFORE_NEW",
                                      key=group, sender=self.owner)
        old_state = self.state(group)
        assert old_state is not None
        first_sid = old_state["session_id"]
        new_event, _ = self._deterministic("/new", "已开始新的 Agent 对话", key=group, sender=self.owner)
        new_id = self._command_journal_id(new_event, group)
        new_state = self.state(group)
        assert new_state["session_id"] != first_sid
        assert new_state["history_after_journal_id"] == new_id
        reset_event, _ = self._deterministic("/reset", "已开始新的 Agent 对话", key=group, sender=self.owner)
        reset_id = self._command_journal_id(reset_event, group)
        reset_state = self.state(group)
        assert reset_state["session_id"] not in {first_sid, new_state["session_id"]}
        assert reset_state["last_seen_journal_id"] == reset_state["history_after_journal_id"] == reset_id
        assert any(row["journal_id"] == self._journal_row(first["message_id"], group)["journal_id"]
                   for row in self.rows()), "group reset deleted canonical Journal history"

        before_states = self.evidence()["states"]
        member_new, _ = self._deterministic("/new", "仅 Bot Owner 可以", key=group, sender="90001")
        member_reset, _ = self._deterministic("/reset", "仅 Bot Owner 可以", key=group, sender="90002")
        assert self.evidence()["states"] == before_states
        assert self.state(group) == reset_state
        return {
            "owner_group_new_allowed": True, "owner_group_reset_allowed": True,
            "member_and_non_owner_rejected": True,
            "rejected_commands": [member_new["message_id"], member_reset["message_id"]],
            "group_history_floor": reset_id,
        }

    def _check_restart_floor(self):
        before = self.state(self.key)
        assert before and before["history_after_journal_id"] > 0
        old_floor = before["history_after_journal_id"]
        old_sid = before["session_id"]
        journal_rows = self.rows()
        self.stop()
        self.start()
        after = self.state(self.key)
        assert after["history_after_journal_id"] == old_floor
        assert after["session_id"] == old_sid
        assert self.rows() == journal_rows, "restart changed or removed canonical Journal rows"
        marker = "OLD_CONTEXT_"
        event, _, result = self._history_tool(
            "history_search", {"query": marker, "limit": 10},
            "重启后请查询旧会话标记。")
        assert_history_floor(result, old_floor, marker)
        assert result["entries"] == []
        return {"session_id": old_sid, "history_floor_persisted": old_floor,
                "journal_rows_unchanged": len(journal_rows), "post_restart_search_empty": True,
                "query_turn": event["message_id"]}

    def _check_command_surface(self):
        rows = []
        for command, expected in (
            ("/help", "V2 Agent 命令"),
            ("/provider", "当前模型："),
            ("/stats", "/stats 尚未接通 DSH"),
            ("/set", "/set 尚未接通 DSH"),
            ("/unset", "/unset 尚未接通 DSH"),
            ("/sid", "UMO:"),
        ):
            before_state = self.state(self.key)
            event, _ = self._deterministic(command, expected)
            assert self.state(self.key) == before_state, command + " changed DSH state"
            rows.append(event["message_id"])
        return {"one_reply_each": True, "no_dsh_calls": True,
                "native_sid_command_preserved": True, "commands": rows}

    def _check_stop(self):
        ready_name = "command-stop-ready-" + uuid.uuid4().hex[:10] + ".txt"
        late_name = "command-stop-late-" + uuid.uuid4().hex[:10] + ".txt"
        baseline = self.http("/api/events")["cursor"]
        self.plan(self.key, kind="tool", name="bash", arguments={
            "command": f"printf READY > {ready_name}; sleep 7; printf LATE > {late_name}",
            "description": "Verify /stop cancels this native foreground command",
            "workdir": ".",
        }, text_after="STOP_TEST_SHOULD_NOT_FINISH")
        event = self.wake(self.key, "请执行计划中的前台命令并等待完成。",
                          sender=self.owner, mention=False)
        self.wait_native({"event": "tool/start", "tool": "bash",
                          "conversation_key": self.key}, after=baseline, timeout=30)
        self.wait(lambda: (self.workspace / ready_name).is_file(), "real foreground Bash started", timeout=20)
        observed = time.monotonic()
        processes = self.matching_processes(late_name)
        assert processes, "foreground shell process carrying the late marker was not observed"
        stop_cursor = self.http("/api/events")["cursor"]
        model_count = self.model_count()
        stop_event = self.wake(self.key, "/stop", sender=self.owner)
        stop_output = self.wait(lambda: next((row for row in self.events()
                                  if row["direction"] == "outbound"
                                  and row["conversation"] == stop_event["conversation"]
                                  and int(row["message_id"]) > int(stop_event["message_id"])
                                  and "已请求停止" in json.dumps(row, ensure_ascii=False)), None),
                                "single deterministic /stop acknowledgement", timeout=25)
        self.wait(lambda: any(row["message_id"] == str(stop_output["message_id"])
                              and row["direction"] == "outbound" for row in self.rows()),
                  "/stop acknowledgement canonical Journal")
        assert self.model_count() == model_count, "/stop invoked DSH"
        assert not any(row.get("event") == "dsh/dispatched" for row in self.trace(stop_event))
        assert sum(1 for row in self.events() if row["direction"] == "outbound"
                   and row["conversation"] == stop_event["conversation"]
                   and int(row["message_id"]) > int(stop_event["message_id"])
                   and "已请求停止" in json.dumps(row, ensure_ascii=False)) == 1
        self.wait_native({"event": "dsh/stop", "stop_reason": "cancelled",
                          "conversation_key": self.key}, after=stop_cursor, timeout=25)
        self.wait(lambda: not self.matching_processes(late_name), "cancelled Bash process exited", timeout=10)
        while time.monotonic() < observed + 7.5:
            time.sleep(.1)
        assert not (self.workspace / late_name).exists(), "cancelled command produced a late side effect"
        run_journal_id = self._journal_row(event["message_id"], self.key)["journal_id"]
        stopped_state = self.state(self.key)
        assert stopped_state["pending_upper_cursor"] == run_journal_id, \
            "confirmed /stop cancellation must retain its exact uncommitted input for owner recovery"
        assert stopped_state["last_seen_journal_id"] < run_journal_id, \
            "/stop cancellation must not commit an incomplete turn"
        return {"run_message_id": event["message_id"], "stop_message_id": stop_event["message_id"],
                "stop_ack_message_id": stop_output["message_id"],
                "actual_bash_started": True, "process_exited": True,
                "observed_after_side_effect_deadline": True, "late_file_absent": True,
                "pending_exact_cancelled_input_for_existing_owner_recovery": run_journal_id,
                "started_processes": processes}

    def _check_new_cancels_running_turn(self):
        ready_name = "command-new-ready-" + uuid.uuid4().hex[:10] + ".txt"
        late_name = "command-new-late-" + uuid.uuid4().hex[:10] + ".txt"
        old_secret = "ACTIVE_OLD_SECRET_" + uuid.uuid4().hex.upper()
        before = self.state(self.key)
        old_sid = before["session_id"] if before else None
        baseline = self.http("/api/events")["cursor"]
        self.plan(self.key, kind="tool", name="bash", arguments={
            "command": f"printf READY > {ready_name}; sleep 7; printf '{old_secret}' > {late_name}",
            "description": "Verify /new cancels the active native command before its side effect",
            "workdir": ".",
        }, text_after="ACTIVE_NEW_TEST_SHOULD_NOT_FINISH")
        active = self.wake(self.key, "正在进行的任务包含旧秘密 " + old_secret + "；请执行计划中的前台命令。",
                           sender=self.owner, mention=False)
        self.wait_native({"event": "tool/start", "tool": "bash",
                          "conversation_key": self.key}, after=baseline, timeout=30)
        self.wait(lambda: (self.workspace / ready_name).is_file(), "real foreground Bash started for /new", timeout=20)
        processes = self.matching_processes(late_name)
        assert processes, "active Bash process carrying the delayed side effect was not observed"

        reset_cursor = self.http("/api/events")["cursor"]
        reset, _ = self._deterministic("/new", "已开始新的 Agent 对话", sender=self.owner)
        reset_id = self._command_journal_id(reset, self.key)
        self.wait_native({"event": "dsh/stop", "stop_reason": "cancelled",
                          "conversation_key": self.key}, after=reset_cursor, timeout=25)
        self.wait(lambda: not self.matching_processes(late_name), "cancelled /new Bash process exited", timeout=10)
        time.sleep(7.2)
        assert not (self.workspace / late_name).exists(), "/new-cancelled command produced a late side effect"

        reset_state = self.state(self.key)
        assert reset_state and reset_state["session_id"] != old_sid
        assert reset_state["history_after_journal_id"] == reset_id
        fresh_text = "重置之后的新请求 NEW_AFTER_ACTIVE_RESET"
        fresh, _ = self._scripted_turn(fresh_text, "NEW_AFTER_ACTIVE_RESET_OK")
        new_sid = self.state(self.key)["session_id"]
        assert new_sid == reset_state["session_id"]
        fresh_inputs = [row.get("input", "") for row in self.evidence(self.key)["lifecycle"]
                        if row.get("event") == "model/start" and row.get("session_id") == new_sid]
        assert fresh_inputs and any("NEW_AFTER_ACTIVE_RESET" in value for value in fresh_inputs)
        assert all(old_secret not in value for value in fresh_inputs), \
            "old active-turn secret reached the new native model context"
        return {"active_turn_message_id": active["message_id"],
                "reset_message_id": reset["message_id"], "new_turn_message_id": fresh["message_id"],
                "old_session_id": old_sid, "new_session_id": new_sid,
                "active_bash_cancelled": True, "late_side_effect_absent": True,
                "new_session_accepts_followup": True, "old_secret_excluded_from_new_input": True,
                "reset_floor": reset_id}

    def run_checks(self):
        started = time.monotonic()
        try:
            self.start()
            assert self.http("/api/state")["scenario"]["owner_id"] == self.owner
            self.case("private-new-and-history-floor", self._check_private_new)
            self.case("group-owner-new-reset-and-non-owner-rejection", self._check_group_ownership)
            self.case("restart-retains-history-floor", self._check_restart_floor)
            self.case("help-legacy-notices-and-sid", self._check_command_surface)
            self.case("new-cancels-active-turn-and-preserves-next-turn", self._check_new_cancels_running_turn)
            self.case("stop-cancels-running-bash", self._check_stop)
        except Exception as exc:
            self.results.append({"case": "fatal", "passed": False,
                                 "error_type": type(exc).__name__, "error": str(exc),
                                 "traceback": traceback.format_exc()})
        finally:
            if self.process:
                try:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                except Exception:
                    pass
            self.stop()
            assert not os.path.ismount(self.directory / "repo/.runtime/dsh-workspace-owner"), \
                "Owner volume remained mounted"
        result = {
            "passed": bool(self.results) and all(row["passed"] for row in self.results),
            "mode": "scripted-native-provider", "real_qq_connections": 0,
            "cases": self.results, "elapsed_seconds": round(time.monotonic() - started, 2),
            "data_dir": str(self.directory),
            "coverage_limits": [
                "The simulated OneBot reports fixture members as ordinary members; actual QQ group-role metadata is not simulated.",
                "Use --live to observe model behavior with the inherited provider; this script never prints credential values.",
            ],
        }
        self.save("result.json", result)
        return result


class LiveCommandCheck(ScriptedCommandCheck):
    """Optional real-model check of fresh-session memory behavior, still simulated QQ."""

    def start(self):
        # Keep the command/history helpers shared while selecting the real provider runtime.
        live_helpers.LiveOwner.start(self)

    def run_live_memory_check(self):
        started = time.monotonic()
        try:
            self.start()
            marker = "LIVE_OLD_CONTEXT_" + uuid.uuid4().hex.upper()
            old_text = "请记住唯一标记 " + marker + "，并只回复这个标记。"
            old_event, old_output = self.turn(old_text, marker)
            old_state = self.state(self.key)
            assert old_state is not None
            old_sid = old_state["session_id"]
            old_row = self._journal_row(old_event["message_id"], self.key)
            baseline = self.model_count()
            reset_event, _ = self._deterministic("/new", "已开始新的 Agent 对话")
            floor = self._command_journal_id(reset_event, self.key)
            fresh_state = self.state(self.key)
            assert fresh_state["session_id"] != old_sid
            assert fresh_state["history_after_journal_id"] == floor

            question = "刚才聊到哪了？"
            event = self.wake(self.key, question, sender=self.owner, mention=False)
            output = self.wait(lambda: next((row for row in self.events()
                                if row["direction"] == "outbound"
                                and row["conversation"] == event["conversation"]
                                and int(row["message_id"]) > int(event["message_id"])), None),
                               "real Flash model response to fresh-history question", timeout=170)
            self.wait(lambda: any(item.get("event") == "turn/completed" for item in self.trace(event)),
                      "live question turn completed", timeout=30)
            answer = json.dumps(output, ensure_ascii=False)
            assert marker not in answer, "model reproduced a marker outside its fresh-session context"
            models = [row for row in self.evidence(self.key)["lifecycle"]
                      if row.get("event") == "model/start" and row.get("session_id") == fresh_state["session_id"]]
            fresh_inputs = [row.get("input", "") for row in models if row.get("input")]
            assert fresh_inputs and all(marker not in value and old_text not in value for value in fresh_inputs)
            history_pairs = []
            for tool in ("mcp__astrbot__history_recent", "mcp__astrbot__history_before",
                         "mcp__astrbot__history_search"):
                history_pairs.extend(self.correlated(event, tool))
            for pair in history_pairs:
                payload = history_payload(pair)
                assert_history_floor(payload, floor, marker)
            assert any(row["journal_id"] == old_row["journal_id"] for row in self.rows())
            self.results.append({"case": "live-fresh-session-memory", "passed": True,
                                 "details": {"old_session_id": old_sid,
                                             "new_session_id": fresh_state["session_id"],
                                             "history_floor": floor,
                                             "old_marker_absent_from_real_model_inputs_and_reply": True,
                                             "history_tool_calls": len(history_pairs),
                                             "all_actual_history_results_respect_floor": True,
                                             "actual_reply": answer,
                                             "old_journal_preserved": old_row["journal_id"],
                                             "model_calls_before_new": baseline}})
        except Exception as exc:
            self.results.append({"case": "live-fresh-session-memory", "passed": False,
                                 "error_type": type(exc).__name__, "error": str(exc),
                                 "traceback": traceback.format_exc()})
        finally:
            try:
                if self.process:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
            except Exception:
                pass
            self.stop()
            assert not os.path.ismount(self.directory / "repo/.runtime/dsh-workspace-owner")
        result = {"passed": bool(self.results) and all(row["passed"] for row in self.results),
                  "mode": "live-model-simulated-QQ", "provider": os.environ.get("V2_DSH_PROVIDER"),
                  "model": os.environ.get("V2_DSH_MODEL"), "real_qq_connections": 0,
                  "cases": self.results, "elapsed_seconds": round(time.monotonic() - started, 2),
                  "data_dir": str(self.directory),
                  "coverage_limits": ["Live Flash output varies; no credential values are recorded.",
                                      "The OneBot transport remains simulated; actual QQ client behavior is not exercised."]}
        self.save("result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="isolated sandbox output; defaults below .runtime")
    parser.add_argument("--live", action="store_true", help="run the actual inherited DSH model for memory check")
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.data_dir or ROOT / ".runtime" / ("agent-commands-" + uuid.uuid4().hex[:12])
    if args.live and not all(os.environ.get(key) for key in
                             ("DEEPSEEK_API_KEY", "V2_DSH_PROVIDER", "V2_DSH_MODEL")):
        parser.error("--live requires inherited provider/model credentials; values are never printed")
    runner = LiveCommandCheck(directory) if args.live else ScriptedCommandCheck(directory)
    result = runner.run_live_memory_check() if args.live else runner.run_checks()
    print(json.dumps({"passed": result["passed"], "mode": result["mode"],
                      "result": str(directory / "result.json")}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
