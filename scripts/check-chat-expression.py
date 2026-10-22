#!/usr/bin/env python3
"""Paired real-Flash expression evaluation through isolated AstrBot/DSH and OneBot.

This runner checks transport/evidence integrity only. It never declares a style
variant successful: every generated review packet requires human scoring.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / "tests/fixtures/chat-expression-guides.json"
SCENARIOS = ROOT / "tests/fixtures/chat-expression-scenarios.json"
ASTRBOT = ROOT / ".runtime/astrbot"
OWNER = "90009"
BOT = "99000"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def digest(text: str | bytes) -> str:
    value = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(value).hexdigest()


def load_inputs(guides_path: Path = GUIDES, scenarios_path: Path = SCENARIOS) -> tuple[dict, dict]:
    guides = json.loads(guides_path.read_text(encoding="utf-8"))
    scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
    if guides.get("version") != 1 or scenarios.get("version") != 1:
        raise ValueError("unsupported chat expression fixture version")
    if not {"principles", "examples"} <= set(guides):
        raise ValueError("guide fixture must contain principles and examples variants")
    for variant in ("principles", "examples"):
        row = guides[variant]
        if not all(isinstance(row.get(key), str) and row[key].strip()
                   for key in ("decision_guide", "profile_reply", "send_tool_description")):
            raise ValueError(f"guide variant {variant} is missing a candidate string")
    seen = set()
    for scenario in scenarios.get("scenarios", []):
        sid = scenario.get("id")
        if not sid or sid in seen:
            raise ValueError(f"missing or duplicate scenario id: {sid!r}")
        seen.add(sid)
        if scenario.get("conversation") not in {"group:91000", f"private:{OWNER}"}:
            raise ValueError(f"scenario {sid} uses an unsupported conversation")
        turns = scenario.get("turns")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 8:
            raise ValueError(f"scenario {sid} must have 1..8 turns")
        if any(not isinstance(turn.get("user"), str) or not turn["user"].strip() for turn in turns):
            raise ValueError(f"scenario {sid} has an empty user turn")
    total = sum(len(s["turns"]) for s in scenarios["scenarios"])
    if not total or not seen:
        raise ValueError("at least one dialogue scenario is required")
    return guides, scenarios


def _replace_profile_reply(path: Path, candidate: str) -> None:
    original = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?m)^(?:QQ 回复|因为 QQ 客户端不支持Markdown渲染).*$",
        lambda _: candidate, original, count=1,
    )
    if count != 1:
        raise ValueError(f"expected one QQ reply policy paragraph in {path}")
    path.write_text(updated, encoding="utf-8")


def _replace_tool_description(path: Path, candidate: str) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next((node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == "qq_send_origin"), None)
    if target is None or not target.body or not isinstance(target.body[0], ast.Expr):
        raise ValueError("staged qq_send_origin declaration or description not found")
    expression = target.body[0]
    if not isinstance(expression.value, ast.Constant) or not isinstance(expression.value.value, str):
        raise ValueError("staged qq_send_origin description is not a literal docstring")
    lines = source.splitlines(keepends=True)
    indentation = " " * expression.col_offset
    # A Python string literal in first-expression position remains a native
    # function docstring, while repr safely handles all candidate punctuation.
    replacement = [indentation + repr(candidate) + "\n"]
    lines[expression.lineno - 1:expression.end_lineno] = replacement
    updated = "".join(lines)
    ast.parse(updated)
    path.write_text(updated, encoding="utf-8")


def stage_candidate(details: dict, candidate: dict) -> dict:
    """Make the sandbox's DSH plugins private, then apply only candidate text."""
    staged_repo = Path(details["staged_repo"])
    dsh = staged_repo / "dsh"
    plugins = dsh / "plugins"
    if not plugins.is_symlink() or plugins.resolve() != (ROOT / "dsh/plugins").resolve():
        raise ValueError("expected staged dsh/plugins to point at the shared source")
    plugins.unlink()
    shutil.copytree(ROOT / "dsh/plugins", plugins,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), symlinks=True)
    profile = plugins / "agent-profile"
    (profile / "decision-guide.md").write_text(candidate["decision_guide"].rstrip() + "\n", encoding="utf-8")
    profiles = dsh / "profile"
    for name in ("agent-owner.md", "agent-group.md", "agent-group-sandbox.md"):
        _replace_profile_reply(profiles / name, candidate["profile_reply"])

    staged_plugin = staged_repo / "astrbot-plugins/v2_dsh_router"
    mcp_path = staged_plugin / "mcp_server.py"
    _replace_tool_description(mcp_path, candidate["send_tool_description"])
    service_path = staged_plugin / "service.py"
    service = service_path.read_text(encoding="utf-8")
    split_call = "for part in split_final_reply(reply):\n                    await send(part)"
    if split_call in service:
        service = service.replace(split_call, "await send(reply)", 1)
        split_override = "staged-final-send-is-single-message"
    elif "split_final_reply(reply)" not in service and "split_final_reply" not in service:
        split_override = "source-already-sends-one-final-message"
    else:
        raise ValueError("unrecognized staged final-reply split implementation")
    service_path.write_text(service, encoding="utf-8")

    # Preserve actual request system messages when web/tool context exceeds the
    # probe's bounded transcript budget. This changes evidence projection only,
    # never messages passed to the model, and stays private to this sandbox.
    probe_path = staged_repo / "sandbox/probe.mjs"
    if probe_path.is_file():
        probe = probe_path.read_text(encoding="utf-8")
        marker = "  // Retain the newest model context, where native tool results appear, rather than"
        projection = ("  const systemProjection = JSON.stringify((messages ?? []).filter(message => message.role === 'system'))\n"
                      "  if (systemProjection.length <= MAX_INPUT_CHARS) return { input: systemProjection, "
                      "input_projection: 'system-only', input_truncated: true }\n")
        if marker not in probe:
            raise ValueError("unrecognized sandbox probe input projection")
        probe_path.write_text(probe.replace(marker, projection + marker, 1), encoding="utf-8")

    # The DSH package tree remains shared and read-only; all mutable profile and
    # application source used by this experiment is private to this directory.
    module_link = dsh / "node_modules"
    if not module_link.is_symlink() or module_link.resolve() != (ROOT / ".runtime/dsh-runtime/node_modules").resolve():
        raise ValueError("pinned DSH node_modules link is not intact after staging")
    expected = {
        "decision_guide": digest(candidate["decision_guide"].rstrip() + "\n"),
        "profile_reply": digest(candidate["profile_reply"]),
        "send_tool_description": digest(candidate["send_tool_description"]),
        "candidate_character_counts": {key: len(candidate[key]) for key in (
            "decision_guide", "profile_reply", "send_tool_description")},
        "candidate_utf8_bytes": {key: len(candidate[key].encode("utf-8")) for key in (
            "decision_guide", "profile_reply", "send_tool_description")},
        "staged_mcp_server": digest(mcp_path.read_bytes()),
        "staged_service": digest(service_path.read_bytes()),
        "shared_plugins_source_unchanged": digest((ROOT / "dsh/plugins/agent-profile/decision-guide.md").read_bytes()),
        "node_modules_target": str(module_link.resolve()),
        "owner_volume_module_present": (profile / "owner_volume.py").is_file(),
        "final_send_mode": split_override,
        "bounded_system_projection": probe_path.is_file(),
    }
    if not expected["owner_volume_module_present"]:
        raise ValueError("private agent-profile copy is missing owner-volume support")
    (staged_repo / "chat-expression-stage.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return expected


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def prompt_loading(candidate: dict, model_events: list[dict]) -> dict:
    prompts = [row.get("input", "") for row in model_events if row.get("event") == "model/start"]
    decoded = []
    for prompt in prompts:
        try:
            decoded.append(list(_strings(json.loads(prompt))))
        except (json.JSONDecodeError, TypeError):
            decoded.append([prompt])
    guide_lines = [line for line in candidate["decision_guide"].splitlines() if line.strip()]
    profile_lines = [line for line in candidate["profile_reply"].splitlines() if line.strip()]
    check = lambda lines: {line: any(line in part for prompt in decoded for part in prompt) for line in lines}
    guide_hits = check(guide_lines)
    profile_hits = check(profile_lines)
    return {
        "model_request_count": len(prompts),
        "input_bytes": [row.get("input_bytes") for row in model_events if row.get("event") == "model/start"],
        "input_chars": [len(row.get("input", "")) for row in model_events if row.get("event") == "model/start"],
        "input_sha256": [row.get("input_sha256") for row in model_events if row.get("event") == "model/start"],
        "guide_loaded_lines": guide_hits,
        "profile_reply_loaded_lines": profile_hits,
        "all_guide_lines_observed": bool(guide_lines) and all(guide_hits.values()),
        "all_profile_reply_lines_observed": bool(profile_lines) and all(profile_hits.values()),
        "tool_description_in_prompt": any(candidate["send_tool_description"] in part
                                           for prompt in decoded for part in prompt),
        "tool_availability_observed": any("mcp__astrbot__qq_send_origin" in row.get("tools", [])
                                           for row in model_events if row.get("event") == "model/start"),
    }


def verify_receipt_join(outbound: list[dict], receipts: list[dict], journal: list[dict],
                        conversation_key: str) -> dict:
    """Pure verifier: all actual messages need one receipt and canonical Journal row."""
    out_by_id = {str(row.get("message_id")): row for row in outbound}
    receipt_ids = [str(row.get("message_id")) for row in receipts if row.get("message_id") is not None]
    journal_ids = {str(row.get("message_id")) for row in journal
                   if row.get("direction") == "outbound" and row.get("conversation_key") == conversation_key}
    return {
        "outbound_count": len(outbound),
        "receipt_count": len(receipt_ids),
        "journal_count": len(journal_ids),
        "unique_receipt_ids": len(receipt_ids) == len(set(receipt_ids)),
        "same_ids_in_transport_and_receipts": set(out_by_id) == set(receipt_ids),
        "all_outbound_in_journal": set(out_by_id) <= journal_ids,
        "outbound_ids": sorted(out_by_id, key=int),
    }


def native_header_check(directory: Path, session_id: str, candidate: dict) -> dict:
    headers = []
    for path in directory.glob("repo/.runtime/dsh-home*/**/session.v4.jsonl"):
        if path.parent.name != session_id:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("type") == "request/header":
                headers.append(row["data"]["header"])
    if not headers:
        raise AssertionError("native request header missing")
    header = headers[-1]
    tool = next((tool for tool in header.get("tools", [])
                 if tool.get("name") == "mcp__astrbot__qq_send_origin"), {})
    config = header.get("config", {})
    description_matches = tool.get("description", "").strip() == candidate["send_tool_description"].strip()
    model_matches = (config.get("provider") == "deepseek-official"
                     and config.get("model") == "deepseek-flash")
    if not description_matches or not model_matches:
        raise AssertionError("native send-tool description or model differs from experiment")
    return {"session_id": session_id, "send_description_matches": description_matches,
            "model_matches": model_matches, "provider": config["provider"], "model": config["model"],
            "send_description_sha256": digest(tool["description"])}


def verify_file_artifact(before: dict[str, dict], after: dict[str, dict],
                         path: str, required_terms: list[str]) -> dict:
    artifact = after.get(path)
    content = artifact.get("text", "") if artifact else ""
    return {
        "path": path,
        "exists_and_nonempty": bool(artifact and artifact.get("bytes", 0) > 0),
        "changed_during_turn": bool(artifact and (
            path not in before or before[path].get("sha256") != artifact.get("sha256"))),
        "sha256": artifact.get("sha256") if artifact else None,
        "bytes": artifact.get("bytes") if artifact else None,
        "required_terms": {term: term in content for term in required_terms},
        "all_required_terms_present": all(term in content for term in required_terms),
        "content": content,
    }


def workspace_snapshot(workspace: Path, filenames: set[str] | None = None) -> dict[str, dict]:
    snapshot = {}
    if not workspace.is_dir():
        return snapshot
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            rel = path.relative_to(workspace).as_posix()
            if filenames is not None and rel not in filenames:
                continue
            snapshot[rel] = {"sha256": digest(raw), "bytes": len(raw),
                             "text": raw.decode("utf-8", errors="replace")}
    return snapshot


def file_inventory(root: Path) -> list[dict]:
    """Hash persistent native session/tool transcript files without dumping secrets."""
    inventory = []
    if not root.is_dir():
        return inventory
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            inventory.append({"path": path.relative_to(root).as_posix(),
                              "bytes": len(raw), "sha256": digest(raw)})
    return inventory


def _runtime_module():
    return load_module("chat_expression_sandbox_runtime", ROOT / "sandbox/runtime.py")


def launch_internal(directory: Path) -> int:
    details_path = directory / "sandbox-runtime.json"
    details = json.loads(details_path.read_text(encoding="utf-8"))
    ports = details["ports"]
    return _runtime_module()._run(details, Path(details["astrbot_source"]), ports, "live",
                                  owner_volume_mib=32)


class ExpressionCheck:
    """LiveOwner transport helpers plus a private patched runtime per variant."""

    def __init__(self, directory: Path, candidate: dict, variant: str):
        command_module = load_module("chat_expression_commands", ROOT / "scripts/check-agent-commands.py")
        self.check = command_module.LiveCommandCheck(directory)
        self.directory = directory
        self.candidate = candidate
        self.variant = variant
        self.rows = []
        self.reset_records = []
        self.stage_manifest = None

    def prepare_and_start(self):
        runtime = _runtime_module()
        details = runtime.prepare(self.directory, ASTRBOT,
                                  {"onebot": self.check.onebot, "ui": self.check.ui, "mcp": self.check.mcp},
                                  "live", OWNER, snapshot_mode="text")
        self.stage_manifest = stage_candidate(details, self.candidate)
        workspace = self.workspace
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        for scenario in self.scenarios if hasattr(self, "scenarios") else []:
            for filename, content in scenario.get("workspace_files", {}).items():
                path = workspace / filename
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)
        runner = ROOT / "scripts/check-chat-expression.py"
        child = [str(ASTRBOT / ".venv/bin/python"), str(runner), "--launch-internal", str(self.directory)]
        self.check.log = (self.directory / "runtime.log").open("ab")
        self.check.process = subprocess.Popen(child, cwd=ROOT, stdout=self.check.log,
                                              stderr=subprocess.STDOUT, start_new_session=True)
        self.check.wait(lambda: self.check.http("/api/state").get("connected"),
                        "isolated simulated OneBot connected", timeout=90)
        def ready():
            with __import__("urllib.request", fromlist=["urlopen"]).urlopen(
                    f"http://127.0.0.1:{self.check.mcp}/healthz", timeout=3) as response:
                return json.load(response).get("ready")
        self.check.wait(ready, "real DSH and tool runtime ready", timeout=90)

    @property
    def workspace(self) -> Path:
        # The sandbox's Owner private-chat contract uses the staged repository
        # root as its host workdir. The group sandbox has a separate workspace.
        return self.directory / "repo"

    def _reset(self, scenario: dict, repeat: int) -> dict:
        key = scenario["conversation"]
        sender = OWNER
        baseline = self.check.model_count()
        event = self.check.wake(key, "/new", sender=sender, mention=False)
        output = self.check.wait(lambda: next((row for row in self.check.events()
                                  if row["direction"] == "outbound" and row["conversation"] == event["conversation"]
                                  and int(row["message_id"]) > int(event["message_id"])), None),
                                 "native /new confirmation", timeout=30)
        self.check.wait(lambda: any(row.get("message_id") == str(output["message_id"])
                                    and row.get("direction") == "outbound" for row in self.check.rows()),
                        "/new confirmation in canonical Journal")
        if self.check.model_count() != baseline:
            raise AssertionError("/new unexpectedly invoked the model")
        state = self.check.state(key)
        if not state or not state.get("session_id"):
            raise AssertionError("/new did not publish a native session")
        floor = next(row["journal_id"] for row in self.check.rows()
                     if row.get("message_id") == str(event["message_id"])
                     and row.get("conversation_key") == key)
        if state and state.get("history_after_journal_id") != floor:
            raise AssertionError("fresh scenario history floor differs from /new command row")
        record = {"scenario": scenario["id"], "repeat": repeat, "command": event,
                  "confirmation": output,
                  "confirmation_journal": next(row for row in self.check.rows()
                      if row.get("message_id") == str(output["message_id"])),
                  "history_floor": floor, "session_id": state.get("session_id") if state else None}
        self.reset_records.append(record)
        return record

    def _turn(self, scenario: dict, turn: dict, index: int, reset: dict) -> dict:
        key = scenario["conversation"]
        expected_tools = ({"read", "write", "edit", "bash"} if turn.get("tool") == "file"
                          else {"bash"} if turn.get("tool") == "bash" else set())
        watched = set(scenario.get("workspace_files", {}))
        if turn.get("expected_output_file"):
            watched.add(turn["expected_output_file"])
        before_files = workspace_snapshot(self.workspace, watched) if watched else {}
        baseline = self.check.http("/api/events").get("cursor")
        event = self.check.wake(key, turn["user"], sender=OWNER, mention=key.startswith("group:"))
        self.check.wait(lambda: any(row.get("event") == "turn/completed" for row in self.check.trace(event)),
                        "native turn/completed", timeout=210)

        def settled():
            traces = [row for row in self.check.trace(event) if row.get("event") == "send/receipt"]
            ids = {str(row.get("message_id")) for row in traces if row.get("message_id")}
            outgoing = [row for row in self.check.events() if row["direction"] == "outbound"
                        and row["conversation"] == event["conversation"]
                        and int(row["message_id"]) > int(event["message_id"])]
            out_ids = {str(row["message_id"]) for row in outgoing}
            journal = [row for row in self.check.rows() if row.get("conversation_key") == key]
            joined = verify_receipt_join(outgoing, traces, journal, key)
            return (joined if ids and joined["same_ids_in_transport_and_receipts"]
                    and joined["all_outbound_in_journal"] else None)

        join = self.check.wait(settled, "all QQ outbound events, send receipts, and canonical Journal rows joined",
                               timeout=40)
        outgoing = [row for row in self.check.events() if row["direction"] == "outbound"
                    and row["conversation"] == event["conversation"]
                    and str(row["message_id"]) in set(join["outbound_ids"])]
        journal = [row for row in self.check.rows() if row.get("conversation_key") == key
                   and str(row.get("message_id")) in set(join["outbound_ids"])]
        receipts = [row for row in self.check.trace(event) if row.get("event") == "send/receipt"]
        if not join["unique_receipt_ids"] or not join["same_ids_in_transport_and_receipts"] \
                or not join["all_outbound_in_journal"] or not outgoing:
            raise AssertionError(f"turn message/receipt/Journal evidence incomplete: {join}")
        query = "/api/events" + ("?" + urlencode({"after": baseline}) if baseline else "")
        lifecycle = self.check.request("GET", query).get("events", [])
        lifecycle = [row for row in lifecycle if row.get("session_id") == reset["session_id"]]
        messages = [row for row in lifecycle if row.get("event") == "model/start"]
        candidate_loading = prompt_loading(self.candidate, messages)
        if not (candidate_loading["all_guide_lines_observed"]
                and candidate_loading["all_profile_reply_lines_observed"]
                and candidate_loading["tool_availability_observed"]):
            raise AssertionError("candidate guide/profile/tool availability missing from native request")
        candidate_loading["native_header"] = native_header_check(self.directory, reset["session_id"], self.candidate)
        before_ids = set(before_files)
        after_files = workspace_snapshot(self.workspace, watched) if watched else {}
        workspace_changes = {name: after_files[name] for name in after_files
                             if name not in before_ids or before_files.get(name, {}).get("sha256") != after_files[name]["sha256"]}
        tool_starts = [row for row in lifecycle if row.get("event") == "tool/start"]
        native_turns = [row for row in lifecycle if row.get("event") in {"turn/start", "turn/end", "turn/completed"}]
        case_record = {
            "variant": self.variant, "scenario": scenario["id"], "scenario_topic": scenario["topic"],
            "repeat": reset["repeat"], "turn_index": index, "intent": turn["intent"],
            "user_text": turn["user"], "human_checks": turn["human_checks"],
            "inbound": event, "outbound_messages": outgoing, "send_receipts": receipts,
            "canonical_journal_rows": journal, "message_join": join,
            "native_lifecycle_after_baseline": lifecycle,
            "native_prompt_loading": candidate_loading,
            "tool_calls_observed": tool_starts, "native_turn_markers": native_turns,
            "workspace_changes": workspace_changes,
            "workspace_snapshot_before": before_files, "workspace_snapshot_after": after_files,
            "expected_output_file": turn.get("expected_output_file"),
            "tool_call_expected": bool(expected_tools),
            "tool_call_observed": any(row.get("tool") in expected_tools for row in tool_starts),
            "chain_complete": True, "expression_evaluation": "human-review-required",
        }
        if expected_tools and not case_record["tool_call_observed"]:
            case_record["tool_call_observed"] = any(row.get("tool") in expected_tools for row in lifecycle)
        if turn.get("expected_output_file"):
            artifact = verify_file_artifact(before_files, after_files,
                                            turn["expected_output_file"],
                                            turn.get("required_file_terms", []))
            case_record["file_artifact"] = artifact
            case_record["file_artifact_verified"] = (artifact["exists_and_nonempty"]
                                                      and artifact["changed_during_turn"]
                                                      and artifact["all_required_terms_present"])
            case_record["file_artifact_sha256"] = artifact["sha256"]
        self.rows.append(case_record)
        evidence_dir = self.directory / "evidence" / scenario["id"] / f"turn-{index:02d}"
        evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (evidence_dir / "turn.json").write_text(json.dumps(case_record, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
        return case_record

    def run_scenario(self, scenario: dict, repeat: int) -> list[dict]:
        reset = self._reset(scenario, repeat)
        return [self._turn(scenario, turn, index, reset)
                for index, turn in enumerate(scenario["turns"], start=1)]

    def stop(self):
        self.check.stop()


def build_blinded_pairs(all_rows: list[dict], variants_by_repeat: dict[int, list[str]],
                        scenarios: dict) -> tuple[dict, dict]:
    by_key = {(row["variant"], row["repeat"], row["scenario"], row["turn_index"]): row
              for row in all_rows}
    rng = random.SystemRandom()
    packet, key = {"human_evaluation": "unrated", "pairs": []}, {}
    rubric = scenarios["human_rubric"]
    for repeat in sorted(variants_by_repeat):
        for scenario in scenarios["scenarios"]:
            variants = variants_by_repeat[repeat]
            labels = ["A", "B"]
            rng.shuffle(labels)
            comparison_key = {labels[i]: variants[i] for i in range(2)}
            pair_key = f"{scenario['id']}:r{repeat}"
            missing = [variant for variant in variants
                       if any((variant, repeat, scenario["id"], index) not in by_key
                              for index in range(1, len(scenario["turns"]) + 1))]
            if missing:
                packet.setdefault("incomplete_pairs", []).append({"pair_id": pair_key,
                                                                   "missing_variants": missing})
                continue
            key[pair_key] = comparison_key
            turns = []
            for index, userturn in enumerate(scenario["turns"], start=1):
                pair = {}
                for label, variant in key[f"{scenario['id']}:r{repeat}"].items():
                    row = by_key[(variant, repeat, scenario["id"], index)]
                    pair[label] = {"outbound_messages": row["outbound_messages"],
                                   "human_checks": row["human_checks"],
                                   "tool_call_observed": row["tool_call_observed"],
                                   "file_artifact_verified": row.get("file_artifact_verified"),
                                   "file_artifact_sha256": row.get("file_artifact_sha256")}
                turns.append({"turn_index": index, "user_text": userturn["user"], "pair": pair,
                              "ratings": {dimension["id"]: None for dimension in rubric["dimensions"]},
                              "preference": None, "notes": ""})
            packet["pairs"].append({"pair_id": f"{scenario['id']}:r{repeat}",
                                    "scenario": scenario["id"], "topic": scenario["topic"],
                                    "turns": turns,
                                    "discussion_level_rating": {label: None for label in ("A", "B")}})
    packet["rubric"] = rubric
    return packet, key


class Runner:
    def __init__(self, variants: list[str], repeats: int, data_dir: Path,
                 scenarios_path: Path = SCENARIOS):
        self.guides, self.scenarios = load_inputs(scenarios_path=scenarios_path)
        self.variants, self.repeats, self.data_dir = variants, repeats, data_dir
        self.all_rows = []
        self.variants_by_repeat = {}
        self.run_errors = []

    def run_one(self, variant: str, repeat: int) -> dict:
        directory = self.data_dir / variant / f"repeat-{repeat:02d}"
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.exists():
            raise ValueError(f"fresh variant runtime directory required: {directory}")
        directory.mkdir(mode=0o700)
        runner = ExpressionCheck(directory, self.guides[variant], variant)
        runner.scenarios = self.scenarios["scenarios"]
        started = time.monotonic()
        result = {"variant": variant, "repeat": repeat, "data_dir": str(directory),
                  "status": "chain-incomplete", "turns": 0}
        try:
            runner.prepare_and_start()
            if runner.check.http("/api/state")["scenario"]["owner_id"] != OWNER:
                raise AssertionError("sandbox Owner identity mismatch")
            for scenario in self.scenarios["scenarios"]:
                rows = runner.run_scenario(scenario, repeat)
                self.all_rows.extend(rows)
                result["turns"] += len(rows)
                result["scenarios"] = result.get("scenarios", []) + [scenario["id"]]
            result["status"] = "transport-and-evidence-complete-human-review-required"
            result["stage_manifest"] = runner.stage_manifest
            result["reset_confirmations"] = runner.reset_records
        except Exception as exc:
            result.update({"error_type": type(exc).__name__, "error": str(exc),
                           "traceback": traceback.format_exc()})
            self.run_errors.append(result)
        finally:
            try:
                if runner.check.process:
                    evidence = runner.check.request("GET", "/api/evidence/export")
                    runner.check.save("evidence.json", evidence)
            except Exception:
                pass
            runner.stop()
            if directory.exists():
                native_home = directory / "repo/.runtime/dsh-home-owner"
                transcript_files = file_inventory(native_home)
                (directory / "native-session-file-inventory.json").write_text(
                    json.dumps(transcript_files, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result["native_session_files"] = transcript_files
            (directory / "result.json").write_text(json.dumps(result | {
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "candidate_sha256": runner.stage_manifest,
                "human_evaluation": "required; no automatic expression pass/fail",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    def run(self) -> dict:
        started = time.monotonic()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        for repeat in range(self.repeats):
            order = list(self.variants)
            if repeat % 2:
                order.reverse()
            self.variants_by_repeat[repeat] = order
            for variant in order:
                self.run_one(variant, repeat)
                # Stop after a broken chain; do not spend additional model calls
                # when evidence or sandbox health has already failed.
                if self.run_errors:
                    break
            if self.run_errors:
                break
        human_packet, answer_key = build_blinded_pairs(self.all_rows, self.variants_by_repeat, self.scenarios)
        review_dir = self.data_dir / "human-review"
        review_dir.mkdir(mode=0o700, exist_ok=True)
        (review_dir / "paired-review.json").write_text(json.dumps(human_packet, ensure_ascii=False, indent=2) + "\n",
                                                       encoding="utf-8")
        (review_dir / "review-key.json").write_text(json.dumps(answer_key, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8")
        expected = self.repeats * len(self.variants) * sum(len(s["turns"]) for s in self.scenarios["scenarios"])
        chain_complete = not self.run_errors and len(self.all_rows) == expected
        result = {
            "chain_complete": chain_complete,
            "expression_pass": None,
            "human_evaluation": "unrated",
            "mode": "real Flash, isolated AstrBot/DSH, simulated OneBot; no real QQ connection",
            "provider": os.environ.get("V2_DSH_PROVIDER"), "model": os.environ.get("V2_DSH_MODEL"),
            "variants": self.variants, "repeats": self.repeats,
            "dialogue_turns_per_variant_repeat": sum(len(s["turns"]) for s in self.scenarios["scenarios"]),
            "captured_turns": len(self.all_rows), "expected_turns": expected,
            "run_errors": self.run_errors, "paired_review": str(review_dir / "paired-review.json"),
            "answer_key": str(review_dir / "review-key.json"),
            "data_dir": str(self.data_dir), "elapsed_seconds": round(time.monotonic() - started, 2),
            "warning": "A complete transport/evidence chain does not constitute a style or expression pass.",
        }
        (self.data_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", default="principles,examples",
                        help="comma-separated candidates: principles,examples")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--data-dir", type=Path,
                        help="fresh private root for all per-variant sandboxes and review artifacts")
    parser.add_argument("--launch-internal", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.launch_internal:
        return launch_internal(args.launch_internal)
    variants = [value.strip() for value in args.variants.split(",") if value.strip()]
    if len(variants) != 2 or set(variants) != {"principles", "examples"}:
        parser.error("--variants must select both principles and examples exactly once")
    if args.repeats < 1 or args.repeats > 5:
        parser.error("--repeats must be 1..5")
    required = ("DEEPSEEK_API_KEY", "V2_DSH_PROVIDER", "V2_DSH_MODEL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("live provider settings must be inherited in the process environment: " + ", ".join(missing))
    try:
        load_inputs(scenarios_path=args.scenarios)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid fixtures: {type(exc).__name__}: {exc}")
    root = args.data_dir or ROOT / ".runtime" / ("chat-expression-" + uuid.uuid4().hex[:12])
    root = root.expanduser().absolute()
    if root.exists() or root.is_symlink():
        parser.error(f"fresh data directory required: {root}")
    os.umask(0o077)
    try:
        result = Runner(variants, args.repeats, root, args.scenarios).run()
    except Exception as exc:
        print(json.dumps({"chain_complete": False, "error_type": type(exc).__name__,
                          "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({key: result[key] for key in (
        "chain_complete", "expression_pass", "human_evaluation", "captured_turns",
        "expected_turns", "paired_review", "data_dir")}, ensure_ascii=False, indent=2))
    return 0 if result["chain_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
