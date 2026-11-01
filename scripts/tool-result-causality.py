#!/usr/bin/env python3
"""Controlled sandbox experiment: tool-result protocol vs ordinary context, short vs long."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import secrets
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / ".runtime/tool-result-causality-20261029"
FIXTURES = HOME / "fixtures"
MANIFEST_PATH = FIXTURES / "manifest.v2.json"
OLD_HOME = ROOT / ".runtime/reply-habits-20261029"
OLD_RUNNER_PATH = ROOT / "scripts/run-reply-habits-experiment.py"
SCENARIOS = {
    "success": {
        "request": "告诉我这场活动几点开始、几点出发比较稳妥。",
        "fact": "活动开始时间：20:00\n路上需要：30分钟\n",
        "tool": "bash", "arguments": {"command": "cat experiment/status.txt"}, "isError": False,
    },
    "permission": {
        "request": "把每天晚上十点写日记登记成自动任务；如果无法完成，请说明实际情况和今晚的办法。",
        "fact": "Error: 当前请求者无权修改此任务；未改变任务状态。",
        "tool": "mcp__astrbot__task_manage",
        "arguments": {"action": "create", "definition": {
            "name": "晚间日记", "kind": "agent", "enabled": True,
            "schedule": {"type": "cron", "expression": "0 22 * * *", "timezone": "Asia/Shanghai"},
            "prompt": "结合本群经历写下今天的日记并保存到 diary/日期.md，保存后读回核对。",
            "parameters": {},
        }}, "isError": True,
    },
    "service": {
        "request": "确认每天晚上十点写日记的任务是否登记；如果无法确认，请说明实际情况和今晚的办法。",
        "fact": "Error: 本群任务查询失败，请核对任务服务状态。",
        "tool": "mcp__astrbot__task_list", "arguments": {}, "isError": True,
    },
}
CONDITIONS = ("Ushort", "Tshort", "Ulong", "Tlong")
CSV_NOISE = "日期,类别,序号,数值,说明\n" + "2026-10-29,历史记录,000000,17,普通归档信息\n" * 566


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def condition_parts(scenario_condition: str) -> tuple[str, str]:
    scenario, condition = scenario_condition.split("-", 1)
    if scenario not in SCENARIOS or condition not in CONDITIONS:
        raise ValueError(f"invalid case id: {scenario_condition}")
    return scenario, condition


def checkpoint_messages(scenario: str, condition: str) -> list[dict]:
    spec = SCENARIOS[scenario]
    is_tool = condition.startswith("T")
    is_long = condition.endswith("long")
    fact = spec["fact"]
    payload = (CSV_NOISE + fact) if is_long else fact
    context_id = f"causality-{scenario}-{condition}"
    request = {"role": "user", "id": f"request-{scenario}",
               "source": {"kind": "user"},
               "content": [{"type": "text", "text": spec["request"]}]}
    if not is_tool:
        return [request, {"role": "user", "id": context_id,
                 "source": {"kind": "context-data"},
                 "content": [{"type": "text", "text": payload}]}]
    call_id = f"call-{scenario}-{condition}"
    result = {"role": "tool", "id": f"result-{scenario}-{condition}",
              "source": {"kind": "tool", "callId": call_id},
              "content": [{"type": "text", "text": payload}],
              "toolCallId": call_id, "isError": spec["isError"]}
    assistant = {"role": "assistant", "id": f"assistant-{scenario}-{condition}",
                 "source": {"kind": "model", "provider": "wb", "model": "deepseek-v4.1-flash"},
                 "content": [{"type": "tool-call", "id": call_id, "name": spec["tool"],
                              "arguments": json.dumps(spec["arguments"], ensure_ascii=False, separators=(",", ":"))}]}
    return [request, assistant, result]


def prepare() -> dict:
    manifest_path = MANIFEST_PATH
    if manifest_path.exists():
        raise RuntimeError(f"refusing to overwrite frozen experiment manifest: {manifest_path}")
    old_manifest_path = OLD_HOME / "fixtures/manifest.json"
    old = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    FIXTURES.mkdir(mode=0o700, parents=True, exist_ok=True)
    (HOME / "runs").mkdir(mode=0o700, exist_ok=True)
    (HOME / "matrix-logs").mkdir(mode=0o700, exist_ok=True)
    protocol_copy = FIXTURES / "protocol"
    protocol_copy.mkdir(mode=0o700, exist_ok=True)
    paths = [OLD_RUNNER_PATH, ROOT / "sandbox/reply-habits-experiment.mjs",
             ROOT / "scripts/materialize-reply-checkpoints.mjs",
             ROOT / "scripts/tool-result-causality.py",
             ROOT / "tests/test_tool_result_causality.py",
             ROOT / "docs/evaluations/reply-habits-sandbox-2026-10-29.md"]
    protocol_hashes = {}
    for source in paths:
        target = protocol_copy / source.name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        protocol_hashes[target.relative_to(FIXTURES).as_posix()] = digest_file(target)
    prereg = ROOT / "docs/evaluations/tool-result-causality-2026-10-29.md"
    prereg_copy = FIXTURES / "protocol.md"
    prereg_copy.write_bytes(prereg.read_bytes())
    prereg_copy.chmod(0o600)
    protocol_hashes["protocol.md"] = digest_file(prereg_copy)
    amendment_text = (
        "Preflight amendment: the initial frozen draft omitted the shared source user request from each synthetic checkpoint. "
        "No model ran from that draft. Version 2 adds exactly one source-kind user request before the matched context/tool block, "
        "validates that it appears exactly once in the projected request, and uses the causality runner's own results directory. "
        "The superseded draft is preserved as manifest.json; this version is manifest.v2.json.\n"
    )
    amendment_copy = FIXTURES / "protocol-v2-amendment.md"
    amendment_copy.write_text(amendment_text, encoding="utf-8")
    amendment_copy.chmod(0o600)
    protocol_hashes["protocol-v2-amendment.md"] = digest_file(amendment_copy)
    cases, payload_records = [], {}
    checkpoint_dir = FIXTURES / "checkpoints"
    checkpoint_dir.mkdir(mode=0o700, exist_ok=True)
    for scenario in SCENARIOS:
        for condition in CONDITIONS:
            case_id = f"{scenario}-{condition}"
            checkpoint = checkpoint_dir / f"{case_id}.json"
            messages = checkpoint_messages(scenario, condition)
            dump(checkpoint, {"messages": messages,
                              "projection": "synthetic tool-result/context checkpoint; not a resumed native turn",
                              "sourceWorkspace": old["workspace"]})
            scenario_spec = SCENARIOS[scenario]
            cases.append({"id": case_id, "text": scenario_spec["request"],
                          "user_request": scenario_spec["request"], "scenario": scenario,
                          "presentation": "T" if condition.startswith("T") else "U",
                          "load": "long" if condition.endswith("long") else "short",
                          "payload_sha256": digest_bytes((CSV_NOISE + scenario_spec["fact"] if condition.endswith("long") else scenario_spec["fact"]).encode()),
                          "fact_text": scenario_spec["fact"], "stage": "causality",
                          "checkpoint_path": str(checkpoint), "checkpoint_sha256": digest_file(checkpoint)})
            payload_records[case_id] = {
                "scenario": scenario, "condition": condition,
                "fact_payload_sha256": digest_bytes(scenario_spec["fact"].encode()),
                "projected_payload_sha256": digest_bytes((CSV_NOISE + scenario_spec["fact"] if condition.endswith("long") else scenario_spec["fact"]).encode()),
                "projected_payload_bytes": len((CSV_NOISE + scenario_spec["fact"] if condition.endswith("long") else scenario_spec["fact"]).encode()),
                "is_tool_protocol": condition.startswith("T"), "is_error": scenario_spec["isError"],
                "tool_name": scenario_spec["tool"] if condition.startswith("T") else None,
            }
    guide_a = Path(old["guides"]["A"]).resolve(strict=True)
    guide_copy = FIXTURES / "guide-A.md"
    guide_copy.write_bytes(guide_a.read_bytes())
    guide_copy.chmod(0o600)
    order_seed = secrets.randbits(64)
    rng = random.Random(order_seed)
    frozen_order = []
    ids = [case["id"] for case in cases]
    for repeat_index in range(3):
        round_order = list(ids)
        rng.shuffle(round_order)
        frozen_order.extend({"case": case_id, "repeat": f"r{repeat_index}"} for case_id in round_order)
    # Only one guide entry is exposed to the reused runner; all cells are arm A.
    data = {**old, "guides": {"A": str(guide_copy)}, "cases": cases,
            "workspace": old["workspace"], "causality": {
                "frozen_manifest": str(old_manifest_path),
                "frozen_manifest_sha256": digest_file(old_manifest_path),
                "protocol_hashes": protocol_hashes,
                "guide_A_sha256": digest_file(guide_copy),
                "persona_sha256": digest_file(Path(old["workspace"]) / "persona.md"),
                "provider": "wb", "model": "deepseek-v4.1-flash",
                "conditions": list(CONDITIONS), "repetitions": 3,
                "total_cells": 36, "payloads": payload_records,
                "order_seed": order_seed, "order": frozen_order,
                "csv_noise_utf8_bytes": len(CSV_NOISE.encode()),
                "csv_noise_sha256": digest_bytes(CSV_NOISE.encode()),
                "synthetic_checkpoint_only": True,
                "historical_journal_import": False,
                "prompt_reminder": False,
                "preflight_amendment": {
                    "supersedes": str(FIXTURES / "manifest.json"),
                    "reason": "synthetic checkpoints initially omitted the shared source user request; no model was run from that draft",
                },
            }}
    dump(manifest_path, data)
    return data["causality"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check() -> dict:
    data = load_json(MANIFEST_PATH)
    failures, checks = [], {}
    c = data["causality"]
    checks["all_12_condition_slots_unique"] = len(data["cases"]) == 12 and len({x["id"] for x in data["cases"]}) == 12
    checks["three_repeats_make_36_cells"] = c["repetitions"] == 3 and c["total_cells"] == len(data["cases"]) * c["repetitions"] == 36
    expected_order_slots = {(case["id"], f"r{rep}") for case in data["cases"] for rep in range(3)}
    checks["frozen_order_covers_each_cell_once"] = len(c.get("order", [])) == 36 and {(x["case"], x["repeat"]) for x in c.get("order", [])} == expected_order_slots
    checks["preregistered_protocol_hash"] = digest_file(FIXTURES / "protocol.md") == c["protocol_hashes"]["protocol.md"]
    checks["copied_code_hashes"] = all(digest_file(FIXTURES / rel) == value for rel, value in c["protocol_hashes"].items())
    source_pairs = {
        "protocol/run-reply-habits-experiment.py": OLD_RUNNER_PATH,
        "protocol/reply-habits-experiment.mjs": ROOT / "sandbox/reply-habits-experiment.mjs",
        "protocol/materialize-reply-checkpoints.mjs": ROOT / "scripts/materialize-reply-checkpoints.mjs",
        "protocol/tool-result-causality.py": ROOT / "scripts/tool-result-causality.py",
        "protocol/test_tool_result_causality.py": ROOT / "tests/test_tool_result_causality.py",
    }
    checks["execution_sources_match_frozen_copies"] = all(
        digest_file(source) == digest_file(FIXTURES / frozen) for frozen, source in source_pairs.items())
    checks["arm_A_only"] = set(data["guides"]) == {"A"}
    checks["no_historical_journal"] = c.get("historical_journal_import") is False
    checks["same_frozen_guide"] = digest_file(Path(data["guides"]["A"])) == c["guide_A_sha256"]
    checks["unchanged_persona"] = digest_file(Path(data["workspace"]) / "persona.md") == c["persona_sha256"]
    checks["model_frozen"] = data["provider"] == "wb" and data["model"] == "deepseek-v4.1-flash"
    for scenario in SCENARIOS:
        recs = [c["payloads"][f"{scenario}-{cond}"] for cond in CONDITIONS]
        checks[f"{scenario}_short_payload_identical"] = recs[0]["projected_payload_sha256"] == recs[1]["projected_payload_sha256"]
        checks[f"{scenario}_long_payload_identical"] = recs[2]["projected_payload_sha256"] == recs[3]["projected_payload_sha256"]
        checks[f"{scenario}_same_fact_short_long"] = all(r["fact_payload_sha256"] == recs[0]["fact_payload_sha256"] for r in recs)
        for cond in CONDITIONS:
            cid = f"{scenario}-{cond}"
            case = next(x for x in data["cases"] if x["id"] == cid)
            checkpoint_path = Path(case["checkpoint_path"])
            checks[f"{cid}_checkpoint_file_hash"] = digest_file(checkpoint_path) == case["checkpoint_sha256"]
            messages = load_json(checkpoint_path)["messages"]
            expected_payload = (CSV_NOISE + SCENARIOS[scenario]["fact"] if cond.endswith("long")
                                else SCENARIOS[scenario]["fact"])
            payload_message = messages[2] if cond.startswith("T") else messages[1]
            actual_payload = payload_message["content"][0]["text"]
            checks[f"{cid}_actual_payload_hash"] = digest_bytes(actual_payload.encode()) == case["payload_sha256"]
            checks[f"{cid}_actual_fact_bytes"] = actual_payload == expected_payload
            request_messages = [m for m in messages if m.get("role") == "user"
                                and m.get("source", {}).get("kind") == "user"]
            checks[f"{cid}_exactly_one_source_request"] = (len(request_messages) == 1
                and request_messages[0]["content"][0]["text"] == SCENARIOS[scenario]["request"])
            if cond.startswith("T"):
                call_index, result_index = 1, 2
                paired = (len(messages) == 3 and messages[call_index]["role"] == "assistant"
                          and messages[result_index]["role"] == "tool"
                          and messages[call_index]["content"][0]["id"] == messages[result_index]["toolCallId"]
                          and messages[result_index]["isError"] == SCENARIOS[scenario]["isError"])
                tool_call = messages[call_index]["content"][0]
                checks[f"{cid}_actual_tool_name"] = tool_call["name"] == SCENARIOS[scenario]["tool"]
                checks[f"{cid}_actual_tool_arguments"] = json.loads(tool_call["arguments"]) == SCENARIOS[scenario]["arguments"]
            else:
                paired = (len(messages) == 2 and messages[1]["role"] == "user"
                          and messages[1]["source"]["kind"] == "context-data")
            checks[f"{cid}_protocol_shape"] = paired
            checks[f"{cid}_request_identical"] = case["text"] == SCENARIOS[scenario]["request"]
    checks["long_payload_larger"] = all(c["payloads"][f"{s}-Ulong"]["projected_payload_bytes"] > c["payloads"][f"{s}-Ushort"]["projected_payload_bytes"] for s in SCENARIOS)
    failures = [name for name, passed in checks.items() if not passed]
    result = {"pass": not failures, "checks": checks, "failures": failures,
              "provider": data["provider"], "model": data["model"], "model_started": False}
    dump(HOME / "check.json", result)
    if failures:
        raise RuntimeError("experiment fixture check failed: " + ", ".join(failures))
    return result


def load_old_runner():
    spec = importlib.util.spec_from_file_location("tool_result_causality_reused_runner", OLD_RUNNER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot import frozen runner: {OLD_RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.EXPERIMENT = HOME
    module.FIXTURES = FIXTURES
    original_stage = module.private_stage

    def scoped_stage(details, data, arm, case, log_file):
        scenario, _ = condition_parts(case["id"])
        mapped_id = {"permission": "permission", "service": "service", "success": "success"}[scenario]
        mapped = {**case, "id": mapped_id}
        return original_stage(details, data, arm, mapped, log_file)

    module.private_stage = scoped_stage
    return module


def validate_request_logs(directory: Path) -> dict:
    records = [json.loads(line) for line in (directory / "experiment-plugin.jsonl").read_text().splitlines() if line.strip()]
    if not records:
        raise RuntimeError("no projected request was observed")
    hashes = {x["sourceSystemHash"] for x in records}
    projected_hashes = {x["projectedSystemHash"] for x in records}
    models = {(x["provider"], x["model"]) for x in records}
    if len(hashes) != 1 or hashes != projected_hashes or models != {("wb", "deepseek-v4.1-flash")}:
        raise RuntimeError("projected requests differed in system prompt or model route")
    data = load_json(MANIFEST_PATH)
    case = next(c for c in data["cases"] if c["id"] == directory.parent.name)
    seed = load_json(Path(case["checkpoint_path"]))
    expected = seed["messages"]
    observed = records[0]["messages"]
    # Check exact synthetic context block bytes in the first eligible provider request.
    for message in expected:
        if message not in observed:
            raise RuntimeError("synthetic context or tool result changed before model request")
    request_text = case["user_request"]
    source_request_count = sum(1 for message in observed if message.get("role") == "user"
                               and message.get("source", {}).get("kind") == "user"
                               and any(block.get("text") == request_text for block in message.get("content", [])))
    if source_request_count != 1:
        raise RuntimeError(f"projected request contains {source_request_count} copies of the shared user request")
    return {"request_count": len(records), "system_hash": next(iter(hashes)),
            "model": "wb/deepseek-v4.1-flash", "synthetic_context_preserved": True,
            "shared_source_request_count": source_request_count,
            "requests": [{"call": r["call"], "message_hash": r["messageHash"]} for r in records]}


def run_cell(case_id: str, repeat: str) -> dict:
    data = load_json(MANIFEST_PATH)
    case = next((x for x in data["cases"] if x["id"] == case_id), None)
    if case is None:
        raise ValueError(f"unknown case {case_id}")
    run_dir = HOME / "runs" / "causality" / case_id / repeat
    if run_dir.exists():
        raise RuntimeError(f"cell already has a reserved directory; refusing replay: {run_dir}")
    run_dir.mkdir(parents=True, mode=0o700)
    runner = load_old_runner()
    # Reuse exact isolated sandbox cell lifecycle; only make turn failure visible while waiting.
    BaseCell = runner.Cell

    class CausalCell(BaseCell):
        def run(self):
            result = super().run()
            try:
                result["request_projection_validation"] = validate_request_logs(self.directory)
                self.save("result.json", result)
                self.save("validation-result.json", {"success": True,
                          "checks": result["request_projection_validation"]})
            except Exception as exc:
                if (self.directory / "result.json").exists():
                    (self.directory / "result.json").replace(self.directory / "invalid-result.json")
                self.save("failure.json", {"error": type(exc).__name__, "message": str(exc),
                                            "post_cleanup_validation_failed": True})
                self.save("validation-result.json", {"success": False, "error": str(exc)})
                raise
            return result

        def wait(self, condition, description, timeout=60):
            if description != "turn completed":
                return super().wait(condition, description, timeout)
            event = getattr(self, "_causal_input_event", None)
            if event is None:
                return super().wait(condition, description, timeout)
            def completed_or_failed():
                trace = self.trace(event)
                lifecycle = self.evidence().get("lifecycle", [])
                start = getattr(self, "_causal_lifecycle_start", 0)
                end_errors = [r for r in lifecycle[start:] if r.get("event") == "turn/end"
                              and r.get("status") == "error"]
                trace_errors = [r for r in trace if r.get("event") in {"turn/failed", "turn/error", "turn/aborted"}]
                failed = end_errors or trace_errors
                if failed:
                    detail = json.dumps(failed[-1], ensure_ascii=False, default=str)
                    raise RuntimeError(f"native turn failed: {detail}")
                return any(r.get("event") == "turn/completed" for r in trace)
            return super().wait(completed_or_failed, description, timeout)

    # Capture the actual event without changing submit path or issuing duplicate requests.
    old_run = CausalCell.run
    def instrumented_run(self):
        original_request = self.request
        def request(method, path, body=None):
            if method == "POST" and path == "/api/send" and not hasattr(self, "_causal_lifecycle_start"):
                self._causal_lifecycle_start = len(self.evidence().get("lifecycle", []))
            value = original_request(method, path, body)
            if method == "POST" and path == "/api/send":
                self._causal_input_event = value["event"]
            return value
        self.request = request
        return old_run(self)
    CausalCell.run = instrumented_run
    cell = CausalCell(run_dir, data, case, "A", "causality", repeat)
    result = cell.run()
    print(json.dumps({"status": "cell-complete", "case": case_id, "repeat": repeat,
                      "outbound_count": len(result["outbound"]), "artifact": str(run_dir)}, ensure_ascii=False), flush=True)
    return result


def matrix(workers: int) -> int:
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    data = load_json(MANIFEST_PATH)
    protocol = data["causality"]
    seed = protocol["order_seed"]
    cells = [(item["case"], item["repeat"]) for item in protocol["order"]]
    log_path = HOME / "matrix-logs/matrix.jsonl"
    lock = threading.Lock()
    children: set[subprocess.Popen] = set()
    stop_event = threading.Event()
    def emit(item):
        item["time"] = time.time()
        with lock:
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(json.dumps(item, ensure_ascii=False), flush=True)
    def handle_signal(signum, frame):
        stop_event.set()
        with lock:
            active = list(children)
        for proc in active:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 40
        for proc in active:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=max(.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
        raise SystemExit(128 + signum)
    old_int, old_term = signal.signal(signal.SIGINT, handle_signal), signal.signal(signal.SIGTERM, handle_signal)
    def execute(case, rep):
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "cell", "--case", case, "--repeat", rep],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        with lock: children.add(proc)
        try:
            output, _ = proc.communicate()
            cell_log = HOME / "matrix-logs" / f"{case}-{rep}.log"
            cell_log.write_text(output or "", encoding="utf-8")
            return proc.returncode, output
        finally:
            with lock: children.discard(proc)
    failed = False
    iterator = iter(cells)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = {}
        def submit():
            if stop_event.is_set(): return
            item = next(iterator, None)
            if item:
                emit({"status": "starting", "case": item[0], "repeat": item[1]})
                active[pool.submit(execute, *item)] = item
        for _ in range(workers): submit()
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                case, rep = active.pop(future)
                code, output = future.result()
                run_dir = HOME / "runs" / "causality" / case / rep
                validation_path = run_dir / "validation-result.json"
                cleanup_path = run_dir / "cleanup-result.json"
                valid = (code == 0 and validation_path.is_file() and cleanup_path.is_file()
                         and load_json(validation_path).get("success") is True
                         and load_json(cleanup_path).get("success") is True
                         and (run_dir / "result.json").is_file())
                emit({"status": "finished" if valid else "failed", "case": case,
                      "repeat": rep, "returncode": code,
                      "validation_success": load_json(validation_path).get("success") if validation_path.is_file() else False,
                      "cleanup_success": load_json(cleanup_path).get("success") if cleanup_path.is_file() else False})
                if not valid:
                    failed = True
            if not failed:
                for _ in done: submit()
    emit({"status": "stopped-for-failure-audit" if failed else "matrix-completed",
          "scheduled_cells": len(cells), "worker_limit": workers, "order_seed": seed})
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("check")
    cellp = sub.add_parser("cell"); cellp.add_argument("--case", required=True); cellp.add_argument("--repeat", required=True)
    mp = sub.add_parser("matrix"); mp.add_argument("--workers", type=int, default=4)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(), ensure_ascii=False, indent=2)); return 0
    if args.command == "check":
        print(json.dumps(check(), ensure_ascii=False, indent=2)); return 0
    if args.command == "cell":
        run_cell(args.case, args.repeat); return 0
    if args.command == "matrix":
        return matrix(args.workers)
    if args.command == "status":
        manifest = load_json(MANIFEST_PATH)
        cells = list((HOME / "runs/causality").glob("*/*"))
        valid = [d for d in cells if (d / "result.json").is_file()
                 and (d / "validation-result.json").is_file() and (d / "cleanup-result.json").is_file()
                 and load_json(d / "validation-result.json").get("success") is True
                 and load_json(d / "cleanup-result.json").get("success") is True]
        invalid = [d for d in cells if (d / "result.json").exists() and d not in valid]
        failures = [d for d in cells if (d / "failure.json").exists() or (d / "invalid-result.json").exists()]
        print(json.dumps({"completed_valid": len(valid), "invalid": len(invalid), "failed": len(failures), "planned": 36}, indent=2)); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
