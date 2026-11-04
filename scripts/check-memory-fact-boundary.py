#!/usr/bin/env python3
"""Compare memory fact boundaries through the pinned, isolated native model.

Private artifacts contain the exact requests/responses for semantic review.
The script checks transport and references, not whether natural language is true.
It never commits generated memories or sends messages to production QQ.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))
parse_model_json = json.loads


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_json(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def fixture_rows(case):
    invitation = "麻烦大家看看演示视频后凭自己的判断投投票吧"
    rows = [dict(journal_id=1, actor_id="10001", actor_name="A", direction="inbound",
                 created_at=1793380831,
                 content={"text": invitation, "segments": [{"type": "text", "text": invitation}]})]
    if case in {"unread_forward", "old_summary"}:
        rows.append(dict(journal_id=2, actor_id="10001", actor_name="A", direction="inbound",
                         created_at=1793380832, content={"text": "", "segments": [{
                             "type": "forward", "id": "opaque-forward-reference",
                             "summary": "演示视频已经发送", "content": [{"type": "video"}]}]}))
    elif case == "actual_video":
        rows[0]["content"]["segments"].append({"type": "video", "file": "opaque-video"})
    elif case == "self_report":
        text = "我已经发了演示视频，大家看完再凭自己的判断投票。"
        rows[0]["content"] = {"text": text, "segments": [{"type": "text", "text": text}]}
    elif case == "posted_link":
        text = "投票入口：https://example.invalid/vote/12，大家可以自行判断。"
        rows[0]["content"] = {"text": text, "segments": [{"type": "text", "text": text}]}
    elif case == "subjective":
        rows = [dict(journal_id=i, actor_id=actor, actor_name=name, direction=direction,
                     created_at=1793380830+i,
                     content={"text": text, "segments": [{"type": "text", "text": text}]})
                for i, actor, name, direction, text in [
                    (1, "10001", "A", "inbound", "这个作品挺好，但我不喜欢被人催着投票。"),
                    (2, "10002", "B", "inbound", "我倒觉得大家一起凑热闹挺有意思。"),
                    (3, "99000", "Bot", "outbound", "我也烦催票，越催越想唱反调；不过看大家起哄又有点好笑。")]]
    elif case != "invitation_only":
        raise ValueError("unknown fixture case")
    return rows


def fixture_prompt(case, evidence):
    rows = fixture_rows(case)
    prompt = {"messages": [evidence.project_message(row)[0] for row in rows],
              "existing_memories": []}
    if case == "old_summary":
        prior = dict(id="11111111-1111-4111-8111-111111111111",
                     content="A已经发送了演示视频，请大家投票。", members=["10001"], weight=3,
                     status="active", source_ids=[1], evidence_status="verified",
                     claims=[{"text": "A邀请大家观看演示视频并投票。", "sources": [{
                         "journal_id": 1, "quote": rows[0]["content"]["text"],
                         "actor_id": "10001", "direction": "inbound"}]}])
        prompt["existing_memories"] = [evidence.project_memory(prior)[0]]
    evidence.number_prompt_evidence(prompt)
    return prompt


def real_prompt(call, journal_path, evidence, *, refresh_observations):
    content = call["messages"][0]["content"]
    text = content if isinstance(content, str) else next(
        part["text"] for part in content if part.get("type") == "text")
    prompt = json.loads(text)
    if refresh_observations:
        with closing(sqlite3.connect(journal_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for row in prompt["messages"]:
                value = db.execute("SELECT content_json FROM journal WHERE journal_id=?",
                                   (row["journal_id"],)).fetchone()
                if value is None:
                    raise ValueError("frozen Journal row is missing")
                row["host_observations"] = evidence.journal_evidence(
                    row["journal_id"], json.loads(value[0]))[1]
    return prompt


def native_endpoint(data):
    artifact_root = str((data / "astrbot/data").resolve())
    keys = {b"SANDBOX_ARTIFACT_ROOT", b"V2_MEMORY_CONTROL_SOCKET", b"V2_MEMORY_CONTROL_TOKEN"}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            entries = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        values = {}
        for entry in entries:
            key, separator, value = entry.partition(b"=")
            if separator and key in keys:
                values[key.decode()] = value.decode()
        if values.get("SANDBOX_ARTIFACT_ROOT") == artifact_root and values.get("V2_MEMORY_CONTROL_SOCKET"):
            return values
    raise RuntimeError("isolated native endpoint is unavailable")


def assert_refs(response, prompt):
    value = parse_model_json(response["text"])
    changes = value.get("changes")
    if not isinstance(changes, list) or len(changes) > 100:
        raise ValueError("invalid changes")
    visible = {ref["evidence_id"] for item in [*prompt["messages"], *prompt["existing_memories"]]
               for ref in item.get("evidence", [])}
    for change in changes:
        claims = change.get("claims")
        if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
            raise ValueError("invalid claims")
        for claim in claims:
            refs = claim.get("sources")
            if not isinstance(refs, list) or not refs or any(type(ref) is not int or ref not in visible for ref in refs):
                raise ValueError("invalid source reference")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-call", type=Path, required=True)
    parser.add_argument("--frozen-journal", type=Path, required=True)
    parser.add_argument("--baseline-guide", type=Path, required=True)
    parser.add_argument("--candidate-guide", type=Path, required=True)
    parser.add_argument("--baseline-evidence", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--cases", nargs="+", choices=["real_100", "invitation_only", "unread_forward",
                        "actual_video", "self_report", "posted_link", "subjective", "old_summary"])
    parser.add_argument("--provider", default="wb")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    args = parser.parse_args(argv)
    os.umask(0o077)
    out, data = args.output_dir.resolve(), args.data_dir.resolve()
    if data.exists():
        raise ValueError("runtime directory already exists")
    if (out / "result.json").exists() or (out / "responses").exists():
        raise ValueError("refusing to overwrite an earlier evaluation")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    data.mkdir(mode=0o700)
    harness = load("fact_boundary_backlog", ROOT / "scripts/check-memory-backlog.py")
    baseline_evidence = load("fact_boundary_old_evidence", args.baseline_evidence)
    candidate_evidence = load("fact_boundary_new_evidence", ROOT / "astrbot-plugins/v2_memory/evidence.py")
    sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
    client = load("fact_boundary_client", ROOT / "astrbot-plugins/v2_dsh_router/dsh_client.py")
    call = json.loads(args.source_call.read_text())
    baseline = args.baseline_guide.read_text()
    candidate = args.candidate_guide.read_text()
    if not call["system"].startswith(baseline):
        raise ValueError("original call does not match the archived baseline guide")
    suffix = call["system"][len(baseline):]
    tasks = []
    cases = args.cases or ["real_100", "invitation_only", "unread_forward", "actual_video",
                          "self_report", "posted_link", "subjective", "old_summary"]
    for case in cases:
        for repetition in range(args.repeats if case == "real_100" else 1):
            for variant, guide, projection in (("baseline", baseline, baseline_evidence),
                                                ("candidate", candidate, candidate_evidence)):
                prompt = (real_prompt(call, args.frozen_journal, projection,
                                      refresh_observations=variant == "candidate")
                          if case == "real_100" else fixture_prompt(case, projection))
                tasks.append((f"{case}-{variant}-{repetition+1}", case, variant, guide+suffix, prompt))
    private_json(out / "plan.json", {"cases": cases, "requests": len(tasks),
                 "provider": args.provider, "model": args.model,
                 "guide_sha256": {"baseline": hashlib.sha256(baseline.encode()).hexdigest(),
                                  "candidate": hashlib.sha256(candidate.encode()).hexdigest()},
                 "original_call_id": call["id"], "semantic_review": "required, not inferred from reference validation"})
    runner = harness.BacklogCheck(data)
    result = {"passed": False, "semantic_review": "pending", "started_at": time.time(), "completed": []}
    try:
        runner.start(args.provider, args.model)
        capacity = runner.native_capacity(args.provider, args.model)
        endpoint = native_endpoint(data)
        def memory_state():
            snapshots = []
            for path in runner.impl._memory_databases():
                with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
                    snapshots.append({"database": str(path.relative_to(data)),
                        "workspace": db.execute("SELECT cursor,revision FROM workspace").fetchone(),
                        "memories": db.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                        "revisions": db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]})
            return snapshots
        initial_memory = memory_state()
        private_json(out / "memory-before.json", initial_memory)
        def execute(task):
            name, case, variant, system, prompt = task
            payload = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
            private_json(out / "requests" / (name + ".json"),
                         {"system": system, "prompt": prompt, "case": case, "variant": variant})
            runtime = object.__new__(client.AcpRuntime)
            runtime._memory_control_path = endpoint["V2_MEMORY_CONTROL_SOCKET"]
            runtime._memory_control_token = endpoint["V2_MEMORY_CONTROL_TOKEN"]
            started = time.time()
            response = runtime.consolidate_memory(provider=args.provider, model=args.model,
                system=system, prompt=payload,
                expected_capacity={"contextWindow": capacity["context_window"],
                                   "defaultMaxTokens": capacity["default_max_tokens"]},
                on_progress=lambda event: private_json(out / "live" / (name+".json"),
                    {"at": time.time(), **{key: event[key] for key in
                     ("phase", "event_count", "text_bytes", "reasoning_bytes") if key in event}}))
            private_json(out / "responses" / (name + ".json"), response)
            parsed = assert_refs(response, prompt)
            return {"name": name, "case": case, "variant": variant, "changes": len(parsed["changes"]),
                    "claims": sum(len(c["claims"]) for c in parsed["changes"]),
                    "seconds": round(time.time()-started, 2), "references_valid": True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(execute, task): task[0] for task in tasks}
            for future in as_completed(futures):
                try:
                    value = future.result()
                except Exception as error:
                    value = {"name": futures[future], "error_type": type(error).__name__,
                             "error_code": getattr(error, "error_code", None)}
                result["completed"].append(value)
                private_json(out / "progress.json", result)
                print(json.dumps(value), flush=True)
        final_memory = memory_state()
        private_json(out / "memory-after.json", final_memory)
        if final_memory != initial_memory:
            raise RuntimeError("probe changed persistent sandbox memory")
        result["passed"] = all("error_type" not in item for item in result["completed"])
        result["memory_commits"] = 0
    except Exception as error:
        result.update(error_type=type(error).__name__)
        raise
    finally:
        result["finished_at"] = time.time()
        result["cleanup"] = runner.stop_and_cleanup()
        if not all(result["cleanup"].get(k) for k in ("stopped", "mounts_released", "temporary_credentials_removed")):
            result["passed"] = False
        private_json(out / "result.json", result)
        if result["cleanup"]["stopped"] and result["cleanup"]["mounts_released"]:
            shutil.rmtree(data)
        print(json.dumps({k: v for k, v in result.items() if k != "completed"}), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
