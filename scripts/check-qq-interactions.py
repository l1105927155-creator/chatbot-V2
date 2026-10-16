#!/usr/bin/env python3
"""Live-model checks for bare mentions, wait notices and plain-text QQ replies.

Each run creates a fresh isolated simulated QQ instance. It never connects to QQ.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "live_owner_helpers", ROOT / "scripts/check-sandbox-live-owner.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
correlation_spec = importlib.util.spec_from_file_location(
    "agent_sandbox_helpers", ROOT / "scripts/check-agent-sandbox.py"
)
correlation = importlib.util.module_from_spec(correlation_spec)
correlation_spec.loader.exec_module(correlation)


class InteractionCheck(helpers.LiveOwner):
    group = "group:91000"
    excluded = "group:91999"

    def __init__(self, directory):
        super().__init__(directory)
        self.results = []
        self.baseline_source_hashes = None
        self.final_model_calls = None

    @staticmethod
    def source_hashes():
        paths = [
            ROOT / "astrbot-plugins/v2_dsh_router/routing.py",
            ROOT / "astrbot-plugins/v2_dsh_router/mcp_server.py",
            ROOT / "astrbot-plugins/v2_dsh_router/runtime_audit.py",
            ROOT / "sandbox/runtime.py",
            ROOT / "dsh/plugins/agent-profile/decision-guide.mjs",
            ROOT / "dsh/profile/qq-agent.md",
            ROOT / "dsh/profile/agent-group-sandbox.md",
            Path(__file__).resolve(),
        ]
        return {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }

    def send_segments(self, key, segments, sender="90001"):
        kind, ident = key.split(":", 1)
        result = self.request(
            "POST",
            "/api/send",
            {
                "conversation": {"type": kind, "id": ident},
                "sender_id": sender,
                "message": segments,
            },
        )
        event = result["event"]
        mid = str(event["message_id"])
        self.wait(
            lambda: any(
                str(row["message_id"]) == mid
                and row["conversation_key"] == key
                and row["direction"] == "inbound"
                for row in self.rows()
            ),
            "canonical inbound Journal row",
        )
        return event

    def answer_pair(self, event, timeout=170):
        key = event["conversation"]["type"] + ":" + event["conversation"]["id"]
        self.wait(
            lambda: any(row.get("event") == "turn/completed" for row in self.trace(event)),
            "live DSH turn completion",
            timeout=timeout,
        )
        outgoing = [
            item
            for item in self.events()
            if item["direction"] == "outbound"
            and item["conversation"] == event["conversation"]
            and int(item["message_id"]) > int(event["message_id"])
        ]
        journal = self.rows()
        inbound_rows = [
            row for row in journal
            if row["message_id"] == str(event["message_id"])
            and row["conversation_key"] == key and row["direction"] == "inbound"
        ]
        assert len(inbound_rows) == 1, "completed turn must have one canonical inbound row"
        router = self.map_state(key)
        assert router is not None, "completed turn must have a Router mapping"
        assert router["pending_upper_cursor"] is None, "completed turn left Router pending"
        assert router["last_seen_journal_id"] == inbound_rows[0]["journal_id"], (
            "Router cursor should consume the current inbound only after the final output"
        )
        for item in outgoing:
            assert any(
                row["message_id"] == str(item["message_id"])
                and row["conversation_key"] == key
                and row["direction"] == "outbound"
                for row in journal
            ), f"outbound {item['message_id']} missing from canonical Journal"
        return outgoing

    def group_workspace(self, group_id):
        import sys
        sys.path.insert(0, str(ROOT / "dsh/plugins/agent-profile"))
        import group_volume
        return Path(group_volume.paths(
            self.workspace / ".runtime/group-storage", "v2-sandbox", "99000", str(group_id)
        )["workspace"])

    def native_calls(self, event, tool):
        """Return every exact call/result, including denied calls, for one turn."""
        trace = self.trace(event)
        dispatched = next((row for row in trace if row.get("event") == "dsh/dispatched"), None)
        assert dispatched and dispatched.get("session_id"), "turn has no dispatched native DSH session"
        sid = dispatched["session_id"]
        matches = []
        for path in (self.directory / "repo/.runtime").glob("dsh-home-*"):
            for session in path.rglob("session.v4.jsonl"):
                if session.parent.name != sid:
                    continue
                calls = {}
                results = {}
                for line in session.read_text().splitlines():
                    item = json.loads(line)
                    data = item.get("data", {})
                    if item.get("type") == "tool/call":
                        call_id = data.get("callId")
                        assert call_id not in calls, "duplicate native tool call ID"
                        calls[call_id] = data
                    elif item.get("type") == "tool/result":
                        message = data.get("message", {})
                        results.setdefault(message.get("toolCallId"), []).append(message)
                starts = {
                    row["call_id"]: row
                    for row in trace
                    if row.get("event") == "tool/start" and row.get("tool") == tool
                }
                ends = {
                    row["call_id"]: row
                    for row in trace
                    if row.get("event") == "tool/end"
                }
                for call_id, call in calls.items():
                    if call.get("name") != tool:
                        continue
                    hashed = hashlib.sha256(str(call_id).encode()).hexdigest()[:24]
                    assert hashed in starts, "native call has no matching audit tool/start"
                    assert hashed in ends, "native call has no matching audit tool/end"
                    exact_results = results.get(call_id, [])
                    assert len(exact_results) == 1, "native call must map to exactly one result"
                    matches.append({
                        "call": call, "result": {"message": exact_results[0]},
                        "audit_call_id": hashed,
                        "audit_start": starts[hashed], "audit_end": ends[hashed],
                    })
        assert matches, f"no native {tool} calls found in dispatched SID {sid}"
        return matches

    @staticmethod
    def text(outbound):
        return "".join(
            segment.get("data", {}).get("text", "")
            for segment in outbound.get("event", {}).get("message", [])
            if segment.get("type") == "text"
        )

    def map_state(self, key):
        return next(
            (row for row in self.evidence(key)["states"] if row["conversation_key"] == key),
            None,
        )

    def dispatch_count(self, event):
        return sum(row.get("event") == "dsh/dispatched" for row in self.trace(event))

    def record_case(self, name, operation):
        started = time.monotonic()
        try:
            row = {"case": name, "passed": True, "details": operation() or {}}
        except Exception as exc:
            row = {
                "case": name,
                "passed": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        try:
            self.save(
                "evidence/" + name + ".json",
                {
                    "transport": self.http("/api/state"),
                    "evidence": self.request("GET", "/api/evidence/export"),
                },
            )
        except Exception as exc:
            row["evidence_save_error"] = type(exc).__name__ + ": " + str(exc)
            row["passed"] = False
        self.results.append(row)
        self.save("progress.json", self.results)
        print(
            json.dumps({k: v for k, v in row.items() if k != "traceback"}, ensure_ascii=False),
            flush=True,
        )
        return row["passed"]

    def nonwake(self, name, key, segments, *, sender="90001"):
        states_before = self.evidence()["states"]
        models_before = self.model_count()
        baseline = max((int(item["message_id"]) for item in self.events()), default=0)
        event = self.send_segments(key, segments, sender=sender)
        time.sleep(1.25)
        assert not [
            item
            for item in self.events()
            if item["direction"] == "outbound"
            and item["conversation"] == event["conversation"]
            and int(item["message_id"]) > baseline
        ], f"{name}: unexpected outbound"
        assert not self.dispatch_count(event), f"{name}: DSH dispatched"
        assert self.model_count() == models_before, f"{name}: model invoked"
        assert self.evidence()["states"] == states_before, f"{name}: Router state changed"
        input_rows = [
            row
            for row in self.rows()
            if row["message_id"] == str(event["message_id"])
            and row["conversation_key"] == key
            and row["direction"] == "inbound"
        ]
        assert len(input_rows) == 1, f"{name}: expected one canonical inbound row"
        return {
            "inbound_id": event["message_id"],
            "journal_id": input_rows[0]["journal_id"],
            "no_outbound": True,
            "no_model": True,
            "no_dispatch": True,
            "router_unchanged": True,
        }

    def run(self):
        started = time.monotonic()
        self.baseline_source_hashes = self.source_hashes()
        try:
            self.start()
            state = self.http("/api/state")
            assert state["scenario"]["owner_id"] == self.owner
            assert self.group in state["scenario"]["admitted"]
            assert self.excluded not in state["scenario"]["admitted"]
            # The default simulator declares only the primary test group.
            # Declare the negative-control group explicitly without admitting it.
            self.request("POST", "/api/conversations", {"type": "group", "id": "91999", "title": "未准入对照群"})
            assert self.excluded not in self.http("/api/scenario")["admitted"]
            image_asset = next(
                asset for asset in self.http("/api/state")["assets"] if asset["name"] == "fixture.png"
            )

            negatives = [
                ("mention-other", self.group, [{"type": "at", "data": {"qq": "90002"}}]),
                ("mention-all", self.group, [{"type": "at", "data": {"qq": "all"}}]),
                ("unmentioned-text", self.group, [{"type": "text", "data": {"text": "只发文字，不唤醒 Bot"}}]),
                ("image-only", self.group, [{"type": "image", "data": {"file": "sandbox://" + image_asset["id"]}}]),
                ("unadmitted-pure-mention", self.excluded, [{"type": "at", "data": {"qq": "99000"}}]),
            ]
            for name, key, segments in negatives:
                if not self.record_case(name, lambda n=name, k=key, s=segments: self.nonwake(n, k, s)):
                    raise RuntimeError("negative wake gate failed: " + name)

            def bare_mention():
                before = self.model_count()
                event = self.send_segments(self.group, [{"type": "at", "data": {"qq": "99000"}}])
                outgoing = self.answer_pair(event)
                assert len(outgoing) == 1, f"bare mention should receive one reply, got {len(outgoing)}"
                assert self.text(outgoing[0]).strip(), "bare mention reply was empty"
                assert self.dispatch_count(event) == 1
                assert self.model_count() == before + 1
                trace = self.trace(event)
                assert sum(row.get("event") == "send/receipt" and row.get("message_id") == str(outgoing[0]["message_id"])
                           for row in trace) == 1
                sid = next(row["session_id"] for row in trace if row.get("event") == "dsh/dispatched")
                mapping = self.map_state(self.group)
                assert mapping and mapping["session_id"] == sid and mapping["pending_upper_cursor"] is None
                in_journal = [row for row in self.rows() if row["message_id"] == str(event["message_id"])
                              and row["conversation_key"] == self.group and row["direction"] == "inbound"]
                out_journal = [row for row in self.rows() if row["message_id"] == str(outgoing[0]["message_id"])
                               and row["conversation_key"] == self.group and row["direction"] == "outbound"]
                assert len(in_journal) == len(out_journal) == 1
                return {
                    "inbound_id": event["message_id"], "outbound_id": outgoing[0]["message_id"],
                    "reply_text": self.text(outgoing[0]), "session_id": sid,
                    "journal_ids": [in_journal[0]["journal_id"], out_journal[0]["journal_id"]],
                    "model_invoked_once": True, "receipt_confirmed": True,
                }

            if not self.record_case("bare-bot-mention", bare_mention):
                raise RuntimeError("bare mention failed")

            def restart_without_replay():
                before_states = self.evidence()["states"]
                before_events = [item["message_id"] for item in self.events()]
                before_rows = self.rows()
                before_models = self.model_count()
                self.stop()
                self.start()
                assert self.evidence()["states"] == before_states
                assert [item["message_id"] for item in self.events()] == before_events
                assert self.rows() == before_rows
                assert self.model_count() == before_models
                return {"router_state_preserved": True, "canonical_journal_preserved": True,
                        "events_unreplayed": True, "model_not_reinvoked": True}

            if not self.record_case("restart-no-replay", restart_without_replay):
                raise RuntimeError("restart preservation failed")

            def whitespace_mention():
                before = self.model_count()
                event = self.send_segments(self.group, [
                    {"type": "at", "data": {"qq": "99000"}},
                    {"type": "text", "data": {"text": " \n\t"}},
                ])
                outgoing = self.answer_pair(event)
                assert len(outgoing) == 1 and self.text(outgoing[0]).strip()
                assert outgoing[0]["event"].get("message") and all(
                    segment.get("type") == "text" for segment in outgoing[0]["event"]["message"]
                ), "quick exchange did not use text-only OneBot segments"
                assert self.dispatch_count(event) == 1 and self.model_count() == before + 1
                assert self.map_state(self.group)["pending_upper_cursor"] is None
                return {"inbound_id": event["message_id"], "outbound_id": outgoing[0]["message_id"],
                        "reply_text": self.text(outgoing[0]), "model_invoked_once": True}

            if not self.record_case("whitespace-bot-mention", whitespace_mention):
                raise RuntimeError("whitespace mention failed")

            def quick_exchange():
                before = self.model_count()
                event = self.send_segments(
                    self.group,
                    [{"type": "at", "data": {"qq": "99000"}},
                     {"type": "text", "data": {"text": "简单聊一句：今天喝茶还是咖啡更适合提神？"}}],
                )
                outgoing = self.answer_pair(event)
                assert len(outgoing) == 1 and self.text(outgoing[0]).strip()
                tools = [row.get("tool") for row in self.trace(event) if row.get("event") == "tool/start"]
                assert not tools, f"quick exchange called tools: {tools}"
                assert self.model_count() == before + 1
                return {"inbound_id": event["message_id"], "outbound_id": outgoing[0]["message_id"],
                        "reply_text": self.text(outgoing[0]), "tool_calls": tools,
                        "no_wait_notice": True, "no_tools": True}

            if not self.record_case("quick-exchange-no-wait-or-tool", quick_exchange):
                raise RuntimeError("quick exchange failed")

            def wait_before_shell(conversation_key, sender, workspace, *, mention=False):
                filename = "qq-interaction-" + uuid.uuid4().hex[:10] + ".txt"
                marker = "QQ_INTERACTION_" + uuid.uuid4().hex[:12]
                command = "sleep 8; printf " + marker + " > " + filename + "; cat " + filename
                cursor = self.http("/api/events")["cursor"]
                prompt = "请在本工作区做一次安全检查：运行命令 `" + command + "` 并核对实际输出。"
                segments = [{"type": "text", "data": {"text": prompt}}]
                if mention:
                    segments.insert(0, {"type": "at", "data": {"qq": "99000"}})
                event = self.send_segments(conversation_key, segments, sender=sender)
                started_tool = self.wait_native(
                    {"event": "tool/start", "tool": "bash", "conversation_key": conversation_key},
                    after=cursor, timeout=120,
                )
                trace = self.trace(event)
                bash_start = next(row for row in trace if row.get("event") == "tool/start" and row.get("tool") == "bash")
                notice_start = next((row for row in trace if row.get("event") == "tool/start"
                                     and row.get("tool") == "mcp__astrbot__qq_send_origin"), None)
                outs_at_start = [item for item in self.events()
                                 if item["direction"] == "outbound" and item["conversation"] == event["conversation"]
                                 and int(item["message_id"]) > int(event["message_id"])]
                marker_path = workspace / filename
                marker_absent_at_start = not marker_path.exists()
                bash_incomplete_at_start = not any(
                    row.get("event") == "tool/end" and row.get("tool") == "bash"
                    and row.get("status") == "completed" for row in trace
                )
                notice_preceded_bash = bool(
                    notice_start and trace.index(notice_start) < trace.index(bash_start)
                    and len(outs_at_start) == 1 and self.text(outs_at_start[0]).strip()
                )
                # Native call/result records are correlated only after the
                # long-running Bash invocation has completed.
                outgoing = self.answer_pair(event)
                final_trace = self.trace(event)
                notice_pairs = self.native_calls(event, "mcp__astrbot__qq_send_origin")
                bash_pairs = self.native_calls(event, "bash")
                assert len(notice_pairs) == 1, "expected one native wait notice call/result"
                notice_result = notice_pairs[0]["result"]["message"]
                assert not notice_result.get("isError") and notice_pairs[0]["audit_end"].get("status") == "completed"
                notice_receipt = next((row for row in final_trace if row.get("event") == "send/receipt"
                                       and row.get("message_id") in {str(item["message_id"]) for item in outs_at_start}), None)
                assert notice_receipt, "wait notice has no exact outbound send receipt"
                bash_starts = [row for row in final_trace if row.get("event") == "tool/start" and row.get("tool") == "bash"]
                assert bash_starts, "no Bash tool starts were audited"
                notice_end = notice_pairs[0]["audit_end"]
                assert all(final_trace.index(notice_receipt) < final_trace.index(row)
                           and final_trace.index(notice_end) < final_trace.index(row)
                           for row in bash_starts), "wait notice receipt/result did not complete before every Bash call"
                assert all(pair["result"]["message"].get("toolCallId") == pair["call"].get("callId")
                           for pair in bash_pairs), "Bash call/result IDs do not match exactly"
                assert all(pair["audit_end"].get("status") in {"failed", "completed"} for pair in bash_pairs)
                successful_bash = [pair for pair in bash_pairs
                                   if not pair["result"]["message"].get("isError")
                                   and pair["audit_end"].get("status") == "completed"]
                assert any(marker in json.dumps(pair["result"], ensure_ascii=False) for pair in successful_bash), (
                    "no successful Bash result contains the requested marker"
                )
                assert marker_path.is_file(), "completed Bash did not create its requested marker"
                assert marker_path.read_text() == marker
                assert len(outgoing) == (2 if notice_preceded_bash else 1), (
                    "expected one final reply and an optional preceding wait notice"
                )
                if notice_preceded_bash:
                    assert len(outgoing) == 2, "expected exactly one wait notice plus one final reply"
                    assert outgoing[0]["message_id"] < outgoing[1]["message_id"]
                    wait_text, final_text = self.text(outgoing[0]), self.text(outgoing[1])
                    assert wait_text.strip() and final_text.strip()
                    assert wait_text not in final_text, "final reply repeats the wait notice verbatim"
                    notice_call = notice_pairs[0]["call"]["callId"]
                else:
                    assert False, "qq_send_origin call exists but no advance wait message preceded Bash"
                    final_text = self.text(outgoing[0])
                    notice_call = None
                bash_call_ids = [pair["call"]["callId"] for pair in bash_pairs]
                assert len(set(bash_call_ids)) == len(bash_call_ids), "Bash native call IDs are not unique"
                assert notice_call not in bash_call_ids
                for item in outgoing:
                    segments = item["event"].get("message")
                    assert segments and all(segment.get("type") == "text" for segment in segments), (
                        "wait/final response was not sent as non-empty text-only OneBot segments"
                    )
                assert marker in final_text
                assert notice_preceded_bash, "model did not send an advance wait notice before Bash"
                trace = self.trace(event)
                receipt_ids = [row["message_id"] for row in trace if row.get("event") == "send/receipt"]
                for item in outgoing:
                    assert receipt_ids.count(str(item["message_id"])) == 1
                journal = self.rows()
                journal_out = [row for row in journal if row["conversation_key"] == conversation_key
                               and row["direction"] == "outbound"
                               and row["message_id"] in {str(item["message_id"]) for item in outgoing}]
                assert len(journal_out) == len(outgoing)
                return {
                    "inbound_id": event["message_id"],
                    "wait_message_id": outgoing[0]["message_id"] if notice_preceded_bash else None,
                    "final_message_id": outgoing[-1]["message_id"], "marker": marker,
                    "marker_file": str(marker_path), "marker_absent_at_bash_start": marker_absent_at_start,
                    "bash_incomplete_at_bash_start": bash_incomplete_at_start,
                    "wait_notice_preceded_bash": notice_preceded_bash,
                    "ordered_tools": ([notice_start["tool"]] if notice_start else []) + [bash_start["tool"]],
                    "native_calls": ([{"tool": "qq_send_origin", "call_id": notice_call,
                                       "result": notice_pairs[0]["result"],
                                       "audit_status": notice_pairs[0]["audit_end"]["status"]}]
                                       if notice_pairs else [])
                                     + [{"tool": "bash", "call_id": pair["call"]["callId"],
                                         "arguments": pair["call"].get("arguments"),
                                         "result": pair["result"],
                                         "audit_status": pair["audit_end"]["status"]}
                                        for pair in bash_pairs],
                    "send_receipts": receipt_ids, "journal_outbound_rows": len(journal_out),
                    "no_duplicate_reply": True,
                }

            self.record_case("advance-wait-before-owner-shell", lambda: wait_before_shell(
                self.key, self.owner, self.workspace
            ))

            def group_wait_before_shell():
                return wait_before_shell(
                    self.group, "90001", self.group_workspace("91000"), mention=True
                )

            self.record_case("advance-wait-before-group-shell", group_wait_before_shell)

            def natural_plain_text():
                before = self.model_count()
                prompt = (
                    "请查阅 DeepSeek 官方文档，用两点编号简要说明 API 请求里 model 和 messages 两个字段各自的作用，"
                    "并给我对应的官方原始页面网址，便于核对。"
                )
                cursor = self.http("/api/events")["cursor"]
                event = self.send_segments(self.group, [
                    {"type": "at", "data": {"qq": "99000"}},
                    {"type": "text", "data": {"text": prompt}},
                ])
                outgoing = self.answer_pair(event)
                assert 1 <= len(outgoing) <= 2, f"unexpected research replies: {len(outgoing)}"
                answer = self.text(outgoing[-1])
                assert answer.strip()
                segments = outgoing[-1]["event"].get("message")
                assert segments and all(segment.get("type") == "text" for segment in segments), (
                    "QQ reply must contain one or more text segments only"
                )
                assert self.model_count() > before
                starts = [row for row in self.trace(event) if row.get("event") == "tool/start"]
                search_pairs = self.correlated(event, "web_search")
                assert search_pairs, "no successful native web_search call for official documentation"
                schemas = [pair for pair in search_pairs]
                sources = [
                    source.get("url", "")
                    for pair in schemas
                    for source in (pair["result"].get("meta") or {}).get("sources", [])
                ]
                plain_urls = re.findall(r"https?://[^\s<>\]})]+", answer)
                assert plain_urls, "reply contains no raw URL"
                normalized = {url.rstrip(".,;:!?，。；：！？") for url in plain_urls}
                assert any(source and source in normalized for source in sources), (
                    "reply raw URL does not match a URL returned by native search"
                )
                assert re.search(r"(?m)^\s*1[.、)]\s*", answer), "reply did not follow the requested two-item numbering"
                markdown = {
                    "heading": bool(re.search(r"(?m)^\s{0,3}#{1,6}\s", answer)),
                    "bold": "**" in answer or "__" in answer,
                    "inline_code": "`" in answer,
                    "fenced_code": "```" in answer,
                    "table": bool(re.search(r"(?m)^\s*\|.*\|\s*$", answer)),
                    "markdown_link": bool(re.search(r"\[[^\]]+\]\(https?://", answer)),
                }
                assert not any(markdown.values()), f"Markdown formatting in QQ reply: {markdown}"
                trace = self.trace(event)
                for item in outgoing:
                    item_segments = item["event"].get("message")
                    assert item_segments and all(segment.get("type") == "text" for segment in item_segments)
                    assert sum(row.get("event") == "send/receipt"
                               and row.get("message_id") == str(item["message_id"]) for row in trace) == 1
                return {"inbound_id": event["message_id"], "outbound_id": outgoing[-1]["message_id"],
                        "wait_message_id": outgoing[0]["message_id"] if len(outgoing) == 2 else None,
                        "reply_text": answer, "native_search_calls": [
                            {"call_id": pair["call"]["callId"], "sources": (pair["result"].get("meta") or {}).get("sources", [])}
                            for pair in schemas], "raw_urls": plain_urls, "numbered_items": True,
                        "markdown": markdown, "receipt_confirmed": True}

            self.record_case("natural-numbered-source-plain-text", natural_plain_text)
        except Exception as exc:
            self.results.append({"case": "fatal", "passed": False, "error_type": type(exc).__name__,
                                 "error": str(exc), "traceback": traceback.format_exc()})
        finally:
            if self.process and self.process.poll() is None:
                try:
                    self.final_model_calls = self.model_count()
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                    self.save("transport.json", self.http("/api/state"))
                except Exception as exc:
                    self.results.append({"case": "final-evidence-export", "passed": False,
                                         "error": type(exc).__name__ + ": " + str(exc)})
            self.stop()
            try:
                from sandbox.evidence import EvidenceSnapshotter
                self.save("stopped-evidence.json", EvidenceSnapshotter(self.directory).snapshot(limit=1000))
            except Exception as exc:
                self.results.append({"case": "stopped-evidence", "passed": False,
                                     "error": type(exc).__name__ + ": " + str(exc)})
        source_hashes_after = self.source_hashes()
        result = {
            "passed": bool(self.results) and all(row.get("passed") for row in self.results)
            and source_hashes_after == self.baseline_source_hashes,
            "mode": "live", "provider": os.environ.get("V2_DSH_PROVIDER"),
            "model": os.environ.get("V2_DSH_MODEL"), "execution_contract": 2,
            "owner_id": self.owner, "bot_id": "99000", "platform_id": "v2-sandbox",
            "data_dir": str(self.directory), "real_qq_connections": 0,
            "model_calls": self.final_model_calls,
            "cases": self.results, "source_sha256_before": self.baseline_source_hashes,
            "source_sha256_after": source_hashes_after,
            "sources_unchanged_during_run": source_hashes_after == self.baseline_source_hashes,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "limitations": [
                "模拟 QQ 身份与本地 OneBot；没有验证真实 QQ、NapCat、客户端渲染或平台权限。",
                "DeepSeek 模型行为为本次样本；不同请求可能选择不同表述或工具。",
                "纯文本断言覆盖标题、粗体、表格、行内代码、代码围栏及 Markdown 链接；不验证所有 Markdown 方言。",
                "在线证据按有界 API 导出，停止后的 SQLite/审计快照用于复核；各事实源不是原子快照。",
            ],
        }
        self.save("result.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if not all(os.environ.get(key) for key in ("DEEPSEEK_API_KEY", "V2_DSH_PROVIDER", "V2_DSH_MODEL")):
        parser.error("live mode requires provider/model/key in process environment")
    if os.environ.get("V2_DSH_PROVIDER") != "deepseek-official" or os.environ.get("V2_DSH_MODEL") != "deepseek-v4-flash":
        parser.error("this acceptance is pinned to deepseek-official/deepseek-v4-flash")
    os.umask(0o077)
    directory = (args.data_dir or ROOT / ".runtime" / ("qq-interactions-" + uuid.uuid4().hex[:12])).resolve()
    if directory.exists():
        parser.error("fresh isolated data directory required")
    result = InteractionCheck(directory).run()
    print(json.dumps({"passed": result["passed"], "cases": len(result["cases"]),
                      "result": str(directory / "result.json")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
