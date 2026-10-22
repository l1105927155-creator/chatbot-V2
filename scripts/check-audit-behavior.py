#!/usr/bin/env python3
"""Exercise fixed scripted behavior cases through a fresh real sandbox runtime."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests/fixtures/behavior/scripted-manifest.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


expansion = load_module("sandbox_expansion_for_behavior", ROOT / "scripts/check-sandbox-expansion.py")
from sandbox.evaluation import score  # noqa: E402


class Behavior(expansion.Expansion):
    owner = "90009"

    def start(self):
        super().start()
        self.request("POST", "/api/conversations", {"type": "group", "id": "91001", "title": "行为对照群"})
        current = self.http("/api/scenario")["admitted"]
        if "group:91001" not in current:
            self.scenario([*current, "group:91001"])

    def case(self, key: str, sender: str, name: str, *, tool: str | None = None,
             arguments: dict | None = None, marker: str, expect_error: bool = False) -> dict:
        before = self.evidence(key)
        old_turns = {event.get("native_turn_id") for event in before["lifecycle"]
                     if event.get("source") == "native-probe" and event.get("event") == "model/start"}
        if tool:
            self.plan(key, kind="tool", name=tool, arguments=arguments or {}, text_after=marker)
        else:
            self.plan(key, kind="reply", text=marker)
        sent = self.wake(key, f"behavior fixture {name}", sender=sender)
        self.answer(sent, marker)
        current = self.evidence(key)
        starts = [event for event in current["lifecycle"] if event.get("source") == "native-probe"
                  and event.get("event") == "model/start" and event.get("native_turn_id") not in old_turns]
        turns = {event.get("native_turn_id") for event in starts}
        assert len(turns) == 1, (name, starts)
        native_turn_id = turns.pop()
        if expect_error:
            assert any(event.get("event") == "tool/end" and event.get("native_turn_id") == native_turn_id
                       and event.get("tool") == tool and event.get("status") == "error"
                       for event in current["lifecycle"]), "failed skill call was not observed as an error"
        self.check(f"{name}: real sandbox turn {native_turn_id}")
        return {"id": name, "conversation_key": key, "native_turn_id": native_turn_id}

    def run_behavior(self):
        started = time.monotonic()
        try:
            self.start()
            assert self.http("/api/state")["scenario"]["owner_id"] == self.owner
            first = self.case("group:91000", "90001", "group-a-skill-positive", tool="skill",
                              arguments={"name": "chat-history"}, marker="GROUP_A_SKILL_LOADED")
            second = self.case("group:91001", "90002", "group-b-skill-positive", tool="skill",
                               arguments={"name": "chat-history"}, marker="GROUP_B_SKILL_LOADED")
            no_call = self.case("group:91000", "90001", "group-a-skill-negative",
                                marker="GROUP_A_NO_SKILL_NEEDED")
            failed = self.case("group:91000", "90001", "group-a-tool-failure", tool="skill",
                               arguments={"name": "absent-behavior-fixture"},
                               marker="GROUP_A_SKILL_FAILURE_OBSERVED", expect_error=True)
            recovered = self.case("group:91000", "90001", "group-a-recovery",
                                  marker="GROUP_A_RECOVERED_AFTER_TOOL_FAILURE")

            final_evidence = self.request("GET", "/api/evidence/export")
            assert not final_evidence.get("differences"), final_evidence.get("differences")
            states = {item["conversation_key"]: item for item in final_evidence["states"]}
            assert states["group:91000"]["session_id"] != states["group:91001"]["session_id"]

            manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
            actual_cases = {case["id"]: case for case in (first, second, no_call, failed, recovered)}
            for case in manifest["scenarios"]:
                case.update(actual_cases[case["id"]])
                case["session_id"] = states[case["conversation_key"]]["session_id"]
            report = score(final_evidence, manifest)
            (self.directory / "evidence.json").write_text(
                json.dumps(final_evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (self.directory / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (self.directory / "behavior-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result = {"passed": report["status"] == "passed", "mode": "scripted",
                      "real_qq_connections": 0, "checks": self.checks,
                      "report": str(self.directory / "behavior-report.json"),
                      "data_dir": str(self.directory), "elapsed_seconds": round(time.monotonic() - started, 2)}
            (self.directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if result["passed"] else 1
        finally:
            self.stop()
            volume = self.directory / "repo/.runtime/group-storage"
            if volume.exists():
                assert not any(os.path.ismount(path) for path in volume.glob("*")), "group sandbox volume remains mounted"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="fresh isolated sandbox directory")
    args = parser.parse_args()
    directory = args.data_dir or ROOT / ".runtime" / ("audit-behavior-" + uuid.uuid4().hex[:12])
    return Behavior(directory, True).run_behavior()


if __name__ == "__main__":
    raise SystemExit(main())
