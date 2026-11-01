#!/usr/bin/env python3
"""Full-process isolated group memory acceptance with a test-only model stream."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


commands = load("group_memory_command_helpers", ROOT / "scripts/check-agent-commands.py")
expansion = commands.expansion

FIXTURE = r'''/** Test-only adapter for isolated memory consolidation; ordinary chat uses sandbox scripted. */
import { appendFileSync, readFileSync } from 'node:fs'

export const name = 'isolated-memory-model-fixture'
export const inject = ['llm']

export function apply(ctx, config = {}) {
  ctx.on('llm/stream', (options, next) => {
    if (options.sessionId) return next()
    return (async function* () {
      const mode = JSON.parse(readFileSync(config.modeFile, 'utf8')).mode
      const input = JSON.parse(options.messages?.[0]?.content?.[0]?.text ?? '{}')
      const rows = Array.isArray(input.messages) ? input.messages : []
      const a = rows.find(row => row.actor_id === '90001')
      const b = rows.find(row => row.actor_id === '90002')
      appendFileSync(config.callsFile, JSON.stringify({
        provider: options.provider, model: options.model, sessionId: options.sessionId ?? null,
        system: options.system, sourceIds: rows.map(row => row.journal_id),
        actors: rows.map(row => row.actor_id), mode,
      }) + '\n', { mode: 0o600 })
      const text = mode === 'malformed'
        ? 'not-json'
        : JSON.stringify({ changes: a && b ? [{
            content: 'A 提到周六联机；B 补充了另一段群聊经历。',
            members: ['90001', '90002'], source_ids: [a.journal_id, b.journal_id],
            claims: [
              {text: 'A提到的经历', sources: [{journal_id:a.journal_id, quote:a.content.text.slice(0,80)}]},
              {text: 'B补充的经历', sources: [{journal_id:b.journal_id, quote:b.content.text.slice(0,80)}]},
            ],
            weight: 3, status: 'active',
          }] : [] })
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text }
      yield { type: 'usage', usage: { inputTokens: 17, outputTokens: 9, totalTokens: 26 } }
      yield { type: 'block-end', index: 0, block: { type: 'text', text } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    })()
  })
}
'''


class GroupMemoryCheck(commands.ScriptedCommandCheck):
    group = "group:91000"

    def __init__(self, directory, mode="scripted"):
        super().__init__(directory)
        self.mode = mode
        self.fixture_path = directory / "memory-model-fixture.mjs"
        self.mode_path = directory / "memory-fixture-mode.json"
        self.calls_path = directory / "memory-model-calls.jsonl"

    def start(self):
        # dsh/plugins is a source link in the staged repo. Keep the test
        # fixture outside it and add only its absolute path after staging.
        # The public runner restages profiles on every invocation. This small
        # isolated launcher applies the fixture after that stage and then uses
        # the same runtime entrypoint, so the test-only plugin stays in data_dir.
        if self.mode == "live":
            missing = [name for name in ("V2_DSH_PROVIDER", "V2_DSH_MODEL", "DEEPSEEK_API_KEY")
                       if not os.environ.get(name)]
            assert not missing, "live sandbox requires existing runtime model config: " + ", ".join(missing)
        launcher = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "import os\n"
            "from sandbox.runtime import prepare, _run\n"
            f"root = Path({str(ROOT)!r})\n"
            f"data = Path({str(self.directory)!r})\n"
            f"fixture = Path({str(self.fixture_path)!r})\n"
            f"mode = Path({str(self.mode_path)!r})\n"
            f"calls = Path({str(self.calls_path)!r})\n"
            f"ports = {{'onebot': {self.onebot}, 'ui': {self.ui}, 'mcp': {self.mcp}}}\n"
            f"mode_name = {self.mode!r}\n"
            "if mode_name == 'scripted':\n"
            "    os.environ.setdefault('V2_DSH_PROVIDER', 'sandbox-scripted')\n"
            "    os.environ.setdefault('V2_DSH_MODEL', 'scripted')\n"
            "astrbot = root / '.runtime/astrbot'\n"
            "details = prepare(data, astrbot, ports, mode_name, '90009', snapshot_mode='text' if mode_name == 'live' else None)\n"
            "if mode_name == 'scripted':\n"
            f"    fixture.write_text({FIXTURE!r}, encoding='utf-8'); fixture.chmod(0o600)\n"
            "    mode.write_text('{\"mode\":\"malformed\"}', encoding='utf-8'); mode.chmod(0o600)\n"
            "    patch = Path(details['staged_repo']) / 'dsh/profile/v2-agent-group-sandbox.patch.yml'\n"
            f"    patch.open('a', encoding='utf-8').write('\\n- insert:\\n    - id: v2-memory-test-fixture\\n      name: {json.dumps(str(self.fixture_path))}\\n      config:\\n        modeFile: {json.dumps(str(self.mode_path))}\\n        callsFile: {json.dumps(str(self.calls_path))}\\n')\n"
            "raise SystemExit(_run(details, astrbot.resolve(), ports, mode_name, owner_volume_mib=32))\n"
        )

        self.log_path = ROOT / ".runtime" / (self.directory.name + ".runtime.log")
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen([sys.executable, "-c", launcher], cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"),
                  "isolated OneBot connection", timeout=90)
        self.workspace = self.directory / "repo"
        self.wait(lambda: self.journal_path().exists(), "canonical Journal startup")
        self.wait(lambda: (self.directory / "astrbot/data/plugin_data/v2_memory/groups.json").is_file(),
                  "registered group memory plugin", timeout=90)
        self.wait(lambda: len(self._memory_databases()) == 1,
                  "private group memory database initialized", timeout=90)

    def _memory_databases(self):
        return list((self.directory / "repo/.runtime/group-storage").glob("*/workspace/.v2-memory/state.sqlite3"))

    def _assert_memory_binding(self):
        manifest = json.loads((self.directory / "astrbot/data/plugin_data/v2_memory/groups.json").read_text())
        assert len(manifest["groups"]) == 1, manifest
        registered = manifest["groups"][0]
        assert (registered["platform_id"], registered["bot_id"], registered["group_id"]) == (
            "v2-sandbox", "99000", "91000"), manifest
        memory_db = self._memory_databases()[0]
        with sqlite3.connect(memory_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT platform_id,bot_id,conversation_key,initialized,cursor,enabled "
                             "FROM workspace").fetchone()
            assert row == ("v2-sandbox", "99000", "group:91000", 1, 0, 1), row
            assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        self.memory_key = registered["key"]
        return memory_db

    def _db_snapshot(self, memory_db):
        with sqlite3.connect(memory_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            workspace = dict(db.execute("SELECT * FROM workspace").fetchone())
            memories = [dict(row) for row in db.execute("SELECT * FROM memories")]
            runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id")]
        return workspace, memories, runs

    def _memory_new(self, text, expected, timeout=50):
        event = self.wake(self.group, text, sender=self.owner)
        output = self.wait(lambda: next((row for row in self.events()
                            if row["direction"] == "outbound"
                            and row["conversation"] == event["conversation"]
                            and int(row["message_id"]) > int(event["message_id"])), None),
                           "memory-aware /new response", timeout=timeout)
        self.wait(lambda: any(row["message_id"] == str(output["message_id"])
                              and row["direction"] == "outbound" for row in self.rows()),
                  "memory-aware /new reply in canonical Journal")
        assert expected in json.dumps(output, ensure_ascii=False), output
        return event, output

    def _check_live_model(self):
        memory_db = self._assert_memory_binding()
        assert not self.state(self.group), "plugin startup must not create a cognitive Session"
        assert not [event for event in self.events() if event["direction"] == "outbound"]

        messages = [
            ("90001", "我把这个月的工资攒起来了，暂时没动。"),
            ("90002", "我记得A之前说工资攒起来了；但我认为那笔钱后来拿去给游戏充值了，所以A其实没攒住。"),
            ("90002", "另外我几乎每天都在维护和提醒Bot。即使我暂停维护一周，我也希望它别忘了这段长期经历。"),
        ]
        inbound = []
        for sender, content in messages:
            inbound.append(self.wake(self.group, content, sender=sender, mention=False))
        rows = {row["message_id"]: row for row in self.rows()}
        source_rows = [rows[str(event["message_id"])] for event in inbound]
        source_ids = [row["journal_id"] for row in source_rows]
        assert all(row["direction"] == "inbound" for row in source_rows)
        assert not [event for event in self.events() if event["direction"] == "outbound"]

        started = time.monotonic()
        reset, _ = self._memory_new("/new", "已开始新的 Agent 对话", timeout=240)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        reset_row = next(row for row in self.rows() if row["message_id"] == str(reset["message_id"])
                         and row["conversation_key"] == self.group)
        workspace, memories, runs = self._db_snapshot(memory_db)
        assert memories, "live consolidation produced no memory"
        assert workspace["cursor"] == reset_row["journal_id"]
        assert runs[-1]["status"] == "completed"
        saved_ids = {source_id for memory in memories
                     for source_id in json.loads(memory["source_ids_json"])}
        assert saved_ids.intersection(source_ids), (saved_ids, source_ids)

        # Ask the real model to retrieve and source-check its just-created memories.
        verify_event = self.wake(
            self.group,
            "请用 memory_search 找到刚才关于A工资、B说的充值争议，以及群友长期维护Bot的记忆；再用 memory_sources 核实原消息，区分A和B各自说了什么。",
            sender=self.owner,
        )
        self.wait(lambda: any(item.get("event") == "turn/completed"
                              for item in self.trace(verify_event)),
                  "live memory retrieval turn completion", timeout=240)
        live_outputs = [row for row in self.events() if row["direction"] == "outbound"
                        and row["conversation"] == verify_event["conversation"]
                        and int(row["message_id"]) > int(verify_event["message_id"])]
        assert live_outputs, "live model produced no final or interim response"
        search_pairs = self.correlated(verify_event, "mcp__astrbot__memory_search")
        source_pairs = self.correlated(verify_event, "mcp__astrbot__memory_sources")
        assert search_pairs, "live model did not perform memory_search"
        assert source_pairs, "live model did not perform memory_sources"
        search_payloads = [self._native_payload(pair) for pair in search_pairs]
        source_payloads = [self._native_payload(pair) for pair in source_pairs]
        returned_memories = [memory for payload in search_payloads for memory in payload.get("memories", [])]
        returned_sources = [entry for payload in source_payloads for entry in payload.get("entries", [])]
        assert returned_memories and returned_sources
        observed_source_ids = {entry["journal_id"] for entry in returned_sources}
        assert observed_source_ids.intersection(source_ids), (observed_source_ids, source_ids)
        memory_text = "\n".join(memory["content"] for memory in memories)
        answer_text = json.dumps(live_outputs, ensure_ascii=False)
        model_requests = sum(item.get("event") == "model/start" for item in self.evidence()["lifecycle"])
        return {
            "model_provider": os.environ.get("V2_DSH_PROVIDER"),
            "model": runs[-1]["model"],
            "consolidation_elapsed_ms": elapsed_ms,
            "consolidation_usage": json.loads(runs[-1]["usage_json"]),
            "input_journal_ids": source_ids,
            "memory_source_journal_ids": sorted(saved_ids),
            "memory_count": len(memories),
            "model_requests_observed": model_requests,
            "memory_contents_for_review": [memory["content"] for memory in memories],
            "retrieval_results": returned_memories,
            "verified_source_journal_ids": sorted(observed_source_ids),
            "source_texts": [entry["content"]["text"] for entry in returned_sources],
            "observations": {
                "memory_mentions_salary_or_wage": any(token in memory_text for token in ("工资", "薪资")),
                "memory_mentions_recharge_or_game": any(token in memory_text for token in ("充值", "游戏")),
                "memory_mentions_bot_maintenance": any(token in memory_text for token in ("维护", "提醒")),
                "model_response_mentions_attribution": any(token in answer_text for token in ("A", "B")),
            },
            "live_model_semantics_automatically_judged": False,
        }

    def _check_new_and_mcp(self):
        memory_db = self._assert_memory_binding()
        assert not self.state(self.group), "plugin startup must not create a cognitive Session"
        assert not [event for event in self.events() if event["direction"] == "outbound"]

        # Create a genuine old group session first. A and B then send ordinary,
        # non-@ messages which must enter the Journal without waking DSH.
        self.plan(self.group, kind="reply", text="MEMORY_FIXTURE_SESSION_READY")
        prime = self.wake(self.group, "Owner primes the isolated group session",
                          sender=self.owner, mention=True)
        self._complete(prime)
        old_state = self.state(self.group)
        assert old_state and old_state["session_id"]
        old_sid = old_state["session_id"]

        outbound_before_members = {event["message_id"] for event in self.events()
                                    if event["direction"] == "outbound"}
        marker_a, marker_b = "MEMORY_A_SATURDAY_GAME", "MEMORY_B_RECALLS_DETAILS"
        event_a = self.wake(self.group, "A: 周六我们约好一起联机。" + marker_a,
                            sender="90001", mention=False)
        event_b = self.wake(self.group, "B: 我记得当时还讨论了游戏时间。" + marker_b,
                            sender="90002", mention=False)
        rows = {row["message_id"]: row for row in self.rows()}
        row_a, row_b = rows[str(event_a["message_id"])], rows[str(event_b["message_id"])]
        assert row_a["direction"] == row_b["direction"] == "inbound"
        assert {event["message_id"] for event in self.events() if event["direction"] == "outbound"} \
            == outbound_before_members

        before, _, _ = self._db_snapshot(memory_db)
        failed, _failed_output = self._memory_new("/new", "未完成")
        after_failure, memories_after_failure, failed_runs = self._db_snapshot(memory_db)
        assert self.state(self.group)["session_id"] == old_sid, "failed memory must preserve old Session ID"
        assert after_failure["cursor"] == before["cursor"] and not memories_after_failure
        assert failed_runs and failed_runs[-1]["status"] == "failed"

        # Explicit bypass rotates the session while preserving the uncollected
        # Journal range, which the following regular /new can then consolidate.
        _skipped, _ = self._memory_new("/new skip_mem", "已开始新的 Agent 对话")
        skipped_sid = self.state(self.group)["session_id"]
        assert skipped_sid != old_sid
        self.mode_path.write_text(json.dumps({"mode": "success"}), encoding="utf-8")
        self.mode_path.chmod(0o600)
        succeeded, _ = self._memory_new("/new", "已开始新的 Agent 对话")
        success_state = self.state(self.group)
        assert success_state["session_id"] not in {old_sid, skipped_sid}
        command_row = next(row for row in self.rows()
                           if row["message_id"] == str(succeeded["message_id"])
                           )
        assert success_state["history_after_journal_id"] == command_row["journal_id"]
        workspace, memories, runs = self._db_snapshot(memory_db)
        assert workspace["cursor"] == command_row["journal_id"]
        assert len(memories) == 1
        saved = memories[0]
        source_ids = json.loads(saved["source_ids_json"])
        assert source_ids == [row_a["journal_id"], row_b["journal_id"]], source_ids
        assert runs[-1]["status"] == "completed"
        assert runs[-1]["usage_json"] == json.dumps({"input_tokens": 17, "output_tokens": 9,
                                                      "total_tokens": 26}, separators=(",", ":"))

        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        assert [item["mode"] for item in calls] == ["malformed", "success"]
        assert all(item["sessionId"] is None for item in calls)
        assert {"90001", "90002"} <= set(calls[-1]["actors"])
        assert {row_a["journal_id"], row_b["journal_id"]} <= set(calls[-1]["sourceIds"])

        # Exercise the native MCP tools in the newly-created Session; results
        # are correlated to real ACP tool calls, not direct Python function calls.
        search_marker = "MEMORY_SEARCH_TOOL_COMPLETE"
        self.plan(self.group, kind="tool", name="mcp__astrbot__memory_search",
                  arguments={"query": "周六联机"}, text_after=search_marker)
        search_event = self.wake(self.group, "Please use memory_search", sender=self.owner)
        self._complete(search_event)
        search_pairs = self.wait(lambda: (found if len(found := self.correlated(
            search_event, "mcp__astrbot__memory_search")) == 1 else None),
            "native memory_search call and result")
        search_payload = self._native_payload(search_pairs[0])
        assert len(search_payload["memories"]) == 1
        assert search_payload["memories"][0]["id"] == saved["id"]

        sources_marker = "MEMORY_SOURCES_TOOL_COMPLETE"
        self.plan(self.group, kind="tool", name="mcp__astrbot__memory_sources",
                  arguments={"memory_id": saved["id"]}, text_after=sources_marker)
        sources_event = self.wake(self.group, "Please use memory_sources", sender=self.owner)
        self._complete(sources_event)
        source_pairs = self.wait(lambda: (found if len(found := self.correlated(
            sources_event, "mcp__astrbot__memory_sources")) == 1 else None),
            "native memory_sources call and result")
        source_payload = self._native_payload(source_pairs[0])
        assert [row["journal_id"] for row in source_payload["entries"]] == source_ids
        assert marker_a in source_payload["entries"][0]["content"]["text"]
        assert marker_b in source_payload["entries"][1]["content"]["text"]

        floor_marker = "MEMORY_HISTORY_FLOOR_COMPLETE"
        self.plan(self.group, kind="tool", name="mcp__astrbot__history_recent",
                  arguments={"limit": 100}, text_after=floor_marker)
        history_event = self.wake(self.group, "Please check recent history", sender=self.owner)
        self._complete(history_event)
        history_pairs = self.wait(lambda: (found if len(found := self.correlated(
            history_event, "mcp__astrbot__history_recent")) == 1 else None),
            "native history_recent call and result")
        recent = self._native_payload(history_pairs[0])["entries"]
        assert all(row["journal_id"] > command_row["journal_id"] for row in recent)
        assert not any(marker_a in json.dumps(row, ensure_ascii=False)
                       or marker_b in json.dumps(row, ensure_ascii=False) for row in recent)

        assert not [event for event in self.events() if event["direction"] == "outbound"
                    and event["conversation"]["id"] == "91000"
                    and int(event["message_id"]) > int(event_b["message_id"])
                    and int(event["message_id"]) < int(failed["message_id"])]
        # A manual collection is independent of conversation rotation and of
        # the daily 99-message threshold. Check both native command spellings.
        retained_state = self.state(self.group)
        manual, _ = self._memory_new("/memory", "记忆整理完成")
        assert self.state(self.group) == retained_state
        manual_workspace, _, _ = self._db_snapshot(memory_db)
        manual_id = next(row["journal_id"] for row in self.rows()
                         if row["message_id"] == str(manual["message_id"]))
        assert manual_workspace["cursor"] == manual_id
        self.mode_path.write_text('{"mode":"malformed"}', encoding="utf-8")
        self._memory_new("/整理记忆", "记忆整理失败")
        assert self.state(self.group) == retained_state
        assert self._db_snapshot(memory_db)[0]["cursor"] == manual_id
        memory_calls = len(self.calls_path.read_text().splitlines())
        denied = self.wake(self.group, "/memory", sender="90001")
        self.answer(denied, "仅 Bot Owner")
        assert len(self.calls_path.read_text().splitlines()) == memory_calls
        return {
            "memory_plugin_manifest_group": "group:91000",
            "startup_initialized_database": str(memory_db.relative_to(self.directory)),
            "startup_cognitive_sessions": 0,
            "non_mention_messages_recorded_without_reply": [event_a["message_id"], event_b["message_id"]],
            "failed_default_new_preserved_session": old_sid,
            "skip_mem_rotated_session": skipped_sid,
            "successful_default_new_rotated_session": success_state["session_id"],
            "memory_cursor_and_new_floor": command_row["journal_id"],
            "memory_source_journal_ids": source_ids,
            "native_mcp_calls": ["memory_search", "memory_sources", "history_recent"],
            "memory_llm_requests_had_no_session_id": True,
            "manual_memory_command_preserved_session_and_context": True,
            "manual_memory_failure_preserved_cursor": True,
            "manual_memory_non_owner_rejected": True,
            "scripted_provider_usage": json.loads(runs[-1]["usage_json"]),
            "actual_model_semantics_evaluated": False,
        }

    @staticmethod
    def _native_payload(pair):
        message = pair["result"]["message"]
        text = "".join(block.get("text", "") for block in message.get("content", [])
                       if block.get("type") == "text")
        value = json.loads(text)
        assert isinstance(value, dict), value
        return value


def run():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--live", action="store_true",
                        help="Run one synthetic-group consolidation and retrieval with the existing configured provider")
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.data_dir or ROOT / ".runtime" / ("group-memory-" + uuid.uuid4().hex[:12])
    mode = "live" if args.live else "scripted"
    check = GroupMemoryCheck(directory, mode=mode)
    result = {"passed": False, "mode": "isolated-live-provider" if args.live else "isolated-scripted-provider",
              "real_qq_connections": 0, "paid_model_calls": 0,
              "data_dir": str(directory)}
    try:
        check.start()
        if args.live:
            result["details"] = check._check_live_model()
            result["paid_model_calls"] = result["details"]["model_requests_observed"]
            result["checks"] = ["isolated live-model consolidation", "native memory retrieval and source verification"]
        else:
            result["details"] = check._check_new_and_mcp()
            result["checks"] = ["real AstrBot/DSH/OneBot startup", "group memory store initialization",
                                "failed /new preserves Session", "skip_mem bypass", "successful consolidation",
                                "native MCP memory search/source and history floor"]
        result["passed"] = True
    except BaseException as error:  # noqa: BLE001 -- preserve evidence on interrupted checks
        result.update(error_type=type(error).__name__, error=str(error))
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        try:
            if check.process is not None:
                try:
                    evidence = check.request("GET", "/api/evidence/export")
                    check.save("evidence.json", evidence)
                    if args.live:
                        result["paid_model_calls"] = sum(
                            item.get("event") == "model/start" for item in evidence.get("lifecycle", []))
                except Exception as error:  # noqa: BLE001 -- cleanup must continue if evidence API is gone
                    result["evidence_error"] = type(error).__name__
            check.stop()
            result["stopped"] = check.process is None
            if getattr(check, "log_path", None) and check.log_path.exists():
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(check.log_path, directory / "runtime.log")
                (directory / "runtime.log").chmod(0o600)
            # An interrupted runtime can leave a group volume mounted if its
            # process exits before the runner's normal finally path completes.
            group_root = directory / "repo/.runtime/group-storage"
            mounted_before_cleanup = []
            if group_root.exists():
                for workspace in group_root.glob("*/workspace"):
                    if os.path.ismount(workspace):
                        mounted_before_cleanup.append(str(workspace))
            result["normal_mount_cleanup"] = not mounted_before_cleanup
            if mounted_before_cleanup:
                helper_path = ROOT / "dsh/plugins/agent-profile/owner_volume.py"
                helper_spec = importlib.util.spec_from_file_location("group_memory_volume_cleanup", helper_path)
                helper = importlib.util.module_from_spec(helper_spec)
                helper_spec.loader.exec_module(helper)
                for workspace in map(Path, mounted_before_cleanup):
                    helper.unmount(workspace)
                result["forced_mount_cleanup"] = mounted_before_cleanup
            mountinfo = Path("/proc/self/mountinfo").read_text(errors="replace")
            result["mounts_released"] = not any(str(group_root) in line for line in mountinfo.splitlines())
            if not result["mounts_released"] or not result["normal_mount_cleanup"]:
                result["passed"] = False
        except BaseException as error:  # noqa: BLE001 -- preserve cleanup failures in the result
            result["cleanup_error"] = f"{type(error).__name__}: {error}"
            result["passed"] = False
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "result.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
