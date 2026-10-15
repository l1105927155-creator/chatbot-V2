#!/usr/bin/env python3
"""Opt-in real-provider acceptance for the safe Agent Profile, without QQ."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins" / "v2_dsh_router"))
from dsh_client import DshClient
sys.path.insert(0, str(ROOT / "dsh/plugins/agent-profile"))
from owner_volume import prepare, unmount


def run(provider: str | None, model: str | None) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agent-profile-live-", dir=ROOT / ".runtime") as directory:
        base = Path(directory)
        settings = dict(
            dsh_bin=ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh",
            dsh_home=base / "home",
            workspace=base / "workspace",
            patch=ROOT / "dsh/profile/v2-agent-group.patch.yml",
            instructions=ROOT / "dsh/profile/agent-group.md",
            provider=provider,
            model=model,
        )
        client = DshClient(**settings)
        try:
            sid = client.create_session()
            first = client.run(sid, "Synthetic acceptance message. Reply briefly with READY.")
            assert "READY" in first, "provider did not follow the simple acceptance request"
            selected = client.select_persona(sid, "analyst")
            assert selected["persona"] == "analyst"
            observed = []
            with client.runtime.client.subscribe_session_notifications(sid) as subscription:
                second = client.run(
                    sid,
                    "Synthetic acceptance message. Load the chat-history skill with the skill tool, "
                    "then briefly explain its purpose. No QQ or other external action is requested.",
                )
                subscription.drain(lambda notification: observed.append(notification.payload.get("update", {})))
            assert second.strip(), "provider returned an empty second turn"
            skill_calls = {update["toolCallId"] for update in observed
                           if update.get("sessionUpdate") == "tool_call"
                           and update.get("title") == "skill"
                           and update.get("rawInput", {}).get("name") == "chat-history"}
            assert any(update.get("toolCallId") in skill_calls and update.get("status") == "completed"
                       for update in observed if update.get("sessionUpdate") == "tool_call_update"), (
                "provider did not complete the requested native skill load"
            )
            usage = [update for update in observed if update.get("sessionUpdate") == "usage_update"]
            client.close_session(sid)
        finally:
            client.close()

        resumed = DshClient(**settings)
        try:
            assert resumed.personas.current(sid)["persona"] == "analyst"
            third = resumed.run(sid, "Synthetic acceptance after restart. Reply briefly with RESUMED.")
            assert "RESUMED" in third, "provider did not complete the resumed acceptance turn"
            resumed.close_session(sid)
            return {
                "provider": resumed.settings["provider"],
                "model": resumed.settings["model"],
                "persistent_session_resumed": True,
                "persona_state_persisted": True,
                "completed_user_turns": 3,
                "native_skill_loaded": True,
                "advertised_context_window": usage[-1]["size"] if usage else None,
                "qq_connections": 0,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "limitation": "Synthetic safe-chat provider check; not real QQ, owner execution, or context-budget acceptance.",
            }
        finally:
            resumed.close()


def run_owner(provider: str | None, model: str | None) -> dict:
    started = time.monotonic()
    identity = {"platform_id": "synthetic-qq", "bot_id": "synthetic-bot",
                "conversation_key": "private:synthetic-owner", "requester_id": "synthetic-owner"}
    # This process and workspace are isolated from the router and its owners.
    os.environ["V2_QQ_OWNERS"] = json.dumps([{
        "platform_id": identity["platform_id"], "bot_id": identity["bot_id"],
        "user_id": identity["requester_id"],
    }])
    with tempfile.TemporaryDirectory(prefix="agent-owner-live-", dir=ROOT / ".runtime") as directory, ExitStack() as cleanup:
        base = Path(directory)
        prepare(base / "workspace", base / "workspace.ext4", 32 * 1024 ** 2)
        cleanup.callback(unmount, base / "workspace")
        settings = dict(
            dsh_bin=ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh",
            dsh_home=base / "home", workspace=base / "workspace",
            patch=ROOT / "dsh/profile/v2-agent-owner.patch.yml",
            instructions=ROOT / "dsh/profile/agent-owner.md",
            provider=provider, model=model, enable_authorization=True,
        )
        def turn(client, sid, prompt, message_id):
            observed = []
            with client.runtime.client.subscribe_session_notifications(sid) as subscription:
                answer = client.run(sid, prompt, trusted_source={
                    **identity, "inbound_message_id": message_id,
                })
                subscription.drain(lambda notification: observed.append(notification.payload.get("update", {})))
            calls = {update["toolCallId"] for update in observed
                     if update.get("sessionUpdate") == "tool_call" and update.get("title") == "bash"}
            assert any(update.get("toolCallId") in calls and update.get("status") == "completed"
                       for update in observed if update.get("sessionUpdate") == "tool_call_update"), (
                "provider did not complete the requested confined Bash action"
            )
            return answer

        client = DshClient(**settings)
        try:
            sid = client.create_session()
            client.select_persona(sid, "developer")
            turn(client, sid,
                 "Synthetic owner acceptance. Use Bash to run exactly: printf OWNER_READY > marker.txt . "
                 "The command ends at marker.txt; the following period is punctuation, not a command argument. "
                 "Use the default workspace and then briefly report success.", "create")
            assert (base / "workspace/marker.txt").read_text() == "OWNER_READY"
            client.close_session(sid)
        finally:
            client.close()
        resumed = DshClient(**settings)
        try:
            answer = turn(resumed, sid,
                          "Synthetic owner acceptance after restart. Use Bash to cat marker.txt in the "
                          "default workspace, then reply with its exact content.", "read-after-restart")
            assert "OWNER_READY" in answer
            assert resumed.personas.current(sid)["persona"] == "developer"
            resumed.close_session(sid)
            return {
                "provider": resumed.settings["provider"], "model": resumed.settings["model"],
                "domain": "owner", "persistent_session_resumed": True,
                "persona_state_persisted": True, "confined_bash_completed": True,
                "completed_user_turns": 2, "qq_connections": 0,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "limitation": "Synthetic isolated owner-provider check; not real QQ or a system administration acceptance.",
            }
        finally:
            resumed.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=("chat", "owner"), default="chat")
    parser.add_argument("--provider", default=os.environ.get("V2_DSH_PROVIDER"))
    parser.add_argument("--model", default=os.environ.get("V2_DSH_MODEL"))
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/agent-profile-live-result.json")
    args = parser.parse_args()
    try:
        result = (run_owner if args.domain == "owner" else run)(args.provider, args.model)
    except Exception as error:
        # Runtime exceptions can include provider diagnostics. Keep credentials
        # and transport details out of this command's public output.
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        return 1
    result["passed"] = True
    if args.domain == "owner" and args.output == ROOT / ".runtime/agent-profile-live-result.json":
        args.output = ROOT / ".runtime/agent-owner-live-result.json"
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
