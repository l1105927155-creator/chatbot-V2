#!/usr/bin/env python3
"""S1–S5 full-process acceptance using native DSH scripted fixtures, no model key."""
from __future__ import annotations

import importlib.util
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_smoke", ROOT / "scripts/check-sandbox.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class Expansion(base.Smoke):
    owner = "90009"

    def request(self, method, path, data=None):
        req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                      method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=35) as response:
                return json.load(response)
        except HTTPError as exc:
            raise AssertionError(f"{method} {path}: HTTP {exc.code}: {exc.read().decode()[:1000]}") from exc

    def start(self):
        cmd = [str(ROOT / "scripts/run-sandbox.sh"), "--dsh-mode", "scripted", "--owner-id", self.owner,
               "--owner-volume-mib", "32", "--data-dir", str(self.directory),
               "--onebot-port", str(self.onebot), "--ui-port", str(self.ui), "--mcp-port", str(self.mcp)]
        ready = subprocess.run([*cmd, "--prepare-only"], cwd=ROOT, capture_output=True, text=True)
        assert ready.returncode == 0, ready.stderr[-2000:]
        self.log = (self.directory / "runtime.log").open("ab")
        self.process = subprocess.Popen(cmd, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"), "Owner isolated runtime ready", timeout=90)
        self.workspace = self.directory / "repo/.runtime/dsh-workspace-owner"
        assert os.path.ismount(self.workspace), "Owner workspace is not a real bounded mount"

    def evidence(self, key=None):
        return self.request("GET", "/api/evidence" + ("?conversation_key=" + key if key else ""))

    def scenario(self, admitted):
        now = self.http("/api/scenario")
        return self.request("PUT", "/api/scenario", {"revision": now["revision"], "admitted": admitted})

    def plan(self, key, **plan):
        return self.request("POST", "/api/scripts", {"conversation_key": key, **plan})

    def fault(self, key, target, kind, **kwargs):
        return self.request("POST", "/api/faults", {"conversation_key": key, "target": target, "kind": kind, **kwargs})

    def wake(self, key, text="test fixture", *, sender="90001", mention=True):
        kind, ident = key.split(":", 1)
        message = [{"type": "text", "data": {"text": text}}]
        if kind == "group" and mention:
            message.insert(0, {"type": "at", "data": {"qq": "99000"}})
        event = self.request("POST", "/api/send", {"conversation": {"type": kind, "id": ident},
                             "sender_id": sender, "message": message})["event"]
        self.wait(lambda: any(str(r["message_id"]) == str(event["message_id"]) for r in self.rows()), "canonical input")
        return event

    def answer(self, event, marker=None):
        key = event["conversation"]
        out = self.wait(lambda: next((e for e in self.events() if e["direction"] == "outbound"
                                     and e["conversation"] == key and e["message_id"] > event["message_id"]), None),
                        "actual AstrBot answer", timeout=50)
        if marker:
            assert marker in json.dumps(out, ensure_ascii=False), out
        self.wait(lambda: any(str(r["message_id"]) == str(out["message_id"]) for r in self.rows()), "canonical output")
        return out

    def wait_native(self, selector, *, after=None, timeout=30):
        result = self.request("POST", "/api/wait", {**selector, "after": after, "timeout_seconds": timeout})
        assert result["matched"], result
        return result

    def run_expansion(self):
        group, excluded, owner = "group:91000", "group:91999", "private:" + self.owner
        try:
            self.start()
            assert self.http("/api/state")["scenario"]["owner_id"] == self.owner
            self.check("startup-bound Owner with real bounded volume and separate domains")
            self.request("POST", "/api/conversations", {"type": "group", "id": "91999", "title": "排除测试群"})
            before = len(self.events())
            event = self.wake(excluded)
            time.sleep(.3)
            ev = self.evidence(excluded)
            assert not ev["states"] and not any(x["direction"] == "outbound" for x in self.events()[before:])
            assert ev["journal"]
            original = self.http("/api/scenario")["admitted"]
            self.scenario([*filter(lambda k: k != group, original), excluded])
            self.wake(group)
            self.plan(excluded, kind="reply", text="SWAPPED_GROUP")
            self.answer(self.wake(excluded), "SWAPPED_GROUP")
            self.scenario([*original, excluded])
            self.check("declared-but-excluded input captured; admission swap changes actual routing")

            self.answer(self.wake(owner, "/v2agent persona developer", sender=self.owner), "人格已切换")
            self.answer(self.wake(owner, "/v2agent scene development", sender=self.owner), "场景已切换")
            self.plan(owner, kind="tool", name="bash", arguments={"command": "printf OWNER_READY > expansion.txt", "description": "Sandbox bounded write", "workdir": "."}, text_after="OWNER_WRITE_DONE")
            self.answer(self.wake(owner, sender=self.owner), "OWNER_WRITE_DONE")
            assert (self.workspace / "expansion.txt").read_text() == "OWNER_READY"
            states = self.evidence()["states"]
            assert any(s["domain"] == "owner" and s["conversation_key"] == owner for s in states)
            self.plan(group, kind="tool", name="bash", arguments={"command": "printf ESCAPED > group-escape.txt", "workdir": "."})
            self.answer(self.wake(group, sender=self.owner), "unavailable")
            assert not (self.workspace / "group-escape.txt").exists()
            self.check("Owner controls and confined Bash succeed; same identity in group cannot execute")

            # Scope/phase/nth: other conversations and earlier invocations do not consume the rule.
            rule = self.fault(group, "model", "truncate", nth=2, phase="after", limit_chars=5, delay_ms=1)
            self.plan(group, kind="reply", text="FIRST_INTACT")
            self.answer(self.wake(group), "FIRST_INTACT")
            self.plan(excluded, kind="reply", text="OTHER_INTACT")
            self.answer(self.wake(excluded), "OTHER_INTACT")
            self.plan(group, kind="reply", text="SECOND_TRUNCATED")
            self.answer(self.wake(group), "SECON")
            hit = next(r for r in self.http("/api/faults")["faults"] if r["id"] == rule["id"])
            assert hit["status"] == "hit" and hit["seen"] == 2
            self.check("one-shot nth/phase fault targets exact conversation and preserves unrelated turns")

            # Load research scene through real Owner control in the group; tools still remain chat-only.
            self.answer(self.wake(group, "/v2agent scene research", sender=self.owner), "场景已切换")
            rule = self.fault(group, "search", "error", delay_ms=1)
            self.plan(group, kind="tool", name="web_search", arguments={"queries": ["sandbox fixture"]}, text_after="SEARCH_FAILURE_OBSERVED")
            self.answer(self.wake(group), "SEARCH_FAILURE_OBSERVED")
            rule2 = self.fault(group, "fetch", "truncate", phase="after", limit_chars=10, delay_ms=1)
            self.plan(group, kind="tool", name="web_fetch", arguments={"url": "https://fixture.test/document"}, text_after="FETCH_TRUNCATION_OBSERVED")
            self.answer(self.wake(group), "FETCH_TRUNCATION_OBSERVED")
            evidence = self.evidence(group)
            inputs = [e.get("input", "") for e in evidence["lifecycle"] if e.get("event") == "model/start"]
            assert any("sandbox injected error" in str(value) for value in inputs), "tool error absent from next model input"
            assert any("Sandbox fi" in str(value) and "truncated" in str(value) for value in inputs), "truncation absent from model input"
            self.check("native search failure and fetch truncation reach subsequent model input")

            # Start real Owner Bash and verify the command actually entered the mounted workspace.
            cursor = self.http("/api/events")["cursor"]
            self.plan(owner, kind="tool", name="bash", arguments={
                "command": "printf started > cancel-ready.txt; (sleep 4; printf escaped > orphan.txt) & sleep 30",
                "description": "Sandbox cancellable descendant", "workdir": "."}, text_after="MUST_NOT_COMPLETE")
            pending_input = self.wake(owner, sender=self.owner)
            self.wait_native({"event": "tool/start", "tool": "bash", "conversation_key": owner}, after=cursor)
            self.wait(lambda: (self.workspace / "cancel-ready.txt").exists(), "actual bounded Bash started")
            cursor = self.http("/api/events")["cursor"]
            self.answer(self.wake(owner, "/v2agent cancel", sender=self.owner), "已请求取消")
            self.wait_native({"event": "dsh/stop", "stop_reason": "cancelled", "conversation_key": owner}, after=cursor)
            time.sleep(4.2)
            assert not (self.workspace / "orphan.txt").exists(), "cancel left a running descendant"
            state = next(s for s in self.evidence(owner)["states"] if s["domain"] == "owner")
            pending = state["pending_upper_cursor"]
            assert pending and state["last_seen_journal_id"] < pending
            prior_calls = sum(e.get("event") == "model/start" for e in self.evidence(owner)["lifecycle"])
            later = self.wake(owner, "queued fixture while pending", sender=self.owner)
            self.wait(lambda: any(e.get("event") == "turn/failed" and e.get("error_type") == "PendingTurnError"
                                  and e.get("inbound_message_id") == str(later["message_id"])
                                  for e in self.evidence(owner)["audit"]), "pending input rejected by actual Router")
            assert sum(e.get("event") == "model/start" for e in self.evidence(owner)["lifecycle"]) == prior_calls
            assert not any(e["direction"] == "outbound" and e["message_id"] > later["message_id"] for e in self.events())
            self.answer(self.wake(owner, f"/v2agent recover {pending}", sender=self.owner), "已解除暂停")
            resolved = next(s for s in self.evidence(owner)["states"] if s["domain"] == "owner")
            assert resolved["pending_upper_cursor"] is None and resolved["last_seen_journal_id"] == pending
            self.plan(owner, kind="reply", text="RECOVERED_NEW_TURN")
            self.answer(self.wake(owner, sender=self.owner), "RECOVERED_NEW_TURN")
            self.check("wait real Bash start, cancel descendants, recover exact pending without consuming newer input")

            evidence = self.evidence()
            assert evidence["atomic_snapshot"] is False
            assert not evidence["differences"], evidence["differences"]
            exported = self.request("GET", "/api/evidence/export")
            assert exported["journal"] and exported["receipts"] and exported["lifecycle"]
            (self.directory / "evidence.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2))
            self.check("read-only correlated canonical/transport/state/native evidence exports")
            return {"passed": True, "mode": "scripted", "real_qq_connections": 0, "checks": self.checks,
                    "data_dir": str(self.directory)}
        finally:
            self.stop()
            assert not os.path.ismount(self.directory / "repo/.runtime/dsh-workspace-owner"), "Owner volume not unmounted"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="fresh isolated acceptance directory")
    args = parser.parse_args()
    directory = args.data_dir or ROOT / ".runtime" / ("sandbox-expansion-" + uuid.uuid4().hex[:12])
    started = time.monotonic()
    result = Expansion(directory, True).run_expansion()
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    path = directory / "result.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
