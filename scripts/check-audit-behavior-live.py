#!/usr/bin/env python3
"""Run fixed live-model tool-choice samples in an isolated sandbox, never QQ."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests/fixtures/behavior/live-manifest.json"
PROVIDER = "deepseek-official"
MODEL = "deepseek-v4-flash"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


live_owner = load_module("live_owner_for_behavior", ROOT / "scripts/check-sandbox-live-owner.py")
from sandbox.evaluation import EvaluationError, score  # noqa: E402


class LiveBehavior(live_owner.LiveOwner):
    def start(self):
        # The normal LiveOwner startup flow, with child output suppressed so no
        # credential-shaped text can be copied into artifacts or the terminal.
        cmd = [str(ROOT / "scripts/run-sandbox.sh"), "--dsh-mode", "live", "--owner-id", self.owner,
               "--snapshot-mode", "text", "--owner-volume-mib", "32", "--data-dir", str(self.directory),
               "--onebot-port", str(self.onebot), "--ui-port", str(self.ui), "--mcp-port", str(self.mcp)]
        prepared = live_owner.subprocess.run([*cmd, "--prepare-only"], cwd=ROOT, stdin=live_owner.subprocess.DEVNULL,
                                             stdout=live_owner.subprocess.DEVNULL,
                                             stderr=live_owner.subprocess.DEVNULL)
        if prepared.returncode:
            raise RuntimeError("isolated live sandbox preparation failed")
        self.process = live_owner.subprocess.Popen(
            cmd, cwd=ROOT, stdin=live_owner.subprocess.DEVNULL,
            stdout=live_owner.subprocess.DEVNULL, stderr=live_owner.subprocess.DEVNULL,
            start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"), "isolated live sandbox", timeout=90)
        self.workspace = self.directory / "repo"
        def healthy():
            with live_owner.urlopen(f"http://127.0.0.1:{self.mcp}/healthz", timeout=3) as response:
                return json.load(response).get("ready")
        self.wait(healthy, "execution contract ready", timeout=90)

    def save_private(self, relative: str, value):
        target = self.directory / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target.chmod(0o600)

    def reset_sample(self, scene: str, prior_sessions: set[str]) -> str:
        self.control("/new", "已开始新的 Agent 对话", sender=self.owner)
        state = self.state()
        if not state or state["session_id"] in prior_sessions:
            raise RuntimeError("sample session was not reset independently")
        self.control("/v2agent scene " + scene, "场景已切换", sender=self.owner)
        state = self.state()
        return state["session_id"]

    def run_sample(self, sample: dict, scenario: dict, prior_sessions: set[str], event_store: dict[str, dict]):
        sid = self.reset_sample("research" if sample["sample_kind"] == "positive" else "casual-chat", prior_sessions)
        prior_sessions.add(sid)
        event = self.wake(self.key, sample["prompt"], sender=self.owner)
        out = self.wait(lambda: next((item for item in self.events()
                       if item["direction"] == "outbound" and item["conversation"] == event["conversation"]
                       and item["message_id"] > event["message_id"]), None),
                        "live model response", timeout=170)
        self.wait(lambda: any(row["message_id"] == str(out["message_id"]) and row["direction"] == "outbound"
                              for row in self.rows()), "confirmed outbound Journal", timeout=30)
        self.wait(lambda: any(row.get("event") == "turn/completed" for row in self.trace(event)),
                  "completed live model turn", timeout=30)
        evidence = self.request("GET", "/api/evidence/export")
        events = evidence.get("lifecycle", [])
        turn_starts = [row for row in events if row.get("source") == "native-probe"
                       and row.get("event") == "model/start" and row.get("session_id") == sid]
        turn_ids = {row.get("native_turn_id") for row in turn_starts}
        if len(turn_ids) != 1:
            raise RuntimeError("sample did not resolve to exactly one native turn")
        native_turn_id = next(iter(turn_ids))
        scenario.update({"native_turn_id": native_turn_id, "session_id": sid,
                         "input_sha256": hashlib.sha256(sample["prompt"].encode("utf-8")).hexdigest()})
        event_store[sid] = evidence
        self.save_private("evidence/samples/" + sample["id"] + ".json", evidence)
        return {"id": sample["id"], "sample_kind": sample["sample_kind"], "session_id": sid,
                "native_turn_id": native_turn_id, "inbound_message_id": event["message_id"],
                "outbound_message_id": out["message_id"], "outbound_sha256": hashlib.sha256(
                    json.dumps(out, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()}

    @staticmethod
    def combine_evidence(last: dict, snapshots: dict[str, dict]) -> dict:
        combined = dict(last)
        unique = {}
        for snapshot in snapshots.values():
            for event in snapshot.get("lifecycle", []):
                key = event.get("runtime_event_id") or event.get("audit_event_id")
                if key is None:
                    key = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                unique[key] = event
        combined["lifecycle"] = sorted(unique.values(), key=lambda row: row.get("observed_at_ns", row.get("at", 0)))
        return combined

    def run_samples(self) -> dict:
        started = time.monotonic()
        fixture_path = self.fixture
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture["dataset_sha256"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        samples = {item["id"]: item for item in fixture.pop("samples")}
        scenarios = {item["id"]: item for item in fixture["scenarios"]}
        prior_sessions: set[str] = set()
        snapshots: dict[str, dict] = {}
        outcomes = []
        try:
            self.start()
            for sample_id, scenario in scenarios.items():
                sample = samples[sample_id]
                try:
                    outcomes.append(self.run_sample(sample, scenario, prior_sessions, snapshots))
                    print(json.dumps({"sample": sample_id, "status": "observed"}, ensure_ascii=False), flush=True)
                except Exception as exc:
                    # Do not retain provider text or exception payloads in the report.
                    outcomes.append({"id": sample_id, "sample_kind": sample["sample_kind"],
                                     "status": "failed", "error_type": type(exc).__name__})
                    if "native_turn_id" not in scenario:
                        scenario.update({"native_turn_id": "unobserved-" + sample_id,
                                         "session_id": self.state()["session_id"] if self.state() else "unobserved"})
                    snapshot = self.request("GET", "/api/evidence/export")
                    snapshots[scenario["session_id"]] = snapshot
                    self.save_private("evidence/samples/" + sample_id + "-failure.json", snapshot)
                    self.save_private("progress.json", outcomes)
                    print(json.dumps({"sample": sample_id, "status": "failed",
                                      "error_type": type(exc).__name__}, ensure_ascii=False), flush=True)

            final = self.request("GET", "/api/evidence/export")
            snapshots["final"] = final
            evidence = self.combine_evidence(final, snapshots)
            for scenario in fixture["scenarios"]:
                if "native_turn_id" not in scenario:
                    scenario["native_turn_id"] = "unobserved-" + scenario["id"]
                    scenario["session_id"] = "unobserved"
                scenario["input_sha256"] = hashlib.sha256(samples[scenario["id"]]["prompt"].encode("utf-8")).hexdigest()
            report = None
            try:
                report = score(evidence, fixture)
            except EvaluationError as exc:
                report = {"schema": "v2-behavior-evaluation/1", "status": "incomplete",
                          "evaluator_error": type(exc).__name__, "message": str(exc)}
            self.save_private("evidence.json", evidence)
            self.save_private("manifest.json", fixture)
            self.save_private("behavior-report.json", report)
            return {"passed": report.get("status") == "passed" and all(item.get("status") != "failed" for item in outcomes),
                    "status": report.get("status"), "mode": "live", "provider": PROVIDER, "model": MODEL,
                    "samples": outcomes, "report": str(self.directory / "behavior-report.json"),
                    "data_dir": str(self.directory), "real_qq_connections": 0,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "experiment_label": fixture.get("experiment_label"),
                    "interpretation": ("Tool-chain completion and tool selection only; no broad semantic-quality claim. "
                                       "Three paired samples are mechanism evidence, not a population estimate."),
                    "limitations": ["Synthetic Owner private sessions in an isolated sandbox; no QQ/NapCat connection",
                                    "Each sample uses a new /new session; prompts and thresholds are fixed in the fixture",
                                    "Token usage and latency are recorded when the provider exposes them; missing values remain explicit"]}
        finally:
            self.stop()
            assert not os.path.ismount(self.directory / "repo/.runtime/dsh-workspace-owner"), "Owner volume remains mounted"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="fresh isolated data directory; no real QQ is connected")
    parser.add_argument("--manifest", type=Path, default=FIXTURE,
                        help="fixed live manifest (default: explicit-invocation suite)")
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("DEEPSEEK_API_KEY must exist in the current environment")
    fixture_path = args.manifest.expanduser().absolute()
    allowed = {FIXTURE.absolute(),
               (ROOT / "tests/fixtures/behavior/live-natural-manifest.json").absolute(),
               (ROOT / "tests/fixtures/behavior/live-natural-v2-manifest.json").absolute()}
    if fixture_path not in allowed:
        parser.error("--manifest must name a checked-in fixed live manifest")
    os.environ["V2_DSH_PROVIDER"] = PROVIDER
    os.environ["V2_DSH_MODEL"] = MODEL
    directory = args.data_dir or ROOT / ".runtime" / ("audit-behavior-live-" + uuid.uuid4().hex[:12])
    directory = directory.expanduser().absolute()
    if directory.exists():
        parser.error("data directory must be fresh; failed evidence is never overwritten")
    os.umask(0o077)
    runner = LiveBehavior(directory)
    runner.fixture = fixture_path
    result = runner.run_samples()
    runner.save_private("result.json", result)
    print(json.dumps({"passed": result["passed"], "status": result["status"],
                      "sample_count": len(result["samples"]), "result": str(directory / "result.json")},
                     ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
