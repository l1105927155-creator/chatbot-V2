#!/usr/bin/env python3
"""Controlled full-process smoke: real pinned AstrBot, local OneBot, optional real DSH.

Runtime data and the result stay under .runtime. No real QQ connection is used.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class Smoke:
    def __init__(self, directory: Path, live: bool):
        self.directory, self.live = directory, live
        self.onebot, self.ui, self.mcp = ports()
        self.base = f"http://127.0.0.1:{self.ui}"
        self.process = None
        self.log = None
        self.checks = []

    def http(self, path, data=None):
        req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def wait(self, predicate, label, timeout=35):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise AssertionError(f"runtime exited during {label}; see {self.directory / 'runtime.log'}")
            try:
                last = predicate()
                if last:
                    return last
            except (URLError, OSError, sqlite3.OperationalError):
                pass
            time.sleep(0.15)
        raise AssertionError(f"timed out: {label}")

    def start(self):
        cmd = [str(ROOT / "scripts/run-sandbox.sh"), "--data-dir", str(self.directory),
               "--onebot-port", str(self.onebot), "--ui-port", str(self.ui), "--mcp-port", str(self.mcp)]
        if not self.live:
            cmd.append("--no-dsh")
        prepared = subprocess.run([*cmd, "--prepare-only"], cwd=ROOT, capture_output=True, text=True)
        if prepared.returncode:
            raise AssertionError("sandbox preparation failed: " + prepared.stderr[-2000:])
        self.log = (self.directory / "runtime.log").open("ab")
        self.process = subprocess.Popen(cmd, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"), "OneBot connection", timeout=90)
        self.wait(lambda: self.journal_path().exists(), "Journal initialized")
        if self.live:
            self.wait(lambda: "V2 DSH router is idle" not in (self.directory / "runtime.log").read_text(errors="replace")
                      and self.state_path().exists(), "router state created")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def journal_path(self):
        return self.directory / "astrbot/data/plugin_data/v2_journal/journal.sqlite3"

    def state_path(self):
        return self.directory / "astrbot/data/plugin_data/v2_dsh_router/state-chat.sqlite3"

    def rows(self):
        with sqlite3.connect(self.journal_path()) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM journal ORDER BY journal_id")]

    def session_ids(self):
        with sqlite3.connect(self.state_path()) as db:
            return dict(db.execute("SELECT conversation_key,session_id FROM conversation_state"))

    def events(self):
        return self.http("/api/state")["events"]

    def send(self, text, *, sender="90001", private=False, mention=False, reply=None, faults=None, message=None):
        segments = message if message is not None else [{"type": "text", "data": {"text": text}}]
        if mention:
            segments = [{"type": "at", "data": {"qq": "99000"}}, *segments]
        payload = {"conversation": {"type": "private" if private else "group", "id": sender if private else "91000"},
                   "sender_id": sender, "message": segments, "faults": faults or {}}
        if reply:
            payload["reply_to"] = reply
        result = self.http("/api/send", payload)
        event = result["event"]
        mid = str(event["message_id"])
        self.wait(lambda: any(str(r["message_id"]) == mid for r in self.rows()), "inbound Journal")
        return event

    def response_after(self, event, *, timeout=35, confirmed=True):
        mid = int(event["message_id"])
        out = self.wait(lambda: next((e for e in self.events() if e["direction"] == "outbound"
                                     and int(e["message_id"]) > mid), None), "Bot output", timeout=timeout)
        if confirmed:
            self.wait(lambda: any(str(r["message_id"]) == str(out["message_id"]) and r["direction"] == "outbound"
                                  for r in self.rows()), "outbound Journal")
        return out

    def check(self, label):
        self.checks.append(label)
        print(label, flush=True)

    def run(self, timeout_case=False):
        try:
            self.start()
            state = self.http("/api/state")
            assert {"90001", "90002"} <= {m["id"] for m in state["members"]}
            assert state["bot"]["id"] == "99000"
            self.check("isolated runtime and two members connected")
            # Quiet group input is still recorded, without a free reply.
            quiet = self.send("quiet group transport fixture")
            time.sleep(0.6)
            assert not any(e["direction"] == "outbound" for e in self.events())
            self.check("unmentioned group input captured without reply")
            cmd = self.send("/v2probe", sender="90002")
            out = self.response_after(cmd)
            assert "v2 phase 1 probe: ok" in json.dumps(out)
            self.check("group deterministic command and confirmed Journal")
            cmd = self.send("/v2probe", private=True)
            private_out = self.response_after(cmd)
            assert private_out["conversation"] == {"type": "private", "id": "90001"}
            self.check("private deterministic command and correct peer")
            quote = self.send("/v2probe", reply=out["message_id"])
            self.response_after(quote)
            assert any(a["action"] == "get_msg" for a in self.http("/api/state")["actions"])
            self.check("platform reply lookup uses original message")
            for kind in ("face", "image", "file", "forward"):
                event = self.send("/sandbox " + kind)
                rich = self.response_after(event)
                assert any(s["type"] == kind for s in rich["event"]["message"]), (kind, rich)
                self.check("actual AstrBot outbound " + kind)
            state = self.http("/api/state")
            image = next(a for a in state["assets"] if a["content_type"] == "image/png")
            document = next(a for a in state["assets"] if a["name"].endswith(".txt"))
            event = self.send("", message=[
                {"type": "face", "data": {"id": "14"}},
                {"type": "image", "data": {"file": "sandbox://" + image["id"]}},
                {"type": "file", "data": {"file": "sandbox://" + document["id"]}},
            ])
            content = json.dumps(next(r for r in self.rows() if str(r["message_id"]) == str(event["message_id"])))
            assert all(kind in content for kind in ("face", "image", "file"))
            forward = self.send("", message=[
                {"type": "node", "data": {"user_id": "90002", "nickname": "Bob", "content": [
                    {"type": "text", "data": {"text": "forward original fixture"}}]}},
            ])
            assert forward["event"]["message"][0]["type"] == "forward"
            fid = forward["event"]["message"][0]["data"]["id"]
            assert "forward original fixture" in json.dumps(self.http("/api/forward/" + fid))
            self.check("rich inbound segments captured in canonical Journal")
            duplicate = self.send("duplicate fixture", faults={"duplicate": True})
            self.http("/api/replay", {"message_id": duplicate["message_id"]})
            time.sleep(0.6)
            assert sum(str(r["message_id"]) == str(duplicate["message_id"]) for r in self.rows()) == 1
            self.check("duplicate injection and explicit replay preserve Journal idempotency")
            resend = self.http("/api/replay", {"message_id": duplicate["message_id"], "mode": "new"})["event"]
            assert resend["message_id"] != duplicate["message_id"]
            self.wait(lambda: any(str(r["message_id"]) == str(resend["message_id"]) for r in self.rows()),
                      "new-ID replay recorded")
            self.check("explicit new-ID replay re-enters the actual pipeline")
            self.http("/api/members", {"id": "90003", "nickname": "Carol", "card": "新测试成员"})
            self.http("/api/conversations", {"type": "private", "id": "90003", "title": "Carol"})
            scenario = self.http("/api/scenario")
            req = Request(self.base + "/api/scenario", data=json.dumps({
                "revision": scenario["revision"], "admitted": [*scenario["admitted"], "private:90003"]
            }).encode(), headers={"Content-Type": "application/json"}, method="PUT")
            with urlopen(req, timeout=5) as response:
                assert response.status == 200
            self.response_after(self.send("/v2probe", sender="90003", private=True))
            self.check("generated member can send in an independent private conversation")
            missing = self.send("/v2probe", private=True, faults={"drop_feedback": True})
            missing_out = self.response_after(missing, confirmed=False)
            time.sleep(0.5)
            assert not any(str(r["message_id"]) == str(missing_out["message_id"]) for r in self.rows())
            self.check("lost self feedback does not fabricate confirmed output")
            if self.live:
                event = self.send("请只回复 SANDBOX_READY，勿调用工具。", mention=True)
                ai = self.response_after(event, timeout=150)
                assert "SANDBOX_READY" in json.dumps(ai)
                event = self.send("请只回复 SANDBOX_QUOTED，勿调用工具。", sender="90002", reply=ai["message_id"])
                ai = self.response_after(event, timeout=150)
                assert "SANDBOX_QUOTED" in json.dumps(ai)
                event = self.send("请只回复 SANDBOX_PRIVATE，勿调用工具。", private=True)
                ai = self.response_after(event, timeout=150)
                assert "SANDBOX_PRIVATE" in json.dumps(ai)
                event = self.send("请只回复 SANDBOX_NEW_MEMBER，勿调用工具。", sender="90003", private=True)
                assert "SANDBOX_NEW_MEMBER" in json.dumps(self.response_after(event, timeout=150))
                self.check("real DSH group mention, second member reply and private turns")
            self.http("/api/connect", {"connected": False})
            self.wait(lambda: not self.http("/api/state")["connected"], "manual disconnect")
            try:
                self.http("/api/send", {"conversation": {"type": "group", "id": "91000"}, "sender_id": "90001",
                                        "message": [{"type": "text", "data": {"text": "offline fixture"}}]})
            except HTTPError as exc:
                assert exc.code == 503
            else:
                raise AssertionError("disconnected input unexpectedly accepted")
            self.http("/api/connect", {"connected": True})
            self.wait(lambda: self.http("/api/state")["connected"], "reconnect")
            self.response_after(self.send("/v2probe"))
            self.check("disconnect rejects new delivery and reconnect restores send")
            if timeout_case:
                self.send("/v2probe", faults={"timeout": True})
                log_path = self.directory / "runtime.log"
                self.wait(lambda: "TimeoutError" in log_path.read_text(errors="replace")
                          or "ApiTimeout" in log_path.read_text(errors="replace"), "real 180s adapter API deadline", timeout=200)
                self.check("real pinned adapter API timeout reached")
            saved_ids = [e["message_id"] for e in self.events()]
            journal_count = len(self.rows())
            sessions = self.session_ids() if self.live else {}
            self.stop()
            self.start()
            assert [e["message_id"] for e in self.events()] == saved_ids
            assert len(self.rows()) == journal_count
            if self.live:
                assert self.session_ids() == sessions
                event = self.send("请只回复 SANDBOX_RESUMED，勿调用工具。", mention=True)
                assert "SANDBOX_RESUMED" in json.dumps(self.response_after(event, timeout=150))
                assert self.session_ids()["group:91000"] == sessions["group:91000"]
            else:
                self.response_after(self.send("/v2probe"))
            self.check("restart retains events, Journal and resumes same DSH session" if self.live else
                       "restart retains events and Journal and accepts new input")
            return {"passed": True, "live_dsh": self.live, "checks": self.checks,
                    "real_qq_connections": 0, "data_dir": str(self.directory)}
        finally:
            self.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true", help="consume the explicitly selected provider/model")
    parser.add_argument("--timeout-case", action="store_true", help="also wait for the pinned 180-second API deadline")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.live_model and not all(os.environ.get(k) for k in ("V2_DSH_PROVIDER", "V2_DSH_MODEL", "DEEPSEEK_API_KEY")):
        parser.error("live test requires V2_DSH_PROVIDER, V2_DSH_MODEL and DEEPSEEK_API_KEY")
    directory = args.data_dir or ROOT / ".runtime" / ("sandbox-check-" + uuid.uuid4().hex[:12])
    started = time.monotonic()
    result = Smoke(directory.resolve(), args.live_model).run(args.timeout_case)
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
