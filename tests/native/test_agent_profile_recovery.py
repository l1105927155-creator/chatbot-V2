"""Exercise explicit recovery after a real cancelled native ACP turn."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


@pytest.mark.parametrize("case", ["group_restart", "group_loaded", "owner_restart"])
def test_pending_recovery_after_native_cancel(case):
    worker = r'''
import asyncio, json, os, sys, time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient as OriginalHarnessClient
root = Path(os.environ["V2_REPO_ROOT"])
case = os.environ["V2_RECOVERY_CASE"]
owner = case.startswith("owner")
sys.path.insert(0, str(root / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(root / "astrbot-plugins/v2_journal"))
sys.path.insert(0, str(root / "dsh/plugins/agent-profile"))
from dsh_client import DshClient, DshTurnCancelled
from service import ChatService
from journal import JournalStore, Conversation, message_from_onebot
from state import StateStore
from owner_volume import prepare, unmount

# Retain the production DshClient/AcpRuntime and profile. Append only a native
# deterministic model adapter and its private trace path for this acceptance.
class ControlledHarnessClient(OriginalHarnessClient):
    def __init__(self, config):
        config = replace(config,
            patches=(*config.patches, os.environ["V2_RECOVERY_PATCH"]),
            env={**(config.env or {}), "V2_RECOVERY_TRACE":os.environ["V2_RECOVERY_TRACE"]},
        )
        super().__init__(config)
import deepseek_harness.client
deepseek_harness.client.HarnessClient = ControlledHarnessClient

def append(journal, conversation, text, message_id):
    group = conversation.key.startswith("group:")
    journal.append(message_from_onebot("qq", {
        "post_type":"message", "message_type":"group" if group else "private",
        "self_id":"bot", "user_id":"owner", "group_id":"1055300305" if group else None,
        "message_id":message_id,
        "message":[{"type":"text","data":{"text":text}}],
    }))

async def scenario(base):
    home, workspace = base / "home", base / "workspace"
    home.mkdir(mode=0o700)
    if owner:
        prepare(workspace, base / "workspace.img", 32 * 1024 * 1024)
        os.environ["V2_QQ_OWNERS"] = json.dumps([{"platform_id":"qq","bot_id":"bot","user_id":"owner"}])
        profile = root / "dsh/profile/v2-agent-owner.patch.yml"
        instructions = root / "dsh/profile/agent-owner.md"
        key = "private:owner"
    else:
        workspace.mkdir(mode=0o700)
        profile = root / "dsh/profile/v2-agent-group.patch.yml"
        instructions = root / "dsh/profile/agent-group.md"
        key = "group:1055300305"
    patch = base / "recovery-probe.patch.yml"
    patch.write_text("- insert:\n    - id: recovery-probe\n      name: "
                     + str(root / "tests/native/agent-profile-recovery-probe.mjs") + "\n")
    os.environ["V2_RECOVERY_PATCH"] = str(patch)
    trace = base / "model-calls.jsonl"
    os.environ["V2_RECOVERY_TRACE"] = str(trace)
    workspace_ready = workspace / "subdir/started.txt"
    if owner:
        (workspace / "subdir").mkdir()
    identity = ("qq", "bot", key)
    conversation = Conversation(*identity)
    journal = JournalStore(base / "journal.sqlite3")
    state = StateStore(base / "state.sqlite3")
    append(journal, conversation, "cancelled accepted turn needs review", "old-turn")
    first = second = None
    first_runtime_stopped = False
    try:
        first = DshClient(
            dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
            dsh_home=home, workspace=workspace, patch=profile, instructions=instructions,
            provider="recovery-acceptance", model="scripted",
            enable_authorization=owner,
        )
        service = ChatService(journal, state, first, trusted_sources=owner,
                              domain="owner" if owner else "chat")
        sends = []
        async def send(value): sends.append(value)
        turn = asyncio.create_task(service.handle(conversation, "old-turn", send))
        deadline = time.monotonic() + 15
        if owner:
            while not await asyncio.to_thread(workspace_ready.exists):
                assert not turn.done(), "Owner turn ended before Bash started"
                assert time.monotonic() < deadline, "Owner Bash readiness was not observed"
                await asyncio.sleep(.02)
        else:
            while not trace.exists() or trace.read_text().count("\n") < 1:
                assert not turn.done(), "group turn ended before native model stream started"
                assert time.monotonic() < deadline, "group model stream readiness was not observed"
                await asyncio.sleep(.02)
        assert await service.cancel(conversation) is True
        try:
            await asyncio.wait_for(turn, timeout=15)
            raise AssertionError("cancelled turn unexpectedly completed")
        except DshTurnCancelled:
            pass
        if owner:
            await asyncio.sleep(3)
            assert not (workspace / "subdir/orphan.txt").exists(), "Owner cancellation left a child process"
            assert first.turn_sources._active == {}, "Owner cancellation retained the trusted turn nonce"
            assert not first._sessions, "Owner cancellation did not drain the native session"
        pending = state.get(*identity)
        assert pending is not None and pending.pending_upper_cursor == 1, pending
        assert pending.last_seen_journal_id == 0
        assert sends == [], sends
        assert len(trace.read_text().splitlines()) == 1, trace.read_text()
        sid = pending.session_id

        # A later wake arrives while the previous result awaits review. It must
        # remain queued and be delivered after the exact pending cursor is skipped.
        append(journal, conversation, "new wake queued during review", "new-turn")
        if case.endswith("restart"):
            first.runtime.close()
            first_runtime_stopped = True
            second = DshClient(
                dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
                dsh_home=home, workspace=workspace, patch=profile, instructions=instructions,
                provider="recovery-acceptance", model="scripted", enable_authorization=owner,
            )
            service = ChatService(journal, StateStore(state.path), second, trusted_sources=owner,
                                  domain="owner" if owner else "chat")
        before_recovery_calls = len(trace.read_text().splitlines())
        resolved = await service.resolve_pending(conversation, 1)
        assert resolved.session_id == sid
        assert resolved.last_seen_journal_id == 1 and resolved.pending_upper_cursor is None
        assert len(trace.read_text().splitlines()) == before_recovery_calls, "recovery called the model"
        assert sends == [], "recovery sent a success message"
        try:
            await service.resolve_pending(conversation, 1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("stale/repeated pending cursor was accepted")

        answer = await service.handle(conversation, "new-turn", send)
        assert answer == "Fresh turn completed after explicit recovery."
        assert sends == [answer]
        final = state.get(*identity)
        assert final.session_id == sid and final.last_seen_journal_id == 2
        assert final.pending_upper_cursor is None
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        assert len(calls) == 2, calls
        latest_delta = json.dumps(calls[-1]["latestJournalDelta"], ensure_ascii=False)
        assert "new wake queued during review" in latest_delta, latest_delta
        assert "cancelled accepted turn needs review" not in latest_delta, latest_delta
        # Both inbound journal rows remain canonical despite the explicit skip.
        assert len(journal.after(conversation, 0, 10)) == 2
        print(json.dumps({"case":case,"same_session":True,"model_calls_before_recovery":1,
                          "model_calls_during_recovery":0,"model_calls_for_new_wake":1,
                          "cursor":final.last_seen_journal_id}))
    finally:
        if second is not None:
            second.close()
        if first is not None:
            if first_runtime_stopped:
                if first.turn_sources is not None:
                    first.turn_sources.close()
            else:
                first.close()

with TemporaryDirectory(prefix="agent-pending-recovery-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    try:
        asyncio.run(scenario(base))
    finally:
        if owner:
            unmount(base / "workspace")
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "V2_RECOVERY_CASE": case}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"{case} native recovery exceeded 120 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"model_calls_during_recovery": 0' in stdout, stdout
