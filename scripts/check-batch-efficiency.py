#!/usr/bin/env python3
"""Replay captured memory model requests through an isolated native DSH runtime.

Full prompts and responses are stored only in a private evidence directory. This
runner never writes sandbox memory or sends QQ messages.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_json(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _payload(call: dict) -> dict:
    try:
        value = json.loads(call["messages"][0]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"call {call.get('id')} has no JSON memory prompt") from error
    if not isinstance(value, dict):
        raise ValueError(f"call {call.get('id')} prompt is not an object")
    return value


def restore_field_aliases(prompt: dict) -> dict:
    """Restore reversible key aliases for validation; never modify sent bytes."""
    aliases = prompt.get("field_aliases")
    if aliases is None:
        return prompt
    if (not isinstance(aliases, dict) or not aliases
            or any(not isinstance(short, str) or not isinstance(full, str)
                   or not short or not full for short, full in aliases.items())
            or len(set(aliases.values())) != len(aliases)):
        raise ValueError("invalid field_aliases map")

    def restore(value):
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "field_aliases":
                    continue
                expanded = aliases.get(key, key)
                if expanded in result:
                    raise ValueError("field alias restoration produced a duplicate key")
                result[expanded] = restore(item)
            return result
        return value

    return restore(prompt)


def _call_id(call: dict, path: Path) -> str:
    value = call.get("id", path.stem)
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value):
        raise ValueError(f"invalid call id in {path.name}")
    return str(value)


def load_calls(directory: Path, selected: list[str] | None) -> list[dict]:
    files = sorted(directory.rglob("*.json"))
    calls = []
    seen = set()
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            continue
        call_id = _call_id(value, path)
        if call_id in seen:
            raise ValueError(f"duplicate captured call id: {call_id}")
        seen.add(call_id)
        calls.append({"id": call_id, "path": path, "call": value})
    if selected:
        missing = set(selected) - seen
        if missing:
            raise ValueError("selected call id is absent: " + ", ".join(sorted(missing)))
        by_id = {item["id"]: item for item in calls}
        calls = [by_id[call_id] for call_id in selected]
    if not calls:
        raise ValueError("no matching captured model calls")
    return calls


def _parse_changes(text: str) -> list[dict]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response JSON key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"changes"}
            or not isinstance(value["changes"], list) or len(value["changes"]) > 100
            or any(not isinstance(change, dict) for change in value["changes"])):
        raise ValueError("invalid memory response changes")
    return value["changes"]


def validate_call_result(prompt: dict, text: str, review_module) -> dict:
    changes = _parse_changes(text)
    visible = set()
    for container in (prompt.get("messages", []), prompt.get("existing_memories", [])):
        if not isinstance(container, list):
            raise ValueError("prompt evidence container is invalid")
        for item in container:
            if not isinstance(item, dict):
                raise ValueError("prompt evidence item is invalid")
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                raise ValueError("prompt evidence list is invalid")
            for ref in evidence:
                if not isinstance(ref, dict):
                    raise ValueError("prompt evidence entry is invalid")
                evidence_id = ref.get("evidence_id")
                if type(evidence_id) is not int or evidence_id < 1 or evidence_id in visible:
                    raise ValueError("prompt evidence IDs must be unique positive integers")
                visible.add(evidence_id)
    drafts = prompt.get("draft_changes")
    if drafts is None:
        for change in changes:
            claims = change.get("claims")
            if not isinstance(claims, list):
                raise ValueError("model change claims are invalid")
            for claim in claims:
                if not isinstance(claim, dict) or not isinstance(claim.get("sources"), list):
                    raise ValueError("model claim sources are invalid")
                if any(type(source) is not int or source not in visible for source in claim["sources"]):
                    raise ValueError("model source reference is absent from visible evidence")
        return {"changes": changes, "review_validated": False,
                "source_references_present": True}
    if not isinstance(drafts, list):
        raise ValueError("review prompt lacks draft_changes")
    review_module.validate_review_decisions(drafts, changes, visible)
    return {"changes": changes, "review_validated": True,
            "source_references_present": True}


def memory_snapshot(runner) -> list[dict]:
    import sqlite3
    snapshots = []
    for path in runner.impl._memory_databases():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            workspace = db.execute("SELECT cursor,revision FROM workspace").fetchone()
            memories = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            revisions = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        snapshots.append({"database": str(path.relative_to(runner.directory)),
                          "cursor": workspace[0], "revision": workspace[1],
                          "memory_count": memories, "revision_count": revisions})
    return snapshots


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--call-id", action="append", nargs="+", required=True,
                        help="captured call ID; repeat this option or provide multiple IDs")
    parser.add_argument("--mode", choices=("serial", "parallel"), required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--provider", default="wb")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 2 or args.repeats < 1:
        parser.error("workers must be 1..2 and repeats must be positive")
    workers = 1 if args.mode == "serial" else args.workers
    output, runtime_dir = args.output.resolve(), args.runtime.resolve()
    calls_dir = args.calls_dir.resolve()
    if not calls_dir.is_dir():
        parser.error("calls-dir must be an existing directory")
    if runtime_dir.exists():
        parser.error("runtime directory must be new and empty")
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be new or empty")
    selected = [item for group in args.call_id for item in group]
    if len(set(selected)) != len(selected):
        parser.error("call IDs must be unique")
    calls = load_calls(calls_dir, selected)
    os.umask(0o077)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_dir.mkdir(mode=0o700, parents=True)
    harness = load("batch_efficiency_backlog", ROOT / "scripts/check-memory-backlog.py")
    probe = load("batch_efficiency_fact_boundary", ROOT / "scripts/check-memory-fact-boundary.py")
    review = importlib.import_module("v2_memory.fact_review")
    runner = harness.BacklogCheck(runtime_dir)
    result = {"passed": False, "mode": args.mode, "workers": workers,
              "repeats": args.repeats, "provider": args.provider, "model": args.model,
              "started_at": time.time(), "calls": []}
    try:
        runner.start(args.provider, args.model)
        capacity = runner.native_capacity(args.provider, args.model)
        private_json(output / "native-capacity.json", capacity)
        endpoint = probe.native_endpoint(runtime_dir)
        client = load("batch_efficiency_dsh_client", ROOT / "astrbot-plugins/v2_dsh_router/dsh_client.py")
        before = memory_snapshot(runner)
        private_json(output / "memory-before.json", before)
        jobs = []
        for repetition in range(args.repeats):
            for entry in calls:
                jobs.append((repetition + 1, entry))
        def execute(job):
            repetition, entry = job
            call = entry["call"]
            call_id = entry["id"]
            raw_prompt = call["messages"][0]["content"][0]["text"]
            prompt_wire = raw_prompt
            prompt = _payload(call)
            restored = restore_field_aliases(prompt)
            name = f"{call_id}-r{repetition}"
            private_json(output / "requests" / f"{name}.json", {
                "call_id": call_id, "repetition": repetition,
                "system": call["system"], "prompt_wire": prompt_wire,
                "prompt_sha256": hashlib.sha256(prompt_wire.encode()).hexdigest(),
                "field_aliases_used": "field_aliases" in prompt})
            progress_events = []
            started = time.monotonic()
            started_at = time.time()
            usage = None
            try:
                transport = object.__new__(client.AcpRuntime)
                transport._memory_control_path = endpoint["V2_MEMORY_CONTROL_SOCKET"]
                transport._memory_control_token = endpoint["V2_MEMORY_CONTROL_TOKEN"]
                response = transport.consolidate_memory(
                    provider=args.provider, model=args.model, system=call["system"],
                    prompt=raw_prompt,
                    expected_capacity={"contextWindow": capacity["context_window"],
                                       "defaultMaxTokens": capacity["default_max_tokens"]},
                    on_progress=lambda event: progress_events.append({
                        "at": time.time(), **{key: event[key] for key in
                        ("phase", "event_count", "text_bytes", "reasoning_bytes") if key in event}}))
                usage = response.get("usage")
                private_json(output / "responses" / f"{name}.json", response)
                validation = validate_call_result(restored, response["text"], review)
                parsed_evidence = {"changes": validation.pop("changes"),
                                   "review_validated": validation["review_validated"],
                                   "source_references_present": validation["source_references_present"]}
                private_json(output / "parsed" / f"{name}.json", parsed_evidence)
                usage = response.get("usage")
                return {"call_id": call_id, "repetition": repetition,
                        "started_at": started_at, "finished_at": time.time(),
                        "seconds": round(time.monotonic() - started, 3),
                        "usage": usage, "error": None,
                        "change_count": len(parsed_evidence["changes"]),
                        "review_validated": validation["review_validated"],
                        "source_references_present": validation["source_references_present"]}
            except Exception as error:
                safe = {"call_id": call_id, "repetition": repetition,
                        "started_at": started_at, "finished_at": time.time(),
                        "seconds": round(time.monotonic() - started, 3),
                        "usage": usage, "error": {"type": type(error).__name__,
                        "code": getattr(error, "error_code", None),
                        "message": str(error)[:400]}}
                private_json(output / "errors" / f"{name}.json", safe["error"])
                return safe
            finally:
                private_json(output / "progress" / f"{name}.json", progress_events)

        workload_started = time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(execute, job): job for job in jobs}
            for future in as_completed(futures):
                result["calls"].append(future.result())
                private_json(output / "progress-manifest.json", result)
        result["workload_started_at"] = workload_started
        result["workload_finished_at"] = time.time()
        result["workload_seconds"] = round(result["workload_finished_at"] - workload_started, 3)
        after = memory_snapshot(runner)
        private_json(output / "memory-after.json", after)
        result["memory_unchanged"] = before == after
        result["passed"] = (result["memory_unchanged"]
                            and all(item.get("error") is None for item in result["calls"]))
    except Exception as error:
        result["fatal_error"] = {"type": type(error).__name__,
                                 "code": getattr(error, "error_code", None)}
    finally:
        result["finished_at"] = time.time()
        result["cleanup"] = runner.stop_and_cleanup()
        if not all(result["cleanup"].get(key) for key in ("stopped", "mounts_released",
                                                           "temporary_credentials_removed")):
            result["passed"] = False
        private_json(output / "manifest.json", result)
        if result["cleanup"].get("stopped") and result["cleanup"].get("mounts_released"):
            shutil.rmtree(runtime_dir, ignore_errors=False)
        print(json.dumps({key: value for key, value in result.items()
                          if key not in {"calls", "fatal_error"}}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
