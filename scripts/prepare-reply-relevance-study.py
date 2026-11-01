#!/usr/bin/env python3
"""Freeze a private, synthetic full-task/Journal study; never edits live data."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RECORD = "memory/preferences.md"
INITIAL = "# 手工偏好资料\n\n- 较长报告按需要作为文件交付\n"
RECORDED = INITIAL + "\n## 本次违规记录\n- 2026-11-01：把活动说明拆成六条普通消息，占了两屏。以后将完整长文作为文件或合并转发交付。\n"
REQUEST = "把刚才把活动说明拆成六条、刷了两屏的这次违规记下来"
P1_REQUEST = "刚才那次拆六条刷屏的事，记下来了吗？"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def row(jid: int, text: str, *, actor="90002", direction="inbound", reply_to=None) -> dict:
    return dict(journal_id=jid, platform_id="v2-sandbox", bot_id="99000", conversation_key="group:91000",
                conversation_type="group", direction=direction, actor_id=actor, actor_name="测试群友" if actor != "99000" else "测试Bot",
                source="reply_relevance_synthetic_fixture", message_id=str(700000 + jid), reply_to=reply_to,
                content_json=json.dumps({"text": text, "segments": [{"type": "text", "text": text}]}, ensure_ascii=False),
                created_at=1793490000 + jid, observed_at_ns=(1793490000 + jid) * 10**9)


def make_cells(repetitions: int = 3) -> list[dict]:
    base = row(1, "刚才我把一份活动说明拆成六条普通消息，占了两屏，这样发不合适", actor="99000", direction="outbound")
    common = dict(owner="90009", record_file=RECORD, journal=[base], initial_record=None, deny_writes=False)
    cells = []
    for scenario in ("simple", "details", "permission"):
        for arm in ("A", "B"):
            for repeat in range(repetitions):
                cells.append({**common, "id": f"P0-{scenario}-{arm}-r{repeat}", "study": "P0", "scenario": scenario,
                              "arm": arm, "repeat": repeat,
                              "request": REQUEST + ("，并告诉我怎么记的，给我能核实的依据" if scenario == "details" else ""),
                              "deny_writes": scenario == "permission"})
    # The target discussion is the ONLY variable among these three conditions.
    # No mention, no answer, before the final wake. All are ordinary member speech.
    discussions = {
        "none": [],
        "unrelated": [row(10, "昨晚巡查脚本没有生成报告"),
                      row(11, "好像正好卡在订阅额度过期的时间，可能模型请求被截断了"),
                      row(12, "还没查到原始错误，只是猜测，暂时先放一边")],
        "related": [row(10, "昨晚巡查脚本没有生成报告"),
                    row(11, "好像正好卡在订阅额度过期的时间，可能模型请求被截断了"),
                    row(12, "我等会儿会问违规记没记。答的时候也帮我区分一下刚才巡查失败说法里哪些是事实、哪些只是猜测", actor="90009")],
    }
    for scenario, discussion in discussions.items():
        for repeat in range(repetitions):
            cells.append({**common, "id": f"P1-{scenario}-B-r{repeat}", "study": "P1", "scenario": scenario,
                          "arm": "B", "repeat": repeat, "request": P1_REQUEST,
                          "initial_record": RECORDED, "journal": [base, *discussion]})
    return cells


def protocol(seed=20261102, repetitions=3) -> dict:
    cells = make_cells(repetitions)
    rng = random.Random(seed)
    order = []
    for study in ("P0", "P1"):
        ids = [c["id"] for c in cells if c["study"] == study]
        rng.shuffle(ids)
        order.extend(ids)
    blind = list(range(1, len(cells) + 1))
    rng.shuffle(blind)
    return {"version": 1, "seed": seed, "repetitions": repetitions, "cells": cells, "order": order,
            "blind_key": {f"review-{n:03d}": c["id"] for n, c in zip(blind, cells)},
            "criteria": {
                "method": "Condition-blind qualitative annotation, then developer verification of files/tools; no length score or user acceptance claim",
                "axes": ["交流意图是否满足", "必要事实有无遗漏", "未被请求的实施细节", "语义复述或旧话题漂移", "状态事实误报", "人格与主动表达是否保留"],
                "severe_failure": "False completed/unknown/permission/autoload claims are separate severe failures even if concise",
                "P0_simple": "Verify actual recording; natural result acknowledgement without unsolicited implementation report",
                "P0_details": "Verify actual recording; give requested implementation details with verifiable evidence",
                "P0_permission": "Real native write-policy demotion, target unchanged; report failure accurately, do not imply recorded",
                "P1_none_unrelated": "Same recorded file, latest question and all context except target segment; confirm record without unrelated patrol advice",
                "P1_related": "A prior ordinary user explicitly requests joining topics; allow relevant clarification while keeping speculation distinct from fact",
            },
            "stopping": ["No automatic retry or replacing failed cells", "Stop causal claims if route/guide/tool/state controls fail",
                         "Finish all predeclared repetitions; negative expression results do not truncate matrix", "Never send to live QQ or deploy candidate"],
            "limits": ["Synthetic deidentified facts inspired by original case, not exact production replay",
                       "P1 record is verified initial filesystem state, not a fabricated model tool-result",
                       "The related positive control changes only the old-discussion segment, not the latest wake",
                       "Only current-scope task/old-topic hypotheses; nine-scenario user acceptance remains separate"]}


def copy_code(source: Path, target: Path, *, exclude_data=False) -> None:
    shutil.copytree(source, target, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", ".runtime", "node_modules", "__pycache__", "*.pyc", *( ["data"] if exclude_data else [])))


def freeze(args) -> Path:
    spec = importlib.util.spec_from_file_location("study_guide_preparation", ROOT / "scripts/prepare-reply-relevance-guides.py")
    guides = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guides)
    for path in (args.workspace, args.runtime_source, args.shared_profile, args.output):
        guides.reject_symlink_path(path)
    if args.output.exists():
        raise ValueError("output already exists")
    protected = [args.workspace, args.runtime_source / "dsh", args.runtime_source / "astrbot-plugins"]
    if any(p.resolve() == args.output.resolve() or p.resolve() in args.output.resolve().parents for p in protected):
        raise ValueError("output must be outside source workspace/code trees")
    for name in ("AGENTS.md", "persona.md"):
        guides.read_regular(args.workspace / name, name)
    args.output.mkdir(mode=0o700, parents=False)
    guides.build(args.workspace / "AGENTS.md", ROOT / "dsh/templates/group/AGENTS.md", args.output / "guides", None)
    workspace = args.output / "workspace"
    workspace.mkdir(mode=0o700)
    shutil.copy2(args.workspace / "persona.md", workspace / "persona.md")
    shutil.copy2(args.output / "guides/baseline.md", workspace / "AGENTS.md")
    save(workspace / "Owner.json", [{"OwnerID": "90009", "nickname": "测试Owner", "group_nickname": "测试Owner"}])
    (workspace / "memory").mkdir(mode=0o700)
    (workspace / RECORD).write_text(INITIAL, encoding="utf-8")
    (workspace / "gloup-rules").mkdir(mode=0o700)
    (workspace / "gloup-rules/20261029.md").write_text("# 合成群规\n\n合理使用机器人，避免把完整长文拆成许多普通消息影响聊天。\n", encoding="utf-8")
    # Keep the real affinity operations/guide/Skill, initialize unrelated member data afresh.
    for rel in ("apps/affinity", "skills/affinity"):
        if (args.workspace / rel).is_dir():
            (workspace / rel).parent.mkdir(mode=0o700, exist_ok=True)
            copy_code(args.workspace / rel, workspace / rel, exclude_data=True)
    runtime = args.output / "runtime-source"
    runtime.mkdir(mode=0o700)
    for name in ("dsh", "astrbot-plugins"):
        copy_code(args.runtime_source / name, runtime / name)
    if "V2_GROUP_OWNER_WRITE_DEMOTIONS" not in (runtime / "dsh/plugins/agent-profile/group-write-policy.mjs").read_text():
        raise ValueError("frozen native write-policy lacks real demotion fixture")
    shared = args.output / "shared-models.patch.yml"
    shutil.copy2(args.shared_profile, shared)
    prereg = protocol(args.seed, args.repetitions)
    save(args.output / "protocol.json", prereg)
    # The key is separate from blind packets and is never given to the blind reviewer.
    save(args.output / "blind-key.json", prereg["blind_key"])
    save(args.output / "review-criteria.json", prereg["criteria"])
    files = [p for p in args.output.rglob("*") if p.is_file()]
    code = [Path(__file__).resolve(), ROOT / "scripts/run-reply-relevance-study.py",
            ROOT / "scripts/prepare-reply-relevance-guides.py", ROOT / "scripts/check-sandbox-live-owner.py",
            ROOT / "sandbox/runtime.py", ROOT / "sandbox/probe.mjs", ROOT / "sandbox/request-snapshot.mjs", ROOT / "upstream.lock.json"]
    manifest = {"version": 1, "main_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "runtime_source": str(runtime.resolve()), "shared_profile": str(shared.resolve()), "provider": args.provider, "model": args.model,
                "workspace": str(workspace.resolve()), "guides": {"A": str((args.output / "guides/baseline.md").resolve()), "B": str((args.output / "guides/candidate.md").resolve())},
                "cells": prereg["cells"], "order": prereg["order"],
                "source_provenance": {"workspace": str(args.workspace), "runtime": str(args.runtime_source),
                                      "agents_sha256": digest(args.workspace / "AGENTS.md"), "persona_sha256": digest(args.workspace / "persona.md")},
                "frozen_hashes": {str(p.resolve()): digest(p) for p in [*files, *code]}}
    save(args.output / "manifest.json", manifest)
    for p in args.output.rglob("*"):
        if p.is_file():
            p.chmod(0o600)
    return args.output / "manifest.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--runtime-source", required=True, type=Path)
    parser.add_argument("--shared-profile", required=True, type=Path)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", default=20261102, type=int)
    parser.add_argument("--repetitions", default=3, type=int)
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error("at least two repetitions are required")
    os.umask(0o077)
    print(freeze(args))


if __name__ == "__main__":
    main()
