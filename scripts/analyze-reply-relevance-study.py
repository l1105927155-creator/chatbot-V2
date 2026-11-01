#!/usr/bin/env python3
"""Export factual study evidence and condition-blind review packets, not quality scores."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def native_tools(directory, paths):
    tools = []
    for path in paths:
        for line in (directory / path).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("type") == "tool/call":
                data = event.get("data", {})
                tools.append({"name": data.get("name"), "call_id": data.get("callId"), "seq": event.get("seq"),
                              "is_error": None, "exit_code": None, "error_text": None})
            if event.get("type") == "tool/result":
                message = event.get("data", {}).get("message", {})
                call = next((t for t in reversed(tools) if t["call_id"] == message.get("toolCallId")), None)
                if call is not None:
                    call["is_error"] = message.get("isError")
                    body = "\n".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text")
                    codes = re.findall(r"^\[exit code: (-?\d+)\]$", body, re.MULTILINE)
                    call["exit_code"] = int(codes[-1]) if codes else None
                    # Native Bash may return isError=false with a nonzero process exit.
                    if message.get("isError") or call["exit_code"] not in (None, 0):
                        call["error_text"] = body
    return tools


def initial_journal_projection(artifact, cell):
    """Compare actual first native Journal input, excluding generated transport IDs."""
    candidates = []
    for message in artifact.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "text":
                continue
            lines = block.get("text", "").splitlines()
            try:
                header = json.loads(lines[0])
            except (IndexError, ValueError):
                continue
            if not isinstance(header, dict) or "latest_user_journal_id" not in header:
                continue
            rows = [json.loads(line) for line in lines[1:] if line.strip()]
            projected = [{"journal_id": r.get("journal_id"), "direction": r.get("direction"),
                          "actor_id": r.get("actor_id"), "text": r.get("content", {}).get("text"),
                          "reply_to": r.get("reply_to")} for r in rows]
            expected = [{"journal_id": r["journal_id"], "direction": r["direction"], "actor_id": r["actor_id"],
                         "text": json.loads(r["content_json"])["text"], "reply_to": r["reply_to"]} for r in cell["journal"]]
            expected.append({"journal_id": 1000, "direction": "inbound", "actor_id": cell["owner"],
                             "text": cell["request"], "reply_to": None})
            candidates.append({"matches_manifest": projected == expected and header.get("conversation") == "group:91000"
                               and header.get("latest_user_journal_id") == 1000,
                               "rows": projected, "header": header})
    return {"matches_manifest": len(candidates) == 1 and candidates[0]["matches_manifest"], "candidates": candidates}


def blind_file_metadata(info):
    if info is None:
        return None
    return {k: info.get(k) for k in ("kind", "sha256", "bytes")}


def bubble_texts(result):
    """Keep all user-visible payloads, including actual sent forward nodes.

    A forward is resolved only through the matching simulator message receipt;
    an unavailable payload stays explicit rather than becoming an empty bubble.
    """
    actions = result.get("evidence", {}).get("simulator", {}).get("actions", [])

    def render(segments):
        parts = []
        for block in segments:
            kind, data = block.get("type"), block.get("data", {})
            if kind == "text":
                parts.append(data.get("text", ""))
            elif kind == "node":
                content = data.get("content", [])
                author = data.get("user_id", data.get("uin", "未知"))
                parts.append(f"\n[转发节点：{author} {data.get('nickname', data.get('name', ''))}]\n"
                             + (content if isinstance(content, str) else render(content)) + "\n")
            elif kind not in {"at", "reply"}:
                parts.append(f"[QQ {kind}：非文本内容，需另核正文]")
        return "".join(parts)

    texts = []
    for bubble in result["bubbles"]:
        segments = bubble.get("event", {}).get("message", [])
        forwards = [b for b in segments if b.get("type") == "forward"]
        if forwards:
            matching = [a for a in actions if a.get("action") in {"send_group_forward_msg", "send_private_forward_msg"}
                        and str(a.get("response", {}).get("data", {}).get("message_id")) == str(bubble["message_id"])
                        and a.get("status") == "ok"]
            if len(matching) == 1:
                texts.append(render([b for b in segments if b.get("type") != "forward"])
                             + "[合并转发正文]\n" + render(matching[0].get("request", {}).get("messages", [])))
                continue
        texts.append(render(segments))
    return texts


def blind_packet(review_id, cell, result, tools, texts):
    # Arbitrary tool output and newly copied files can contain the guide or run ID.
    # Keep them in the private evidence; the blind reviewer gets factual deltas.
    changed = {f"file-{index:03d}": {"before": blind_file_metadata(result["before_files"].get(name)),
                                    "after": blind_file_metadata(result["after_files"].get(name))}
               for index, name in enumerate(result.get("changed_paths", []), 1)
               if name not in {"AGENTS.md", "persona.md"}}
    return {"review_id": review_id, "request": cell["request"], "bubbles": texts,
            "background_journal": [{"actor_id": r["actor_id"], "direction": r["direction"],
                                    "text": json.loads(r["content_json"])["text"]} for r in cell["journal"]],
            "protocol_status": result["protocol_status"], "terminal_event": result["terminal"].get("event"),
            "known_record_file_before": blind_file_metadata(result["before_files"].get(cell["record_file"])),
            "known_record_file_after": blind_file_metadata(result["after_files"].get(cell["record_file"])),
            "changed_files": changed, "standing_guide_changed_by_model": result.get("guide_changed_during_turn"),
            "tools": [{k: t.get(k) for k in ("name", "is_error", "exit_code")} for t in tools],
            "note": "Condition labels/prompt variants withheld; raw tool output and file paths/text kept in private evidence. "
                    "State truth requires separate developer verification; hashes alone do not prove a recorded fact. "
                    "Review all actual bubbles; no automatic quality judgement. User acceptance pending."}


def analyze(manifest_path: Path, output: Path):
    manifest_path = manifest_path.resolve(strict=True)
    directory = manifest_path.parent
    manifest = load(manifest_path)
    key = load(directory / "blind-key.json")
    if output.exists() or output.is_symlink() or output.resolve() != output.absolute():
        raise ValueError("output must be a new directory without indirect components")
    output.mkdir(mode=0o700)
    blind = output / "blind"
    blind.mkdir(mode=0o700)
    save(blind / "criteria.json", load(directory / "review-criteria.json"))
    summary = []
    inputs = []
    for cell in manifest["cells"]:
        run = directory / "runs" / cell["id"]
        if not (run / "result.json").is_file():
            summary.append({"cell": cell["id"], "status": "failed_without_result" if (run / "failure.json").exists() else "missing"})
            continue
        result = load(run / "result.json")
        cleanup = load(run / "cleanup.json") if (run / "cleanup.json").is_file() else {}
        sections, tool_hashes, settings, model_inputs, artifacts = [], set(), set(), [], []
        for path in result["request_snapshots"]:
            if not path.endswith(".json"):
                continue
            artifact = load(run / path)
            if artifact.get("metadata", {}).get("studyCell") != cell["id"]:
                continue
            artifacts.append(artifact)
            sections.append({s["name"]: sha(s["text"]) for s in artifact.get("assembly", {}).get("sections", [])
                             if s.get("name") in {"deployment:persona-prefix", "v2:active-persona", "v2:decision-guide", "v2:group-workspace-guide"}})
            tool_hashes.add(sha(artifact.get("tools")))
            settings.add(sha({"provider": artifact["metadata"].get("provider"), "model": artifact["metadata"].get("model"),
                              "settings": artifact.get("settings")}))
            model_inputs.append(sha({"messages": artifact.get("messages"), "system": artifact.get("system")}))
        tools = native_tools(run, result["native_session_snapshots"])
        projections = [initial_journal_projection(artifact, cell) for artifact in artifacts]
        initial_projection = {"matches_manifest": bool(projections) and all(p["matches_manifest"] for p in projections),
                              "snapshots_checked": len(projections),
                              "journal_projection": projections[0] if projections else None}
        texts = bubble_texts(result)
        review_id = next(k for k, v in key.items() if v == cell["id"])
        save(blind / (review_id + ".json"), blind_packet(review_id, cell, result, tools, texts))
        summary.append({"cell": cell["id"], "status": result["protocol_status"], "review_id": review_id,
                        "source_verification": result["source_verification"], "protocol_gates": result.get("protocol_gates"),
                        "cleanup_ok": cleanup.get("cleanup_ok", False), "changed_paths": result.get("changed_paths"),
                        "initial_journal_matches_manifest": initial_projection["matches_manifest"],
                        "guide_changed": result.get("guide_changed_during_turn"),
                        "tools": tools, "bubble_texts": texts, "standing_sections": sections,
                        "tool_hashes": sorted(tool_hashes), "config_hashes": sorted(settings), "model_input_hashes": model_inputs})
        inputs.append({"cell": cell["id"], "request": cell["request"], "seeded_journal": cell["journal"],
                       "actual_initial_journal": initial_projection,
                       "before_user_files": {name: info.get("sha256") for name, info in result["before_files"].items()
                                             if name != "AGENTS.md" and not name.startswith(".v2-memory/")}})
    completed = [r for r in summary if r["status"] not in {"missing", "failed_without_result"}]
    report = {"expected": len(manifest["cells"]), "results_observed": len(completed), "rows": summary,
              "all_protocol_complete": len(completed) == len(summary) and all(r["status"] == "complete" for r in completed),
              "all_cleanup_ok": len(completed) == len(summary) and all(r["cleanup_ok"] for r in completed),
              "all_initial_journal_verified": len(completed) == len(summary) and all(r["initial_journal_matches_manifest"] for r in completed),
              "standing_section_hashes": {name: sorted({v[name] for r in completed for v in r["standing_sections"] if name in v})
                                          for name in ("deployment:persona-prefix", "v2:active-persona", "v2:decision-guide", "v2:group-workspace-guide")},
              "stable_tool_definitions": len({v for r in completed for v in r["tool_hashes"]}) == 1,
              "stable_model_configs": len({v for r in completed for v in r["config_hashes"]}) == 1,
              "expression_acceptance": "Not determined by this script; review per case and preserve severe false-state claims"}
    save(output / "summary.json", report)
    save(output / "controlled-inputs.json", inputs)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    analyze(args.manifest, args.output)


if __name__ == "__main__":
    main()
