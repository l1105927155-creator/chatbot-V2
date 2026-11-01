#!/usr/bin/env python3
"""Freeze and run 42 preregistered live sandbox cells for reply-priority diagnosis."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / ".runtime/reply-priority-20261029"
FIX = HOME / "fixtures"
OLD = ROOT / ".runtime/reply-habits-20261029/fixtures"
CAUSAL = ROOT / ".runtime/tool-result-causality-20261029/runs/causality/service-Tshort/r2"
CHECKPOINT1 = OLD / "checkpoints/diary-after-send.json"
SOURCE_SESSION = OLD / "source-session.v4.jsonl"
MATERIALIZER = ROOT / "scripts/materialize-reply-checkpoints.mjs"
MODEL = ("wb", "deepseek-v4.1-flash")
OLD_SYSTEM = "独立接话、转告或必要的等候说明可用 qq_send_origin；最终回复的 @ 和引用用 qq_prepare_reply 设置，然后正常输出最终正文。已通过工具发送的内容无需重复"
NEW_SYSTEM = "通过 qq_send_origin 发出的各条消息和最终正文共同构成本轮回复；多条表达时工具发送前面各条，最终正文承接尚未发送的最后一条，以已发内容为前文继续；用 qq_prepare_reply 设置最后一条 @ 引用"
OLD_SEND = "用于自然交流中的独立接话、转告其他成员、必要的等候说明。"
NEW_SEND = "日常交流、工具执行结果和失败说明需要分条表达时，均可用本工具发送前面的消息；这些消息与最终正文共同构成本轮回复，普通聊天的最后一条由Agent正文发送。定时轮次仍遵循其独立发布规则。"
STOP_HINT = "权限拒绝且没有新的身份、配置或输入证据时，结束本轮重复尝试，并说明已知结果和实际可行的下一步。查询失败表示状态未知；不得据此或因文件不存在断言任务未登记。超时不能说明操作肯定未执行。"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_msg_text(message: dict) -> str:
    return "\n".join(str(x.get("text", "")) for x in message.get("content", []) if x.get("type") == "text")


def latest_source_user(messages: list[dict]) -> tuple[int, dict, str]:
    found = [(i, m) for i, m in enumerate(messages)
             if m.get("role") == "user" and m.get("source", {}).get("kind") == "user"]
    if not found:
        raise RuntimeError("checkpoint lacks a source-kind user request")
    i, message = found[-1]
    text = parse_msg_text(message).strip()
    # Native QQ contexts encode the journal window as NDJSON; select only its
    # declared latest user row instead of mistaking an earlier wake for this request.
    lines = text.splitlines()
    try:
        header = json.loads(lines[0])
        latest_id = int(header["latest_user_journal_id"])
        for line in lines[1:]:
            row = json.loads(line)
            if int(row.get("journal_id", -1)) == latest_id:
                value = row.get("content", {}).get("text", "").strip()
                if value:
                    text = value
                break
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    return i, message, text


def paired_sends(messages: list[dict], source_index: int) -> list[str]:
    calls: dict[str, tuple[str, dict]] = {}
    sent: list[str] = []
    for message in messages[source_index + 1:]:
        if message.get("role") == "assistant":
            for block in message.get("content", []):
                if block.get("type") == "tool-call":
                    calls[block["id"]] = (block.get("name", ""), block)
        elif message.get("role") == "tool":
            call = calls.get(message.get("toolCallId"))
            if call and call[0].endswith("qq_send_origin") and not message.get("isError"):
                try:
                    arg = json.loads(call[1].get("arguments", "{}"))
                    sent.append(str(arg.get("text", "")))
                except json.JSONDecodeError:
                    raise RuntimeError("malformed qq_send_origin arguments in checkpoint")
    return sent


def tool_facts(messages: list[dict], source_index: int) -> list[str]:
    names: dict[str, str] = {}
    facts = []
    for message in messages[source_index + 1:]:
        if message.get("role") == "assistant":
            for block in message.get("content", []):
                if block.get("type") == "tool-call":
                    names[block["id"]] = block.get("name", "")
        elif message.get("role") == "tool" and message.get("isError"):
            name = names.get(message.get("toolCallId"), "tool")
            value = parse_msg_text(message)
            if "task_" in name or "Permission" in value or "timeout" in value.lower():
                facts.append(f"{name}: {value}")
    return facts


def ensure_checkpoint_pairing(messages: list[dict]) -> None:
    pending = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            if pending:
                raise RuntimeError("checkpoint contains an unfinished prior call batch")
            for b in msg.get("content", []):
                if b.get("type") == "tool-call":
                    pending.add(b["id"])
        elif msg.get("role") == "tool":
            call_id = msg.get("toolCallId")
            if call_id not in pending:
                raise RuntimeError(f"checkpoint has orphan tool result {call_id}")
            pending.remove(call_id)
        elif pending:
            raise RuntimeError("checkpoint splits an unresolved tool batch")
    if pending:
        raise RuntimeError(f"checkpoint has pending calls: {sorted(pending)}")


def export_sqlite_journal(path: Path, upper: int) -> list[dict]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(row) for row in db.execute("select * from journal where journal_id<=? order by journal_id", (upper,))]
    db.close()
    return rows


def copy_causality_workspace(destination: Path, manifest: dict) -> tuple[Path, str]:
    result = json.loads((CAUSAL / "offline-volume-artifact.json").read_text(encoding="utf-8"))
    image = Path(result["path"])
    if digest_file(image) != result["sha256"] or image.stat().st_size != result["bytes"]:
        raise RuntimeError("causality offline volume artifact does not match its recorded digest")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    subprocess.run(["debugfs", "-R", f"rdump / {destination}", str(image)],
                   capture_output=True, text=True, check=True)
    source = Path(json.loads((CAUSAL / "sandbox-runtime-copy.json").read_text())["staged_repo"])
    source_group_root = source / ".runtime/group-storage"
    dirs = list(source_group_root.glob("*/workspace"))
    if len(dirs) != 1:
        raise RuntimeError(f"expected one causal run workspace source path, got {len(dirs)}")
    return destination, str(dirs[0])


def make_checkpoint(out: Path, source: Path, *, seq: int | None = None) -> dict:
    if seq is not None:
        subprocess.run(["node", str(MATERIALIZER), str(source), str(seq), str(out)], check=True)
    value = json.loads(out.read_text(encoding="utf-8"))
    ensure_checkpoint_pairing(value["messages"])
    source_index, source_message, request = latest_source_user(value["messages"])
    return {"path": out, "value": value, "sha256": digest_file(out), "source_index": source_index,
            "source_user_request": request,
            "already_sent": paired_sends(value["messages"], source_index),
            "facts": tool_facts(value["messages"], source_index)}


def freeze() -> dict:
    manifest_path = FIX / "manifest.json"
    if manifest_path.exists():
        raise RuntimeError(f"refusing to overwrite frozen diagnostics manifest: {manifest_path}")
    old_manifest = json.loads((OLD / "manifest.json").read_text(encoding="utf-8"))
    prod = Path(old_manifest["production_router"]).resolve(strict=True)
    guide = Path(old_manifest["guides"]["A"]).resolve(strict=True)
    persona_source = Path(old_manifest["workspace"]) / "persona.md"
    if not persona_source.is_file():
        raise RuntimeError("frozen baseline Persona is missing")
    if not CHECKPOINT1.is_file() or not SOURCE_SESSION.is_file():
        raise RuntimeError("required original checkpoints/session are missing")
    if not CAUSAL.joinpath("experiment-plugin.jsonl").is_file():
        raise RuntimeError("causality source projected request is missing")
    if HOME.exists() and any(HOME.iterdir()):
        raise RuntimeError(f"diagnostics evidence root is non-empty; inspect before use: {HOME}")

    build = HOME / ".building"
    if build.exists():
        raise RuntimeError(f"stale freeze staging exists; inspect manually: {build}")
    (build / "checkpoints").mkdir(mode=0o700, parents=True)
    (build / "workspaces").mkdir(mode=0o700)
    (build / "journals").mkdir(mode=0o700)
    (build / "guides").mkdir(mode=0o700)
    # Preserve exact fixture code and artifacts used to define the experiment.
    code_paths = [Path(__file__).resolve(), ROOT / "scripts/run-reply-habits-experiment.py", ROOT / "sandbox/reply-habits-experiment.mjs",
                  ROOT / "sandbox/reply-priority-experiment.mjs", ROOT / "scripts/materialize-reply-checkpoints.mjs",
                  ROOT / "tests/native/reply-priority-experiment.mjs",
                  ROOT / "docs/evaluations/reply-priority-diagnostics-2026-10-29.md"]
    frozen_code = {}
    for source in code_paths:
        target = build / "protocol" / source.relative_to(ROOT)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(0o600)
        frozen_code[target.relative_to(build).as_posix()] = digest_file(target)
    guide_copy = build / "guides/A.md"
    shutil.copy2(guide, guide_copy)

    # Checkpoint 1: original production transcript after first send, without final.
    cp1 = build / "checkpoints/original-11-15-after-first-send.json"
    shutil.copy2(CHECKPOINT1, cp1)
    d1 = json.loads(cp1.read_text(encoding="utf-8"))
    ensure_checkpoint_pairing(d1["messages"])
    i1, _, req1 = latest_source_user(d1["messages"])
    sent1 = paired_sends(d1["messages"], i1)
    facts1 = tool_facts(d1["messages"], i1)
    if not sent1:
        raise RuntimeError("checkpoint 1 must contain the already-sent first message")
    journal_old = json.loads(Path(old_manifest["journal_path"]).read_text(encoding="utf-8"))
    j1 = [r for r in journal_old if 33907 <= int(r["journal_id"]) <= 34197]
    ids1 = {int(x["journal_id"]) for x in j1}
    if 34197 not in ids1 or 34198 in ids1:
        raise RuntimeError("checkpoint 1 Journal boundary must include 34197 and exclude 34198")
    j1path = build / "journals/original-through-34197.json"
    save(j1path, j1)

    # Checkpoint 2: last actual model request from the earlier causal live run.
    causal_rows = [json.loads(line) for line in CAUSAL.joinpath("experiment-plugin.jsonl").read_text().splitlines() if line.strip()]
    if not causal_rows:
        raise RuntimeError("causality run has no projected requests")
    last_request = causal_rows[-1]
    if (last_request.get("provider"), last_request.get("model")) != MODEL:
        raise RuntimeError("causality checkpoint used unexpected provider/model")
    messages2 = last_request.get("messages")
    if not isinstance(messages2, list):
        raise RuntimeError("last causality projected request lacks full messages")
    ensure_checkpoint_pairing(messages2)
    i2, _, req2 = latest_source_user(messages2)
    sent2 = paired_sends(messages2, i2)
    facts2 = tool_facts(messages2, i2)
    if len(sent2) != 4:
        raise RuntimeError(f"checkpoint 2 expected exactly four prior qq_send_origin messages, got {len(sent2)}")
    ws2, source_ws2 = copy_causality_workspace(build / "workspaces/causality-service", old_manifest)
    cp2 = build / "checkpoints/causality-service-after-four-sends.json"
    save(cp2, {"messages": messages2,
               "sourceWorkspace": source_ws2,
               "sourceRun": str(CAUSAL), "sourceRequestIndex": i2,
               "projection": "last actually projected live request before final; source run output and Journal receipt retained"})
    # The actual private sandbox filesystem is extracted read-only from its archived ext4 image.
    j2 = export_sqlite_journal(CAUSAL / "astrbot/data/plugin_data/v2_journal/journal.sqlite3", upper=5)
    if len(j2) != 5 or j2[-1]["direction"] != "outbound":
        raise RuntimeError("causality Journal must include the fourth prior outbound and exclude final")
    prior_result = json.loads((CAUSAL / "result.json").read_text())
    if str(j2[-1]["message_id"]) != str(prior_result["outbound"][3]["message_id"]):
        raise RuntimeError("source Journal boundary does not match fourth actual receipt")
    if any(str(row['message_id']) == str(prior_result['outbound'][4]['message_id']) for row in j2):
        raise RuntimeError("source final leaked into the terminal checkpoint Journal")
    j2path = build / "journals/causality-through-fourth-receipt.json"
    save(j2path, j2)

    # Failure checkpoint: original session prefix exactly through sequence 176.
    cp3 = build / "checkpoints/first-task-manage-refusal-seq176.json"
    # Materialize directly from the original frozen source session (not from the copy list above).
    subprocess.run(["node", str(MATERIALIZER), str(SOURCE_SESSION), "176", str(cp3)], check=True)
    d3value = json.loads(cp3.read_text(encoding="utf-8"))
    ensure_checkpoint_pairing(d3value["messages"])
    i3, _, req3 = latest_source_user(d3value["messages"])
    sent3 = paired_sends(d3value["messages"], i3)
    facts3 = tool_facts(d3value["messages"], i3)
    task_manage_errors = [m for m in d3value["messages"] if m.get("role") == "tool" and m.get("isError")
                          and "task_manage" in " ".join(str(x.get("text", "")) for x in m.get("content", []))]
    # Pair lookup is authoritative: confirm the denied task_manage result itself exists.
    call_names = {b.get("id"): b.get("name") for m in d3value["messages"] if m.get("role") == "assistant"
                  for b in m.get("content", []) if b.get("type") == "tool-call"}
    denied = [m for m in d3value["messages"] if m.get("role") == "tool" and m.get("isError")
              and call_names.get(m.get("toolCallId")) == "mcp__astrbot__task_manage"]
    if not denied:
        raise RuntimeError("seq176 checkpoint does not contain the required explicit task_manage refusal")
    j3 = [r for r in journal_old if 33907 <= int(r["journal_id"]) <= 34192]
    if not any(int(x["journal_id"]) == 34192 for x in j3):
        raise RuntimeError("failure checkpoint source Journal boundary 34192 missing")
    j3path = build / "journals/original-through-34192.json"
    save(j3path, j3)

    checkpoints = [
        {"id": "terminal-original", "path": cp1, "value": d1, "workspace": Path(old_manifest["workspace"]),
         "source_workspace": old_manifest["source_workspace"], "journal": j1path, "journal_upper": 34197,
         "journal_boundary": 34197, "source_request": req1, "already_sent": sent1, "facts": facts1,
         "known_outcome": "permission-refused-no-change"},
        {"id": "terminal-causality", "path": cp2, "value": {"messages": messages2}, "workspace": ws2,
         "source_workspace": source_ws2, "journal": j2path, "journal_upper": 5,
         "journal_boundary": 5, "source_request": req2, "already_sent": sent2, "facts": facts2,
         "known_outcome": "service-status-unknown"},
        {"id": "failure-seq176", "path": cp3, "value": d3value, "workspace": Path(old_manifest["workspace"]),
         "source_workspace": old_manifest["source_workspace"], "journal": j3path, "journal_upper": 34192,
         "journal_boundary": 34192, "source_request": req3, "already_sent": sent3, "facts": facts3,
         "known_outcome": "permission-refused-no-change"},
    ]
    terminal_variants = [
        ("D0G0-limited", {"limited": True}),
        ("G1", {"system_aligned": True, "limited": True}),
        ("D1", {"tool_aligned": True, "limited": True}),
        ("G1D1", {"system_aligned": True, "tool_aligned": True, "limited": True}),
        ("D0G0-full", {}),
    ]
    failure_variants = [
        ("E0S0", {}), ("E1S0", {"error_envelope": True}),
        ("E0S1", {"stop_hint": True}), ("E1S1", {"error_envelope": True, "stop_hint": True}),
    ]
    cases = []
    for checkpoint in checkpoints[:2]:
        for variant, flags in terminal_variants:
            text = checkpoint['source_request']
            cases.append({"id": f"{checkpoint['id']}--{variant}", "study": "terminal",
                          "scenario": checkpoint["id"], "variant": variant,
                          "system_aligned": flags.get("system_aligned", False),
                          "tool_aligned": flags.get("tool_aligned", False), "limited": flags.get("limited", False),
                          "error_envelope": False, "stop_hint": False,
                          "checkpoint_path": str(checkpoint["path"]), "checkpoint_sha256": digest_file(checkpoint["path"]),
                          "workspace": str(checkpoint["workspace"]), "source_workspace": checkpoint["source_workspace"],
                          "already_sent": checkpoint["already_sent"], "facts": checkpoint["facts"],
                          "source_user_request": checkpoint["source_request"], "user_request": text,
                          "known_outcome": checkpoint["known_outcome"], "journal_path": str(checkpoint["journal"]),
                          "journal_upper": checkpoint["journal_upper"], "journal_boundary": checkpoint["journal_boundary"],
                          "fault_fixture": "permission" if checkpoint["id"] == "terminal-original" else "service"})
    for variant, flags in failure_variants:
        cases.append({"id": f"failure-seq176--{variant}", "study": "failure",
                      "scenario": "failure-seq176", "variant": variant,
                      "system_aligned": False, "tool_aligned": False, "limited": False,
                      "error_envelope": flags.get("error_envelope", False), "stop_hint": flags.get("stop_hint", False),
                      "checkpoint_path": str(cp3), "checkpoint_sha256": digest_file(cp3),
                      "workspace": str(Path(old_manifest["workspace"])), "source_workspace": old_manifest["source_workspace"],
                      "already_sent": sent3, "facts": facts3, "source_user_request": req3,
                      "user_request": req3,
                      "known_outcome": "permission-refused-no-change", "journal_path": str(j3path),
                      "journal_upper": 34192, "journal_boundary": 34192, "fault_fixture": "permission"})
    if len(cases) != 14 or len({x["id"] for x in cases}) != 14:
        raise RuntimeError("expected exactly 14 unique case/condition slots")
    order_seed = secrets.randbits(64)
    slots = [{"case": case["id"], "repeat": f"r{n}"} for case in cases for n in range(3)]
    random.Random(order_seed).shuffle(slots)
    if len(slots) != 42 or len({(x["case"], x["repeat"]) for x in slots}) != 42:
        raise RuntimeError("frozen order must contain exactly 42 unique cells")

    # Before finalizing, verify the only requested text anchors exist in pinned inputs.
    active_system = OLD / "deployment/dsh/profile/agent-group-sandbox.md"
    if active_system.read_text().count(OLD_SYSTEM) != 1:
        raise RuntimeError("system allowlist anchor missing")
    if (prod / "mcp_server.py").read_text().count(OLD_SEND) != 1:
        raise RuntimeError("tool description anchor missing")
    hashes = {}
    for p in sorted(build.rglob("*")):
        if p.is_file():
            hashes[p.relative_to(build).as_posix()] = digest_file(p)
    data = {
        "version": 1, "study": "reply-priority-diagnostics", "provider": MODEL[0], "model": MODEL[1],
        "production_router": old_manifest["production_router"],
        "shared_profile_frozen": old_manifest["shared_profile_frozen"],
        "workspace": old_manifest["workspace"], "guide_A": str(guide_copy),
        "persona_sha256": digest_file(Path(old_manifest["workspace"]) / "persona.md"),
        "guide_sha256": digest_file(guide_copy), "source_deployment": old_manifest["source_deployment"],
        "code_hashes": frozen_code, "files_sha256": hashes, "cases": cases,
        "repetitions": 3, "total_cells": 42, "order_seed": order_seed, "order": slots,
        "sources": {"prior_manifest": str(OLD / "manifest.json"),
                    "prior_manifest_sha256": digest_file(OLD / "manifest.json"),
                    "causality_run": str(CAUSAL),
                    "causality_request_sha256": digest_file(CAUSAL / "experiment-plugin.jsonl"),
                    "causality_volume_sha256": json.loads((CAUSAL / "offline-volume-artifact.json").read_text())["sha256"],
                    "checkpoint1_journal_upper": 34197, "checkpoint1_excluded_final": 34198,
                    "checkpoint2_journal_upper": 5, "checkpoint2_excluded_final": 6,
                    "failure_checkpoint_seq": 176},
        "provenance_limits": ["native transcript checkpoints are seed prefixes, not resumed native executions",
                              "only a fresh QQ-style wake is sent to complete each checkpoint",
                              "terminal causality workspace is a read-only ext4 rdump from its offline artifact"],
    }
    def final_paths(value):
        if isinstance(value, str):
            return value.replace(str(build), str(FIX))
        if isinstance(value, list):
            return [final_paths(v) for v in value]
        if isinstance(value, dict):
            return {k: final_paths(v) for k, v in value.items()}
        return value
    save(build / "manifest.json", final_paths(data))
    # Re-open and check that the just-written manifest still covers every frozen source.
    reloaded = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    if len(reloaded["order"]) != 42 or any(digest_file(build / rel) != value for rel, value in frozen_code.items()):
        raise RuntimeError("final manifest/code hash verification failed")
    if HOME.exists():
        for name in ("fixtures", "runs", "matrix-logs"):
            child = HOME / name
            if child.exists():
                raise RuntimeError(f"unexpected existing final path blocks atomic freeze: {child}")
    (build / "manifest.json").replace(build / "manifest.final.json")
    build.rename(FIX)
    (HOME / "runs").mkdir(mode=0o700)
    (HOME / "matrix-logs").mkdir(mode=0o700)
    final = json.loads((FIX / "manifest.final.json").read_text(encoding="utf-8"))
    # Final canonical filename is only created after all checks pass.
    (FIX / "manifest.final.json").replace(FIX / "manifest.json")
    return {"manifest": str(FIX / "manifest.json"), "cases": len(cases), "cells": len(slots),
            "order_seed": order_seed, "checkpoint_sources": [str(CHECKPOINT1), str(CAUSAL), str(SOURCE_SESSION)]}


def verify_manifest() -> dict:
    data = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    amendment_path = HOME / 'protocol-amendment.json'
    active_code = json.loads(amendment_path.read_text()).get('active_code_sha256', {}) if amendment_path.exists() else {}
    checks = {
        "14_cases": len(data["cases"]) == 14,
        "42_unique_slots": len(data["order"]) == 42 and len({(x["case"], x["repeat"]) for x in data["order"]}) == 42,
        "WB_model_frozen": (data["provider"], data["model"]) == MODEL,
        "case_ids_unique": len({x["id"] for x in data["cases"]}) == 14,
        "all_checkpoint_hashes": all(digest_file(Path(x["checkpoint_path"])) == x["checkpoint_sha256"] for x in data["cases"]),
        "all_workspaces_present": all(Path(x["workspace"]).is_dir() for x in data["cases"]),
        "persona_preserved": all(digest_file(Path(x["workspace"]) / "persona.md") == data["persona_sha256"]
                                  for x in data["cases"]),
        "guide_A_preserved": digest_file(Path(data["guide_A"])) == data["guide_sha256"],
        "four_terminal_variants_limited": all(c['limited'] == (c['variant'] != 'D0G0-full')
            for c in data['cases'] if c['study'] == 'terminal'),
        "frozen_files_match": all(digest_file(FIX / rel) == value for rel,value in data['files_sha256'].items()),
        "executing_code_matches": all(digest_file(ROOT / rel) == active_code.get(rel, digest_file(FIX / 'protocol' / rel))
            for rel in ['scripts/reply-priority-diagnostics.py','scripts/run-reply-habits-experiment.py',
                        'sandbox/reply-priority-experiment.mjs']),
    }
    for case in data["cases"]:
        messages = json.loads(Path(case["checkpoint_path"]).read_text(encoding="utf-8"))["messages"]
        ensure_checkpoint_pairing(messages)
        latest_source_user(messages)
        rows = json.loads(Path(case["journal_path"]).read_text(encoding="utf-8"))
        if not any(int(row["journal_id"]) == int(case["journal_boundary"]) for row in rows):
            checks[f"{case['id']}_journal_boundary"] = False
        if any(int(row["journal_id"]) > int(case["journal_upper"]) for row in rows):
            checks[f"{case['id']}_journal_future_absent"] = False
    if not all(checks.values()):
        raise RuntimeError("manifest verification failed: " + repr(checks))
    return checks


def configure_runner(data: dict, case: dict):
    base = load_module("reply_priority_base_runner", ROOT / "scripts/run-reply-habits-experiment.py")
    base.EXPERIMENT = HOME
    base.FIXTURES = FIX
    original_stage = base.private_stage

    def stage(details, runner_data, arm, runtime_case, log_file):
        mapped = dict(runtime_case)
        if mapped.get("scenario") == "terminal-causality":
            mapped["id"] = "service"
        else:
            mapped["id"] = "diary-after-send"
        staged = original_stage(details, runner_data, arm, mapped, log_file)
        staged_file = Path(details["staged_repo"]) / "sandbox/reply-habits-experiment.mjs"
        staged_file.write_bytes((ROOT / "sandbox/reply-priority-experiment.mjs").read_bytes())
        staged_file.chmod(0o600)
        staged["priority_experiment_plugin_sha256"] = digest_file(staged_file)
        staged["fault_fixture"] = runtime_case["fault_fixture"]
        return staged

    base.private_stage = stage

    def import_source_journal(self):
        rows = json.loads(Path(self.case["journal_path"]).read_text(encoding="utf-8"))
        rows = [row for row in rows if int(row["journal_id"]) <= int(self.case["journal_upper"])]
        if int(self.case["journal_boundary"]) not in {int(row["journal_id"]) for row in rows}:
            raise RuntimeError("required source journal checkpoint boundary missing")
        db = sqlite3.connect(self.journal_path(), timeout=20)
        if db.execute('select count(*) from journal').fetchone()[0]:
            db.close()
            raise RuntimeError('refusing history import into non-empty canonical Journal')
        for row in rows:
            db.execute('''insert into journal(journal_id,platform_id,bot_id,conversation_key,conversation_type,
                direction,actor_id,actor_name,source,message_id,reply_to,content_json,created_at,observed_at_ns)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (row['journal_id'],base.PLATFORM,base.BOT,'group:'+base.GROUP,'group',row['direction'],
                 base.BOT if str(row['actor_id']) == str(row['bot_id']) else row['actor_id'],row['actor_name'],
                 'reply_priority_source_fixture',row['message_id'],row['reply_to'],
                 base._old.imported_content(row['content_json'],str(row['bot_id'])),
                 row['created_at'],row['observed_at_ns']))
        db.commit()
        db.close()
        state = {'rows':len(rows),'journal_ids_preserved':True}
        state.update({"source_journal_ids": [int(r["journal_id"]) for r in rows],
                      "source_journal_upper": self.case["journal_upper"],
                      "required_boundary": self.case["journal_boundary"],
                      "future_final_excluded": True})
        self.journal_import = state
        self.save("source-journal-import.json", state)
        return rows

    base.Cell.import_source_journal = import_source_journal
    data = copy.deepcopy(data)
    data["guides"] = {"A": data["guide_A"]}
    data["workspace"] = case["workspace"]
    data["journal_path"] = case["journal_path"]
    data["source_group"] = "697823265" if case["scenario"] != "terminal-causality" else "91000"
    runtime_case = copy.deepcopy(case)
    runtime_case["id"] = "diary-after-send"  # activates canonical source Journal import in the reused lifecycle
    runtime_case["stage"] = case["study"]
    runtime_case["text"] = case["user_request"]
    runtime_case["checkpoint_path"] = case["checkpoint_path"]
    # The plugin consumes per-case flags from the checkpoint wrapper, not the legacy mode value.
    checkpoint = json.loads(Path(case["checkpoint_path"]).read_text(encoding="utf-8"))
    checkpoint["diagnostics"] = {key: case.get(key, False) for key in (
        "system_aligned", "tool_aligned", "limited", "error_envelope", "stop_hint")}
    checkpoint["diagnostics"].update({"id": case["id"], "known_outcome": case["known_outcome"],
                                      "persona_sha256": data["persona_sha256"], "guide_sha256": data["guide_sha256"]})
    enriched = HOME / "runs" / "seeds" / f"{case['id']}.json"
    if enriched.exists():
        if json.loads(enriched.read_text(encoding="utf-8")) != checkpoint:
            raise RuntimeError("refusing to replace an existing enriched checkpoint")
    else:
        save(enriched, checkpoint)
    runtime_case["checkpoint_path"] = str(enriched)
    data["provider"], data["model"] = MODEL
    return base, data, runtime_case


def run_cell(case_id: str, repeat: str, prepare_only: bool = False) -> dict:
    verify_manifest()
    data = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    case = next((x for x in data["cases"] if x["id"] == case_id), None)
    if not case:
        raise ValueError(f"unknown diagnostic case {case_id}")
    if repeat not in {"r0", "r1", "r2"}:
        raise ValueError("repeat must be r0, r1, or r2")
    directory = HOME / "runs" / case["study"] / case["scenario"] / case["variant"] / repeat
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError(f"refusing to overwrite an existing cell: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    base, run_data, runtime_case = configure_runner(data, case)
    # Distinct case IDs map to A solely because all cases preserve the same A guide;
    # the experiment plugin reads the flags from each case's enriched checkpoint.
    cell = base.Cell(directory, run_data, runtime_case, "A", case["study"], repeat)
    if prepare_only:
        details = base.runtime.prepare(directory, ROOT / ".runtime/astrbot",
                                       {"onebot": cell.onebot, "ui": cell.ui, "mcp": cell.mcp},
                                       "live", base.OWNER, snapshot_mode="text")
        staged = base.private_stage(details, run_data, "A", runtime_case, directory / "experiment-plugin.jsonl")
        cell.save("prepare-validation.json", {"prepared": True, "live_model_started": False,
                                               "runtime": details, "stage": staged,
                                               "workspace_hashes": {
                                                   "persona": digest_file(Path(case["workspace"]) / "persona.md"),
                                                   "guide": digest_file(Path(case["workspace"]) / "AGENTS.md")}})
        return {"prepared": True, "live_model_started": False, "directory": str(directory)}
    original_request, original_wait = cell.request, cell.wait
    def tracked_request(method, path, *args, **kwargs):
        value = original_request(method, path, *args, **kwargs)
        if method == 'POST' and path == '/api/send':
            cell.priority_input_event = value['event']
        return value
    def guarded_wait(condition, description, timeout=60):
        if description != 'turn completed':
            return original_wait(condition, description, timeout)
        def poll():
            errors = [row for row in cell.evidence()['lifecycle']
                      if row.get('event') == 'turn/end' and row.get('status') == 'error']
            if errors:
                native_error = []
                for session in (directory/'repo/.runtime').glob('dsh-home-group-*/sessions/*/*/session.v4.jsonl'):
                    for line in session.read_text().splitlines():
                        event = json.loads(line)
                        if event.get('type') == 'turn/end' and event.get('data',{}).get('reason',{}).get('kind') == 'error':
                            native_error.append(event['data']['reason'])
                cell.save('native-turn-failure.json', native_error)
                raise RuntimeError('native turn failed: '+json.dumps(native_error,ensure_ascii=False))
            return condition()
        return original_wait(poll, description, timeout=600)
    cell.request, cell.wait = tracked_request, guarded_wait
    result = cell.run()
    result.update({'condition':case,'case':case_id,'stage':case['study']})
    cell.save("result.json", result)
    # Completion requires the plugin observer to have seen the exact request and cleanup proof.
    projection = []
    path = directory / "experiment-plugin.jsonl"
    if path.is_file():
        projection = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not projection or any((x.get("provider"), x.get("model")) != MODEL for x in projection):
        raise RuntimeError("actual projected request did not confirm the frozen WB model")
    if not (directory / "cleanup-result.json").is_file():
        raise RuntimeError("sandbox cleanup proof is missing")
    cleanup = json.loads((directory / "cleanup-result.json").read_text(encoding="utf-8"))
    if not cleanup.get("success") or not cleanup.get("supervisor_exited") or cleanup.get("workspace_mounted_after_cleanup"):
        raise RuntimeError("sandbox cleanup verification failed")
    for request in projection:
        system = parse_msg_text(request['messages'][0])
        if Path(data['guide_A']).read_text() not in system or Path(case['workspace'],'persona.md').read_text().strip() not in system:
            raise RuntimeError('full frozen guide or Persona missing from actual request')
        if not all(request['checks'].get(k) is True for k in ['system_changed_exactly_once','tools_schema_unchanged','tool_visibility_only','paired']):
            raise RuntimeError('actual projection did not pass its whitelist checks')
    validation = {"success":True,"expected_provider": MODEL[0], "expected_model": MODEL[1],
                  "requests_seen": len(projection), "all_requests_match_model": True,
                  "cleanup_success": True, "receipt_journal_joins": result.get("receipt_journal_joins", [])}
    cell.save("request-projection-validation.json", validation)
    return {"completed": True, "case": case_id, "repeat": repeat,
            "outbounds": len(result["outbound"]), "requests": len(projection), "artifact": str(directory)}


def completed(directory):
    if not (directory/'result.json').exists():
        return False
    for name in ('request-projection-validation.json','cleanup-result.json'):
        file = directory/name
        if not file.exists() or json.loads(file.read_text()).get('success') is not True:
            raise RuntimeError('existing output requires validation/cleanup audit, never replay: '+str(directory))
    if any((directory/name).exists() for name in ('failure.json','validation-failure.json','cleanup-failure.json')):
        raise RuntimeError('existing result has unresolved failure: '+str(directory))
    return True


def matrix(workers=4, limit=None):
    if not 1 <= workers <= 4:
        raise ValueError('workers must be 1..4')
    verify_manifest()
    data = json.loads((FIX/'manifest.json').read_text())
    cases = {c['id']:c for c in data['cases']}
    cells = []
    for item in data['order']:
        c=cases[item['case']]
        directory=HOME/'runs'/c['study']/c['scenario']/c['variant']/item['repeat']
        if completed(directory):
            continue
        if directory.exists():
            raise RuntimeError('incomplete attempt requires audit, never replay: '+str(directory))
        cells.append(item)
    if limit is not None:
        cells=cells[:limit]
    active_children=set(); lock=threading.Lock(); stopping=False
    def emit(row):
        row['time']=time.time()
        with (HOME/'matrix-logs/progress.jsonl').open('a') as f:
            f.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
        with lock:
            children=list(active_children)
        for child in children:
            if child.poll() is None: child.send_signal(signal.SIGINT)
        for child in children:
            try: child.wait(timeout=50)
            except subprocess.TimeoutExpired: child.kill();child.wait()
        raise SystemExit(128+signum)
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    def execute(item):
        output=HOME/'matrix-logs'/f"{item['case']}--{item['repeat']}.log"
        with output.open('w') as log:
            child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--case',item['case'],'--repeat',item['repeat']],
                                   cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            with lock: active_children.add(child)
            try: return child.wait()
            finally:
                with lock: active_children.discard(child)
    failed=False; pending=iter(cells)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active={}
        def submit():
            if stopping:return
            item=next(pending,None)
            if item:
                emit({'status':'starting',**item});active[pool.submit(execute,item)]=item
        for _ in range(workers):submit()
        while active:
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                item=active.pop(future);code=future.result();c=cases[item['case']]
                directory=HOME/'runs'/c['study']/c['scenario']/c['variant']/item['repeat']
                valid=code==0 and completed(directory)
                failed |= not valid
                emit({'status':'finished' if valid else 'failed',**item,'returncode':code})
            if not failed:
                for _ in done:submit()
    emit({'status':'stopped-for-audit' if failed else 'matrix-completed','scheduled':len(cells)})
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--repeat", choices=("r0", "r1", "r2"), default="r0")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument('--matrix',action='store_true')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--limit',type=int)
    args = parser.parse_args()
    if args.freeze:
        print(json.dumps(freeze(), ensure_ascii=False))
        return 0
    if args.verify:
        print(json.dumps(verify_manifest(), ensure_ascii=False, indent=2))
        return 0
    if args.matrix:
        return matrix(args.workers,args.limit)
    if not args.case:
        parser.error("select --freeze, --verify, or --case")
    print(json.dumps(run_cell(args.case, args.repeat, args.prepare_only), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
