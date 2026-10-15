"""Exercise Owner cancellation through ChatService and the pinned ACP runtime."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_owner_service_cancel_drains_native_acp_turn():
    worker = r'''
import asyncio, json, os, sys, time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient as OriginalHarnessClient
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(root / "astrbot-plugins/v2_journal"))
sys.path.insert(0, str(root / "dsh/plugins/agent-profile"))
from dsh_client import DshClient, DshTurnCancelled
from service import ChatService
from runtime_audit import AuditStore
from journal import JournalStore, Conversation, message_from_onebot
from state import StateStore
from owner_volume import prepare, unmount

# Preserve the production DshClient/AcpRuntime path; add only a deterministic
# native LLM adapter to the pinned profile for this isolated acceptance.
class ControlledHarnessClient(OriginalHarnessClient):
    def __init__(self, config):
        config = replace(config, patches=(*config.patches, os.environ["V2_CANCEL_PROBE_PATCH"]))
        super().__init__(config)
import deepseek_harness.client
deepseek_harness.client.HarnessClient = ControlledHarnessClient

async def scenario(base):
    home, workspace = base / "home", base / "workspace"
    home.mkdir(mode=0o700)
    patch = base / "cancel-probe.patch.yml"
    patch.write_text("- insert:\n    - id: cancel-probe\n      name: "
                     + str(root / "tests/native/agent-profile-cancel-probe.mjs") + "\n")
    os.environ["V2_CANCEL_PROBE_PATCH"] = str(patch)
    trace = base / "adapter.jsonl"
    os.environ["V2_CANCEL_PROBE_TRACE"] = str(trace)
    prepare(workspace, base / "workspace.img", 32 * 1024 * 1024)
    (workspace / "subdir").mkdir()
    os.environ["V2_QQ_OWNERS"] = json.dumps([{"platform_id":"qq","bot_id":"bot","user_id":"owner"}])
    ready = workspace / "subdir/started.txt"
    os.environ["V2_CANCEL_PROBE_READY"] = str(ready)
    dsh = DshClient(
        dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
        dsh_home=home, workspace=workspace,
        patch=root / "dsh/profile/v2-agent-owner.patch.yml",
        instructions=root / "dsh/profile/agent-owner.md",
        provider="owner-cancel-acceptance", model="scripted",
        enable_authorization=True,
    )
    journal = JournalStore(base / "journal.sqlite3")
    state = StateStore(base / "state.sqlite3")
    audit = AuditStore(base / "audit")
    service = ChatService(journal, state, dsh, trusted_sources=True, audit=audit)
    conversation = Conversation("qq", "bot", "private:owner")
    journal.append(message_from_onebot("qq", {
        "post_type":"message", "message_type":"private", "self_id":"bot",
        "user_id":"owner", "message_id":"cancel-me",
        "message":[{"type":"text","data":{"text":"run the long task"}}],
    }))
    sent = []
    async def send(value): sent.append(value)
    pending = None
    try:
        pending = asyncio.create_task(service.handle(conversation, "cancel-me", send))
        deadline = time.monotonic() + 12
        while not await asyncio.to_thread(ready.exists):
            assert not pending.done(), pending.result() if pending.done() else "turn ended before native Bash started"
            assert time.monotonic() < deadline, "native Bash never started"
            await asyncio.sleep(.02)
        started = time.monotonic()
        assert await service.cancel(conversation) is True
        assert time.monotonic() - started < 2, "cancel waited for the conversation lock"
        try:
            await asyncio.wait_for(pending, timeout=12)
            raise AssertionError("cancelled turn unexpectedly completed")
        except DshTurnCancelled:
            pass
        await asyncio.sleep(3)
        assert not (workspace / "subdir/orphan.txt").exists(), "cancel left a descendant process"
        mapped = state.get("qq", "bot", "private:owner")
        assert mapped is not None and mapped.pending_upper_cursor == 1, mapped
        assert mapped.last_seen_journal_id == 0
        assert sent == [], sent
        assert dsh.turn_sources._active == {}, "trusted turn nonce was not cleaned"
        assert mapped.session_id not in dsh._quarantined_sessions
        assert mapped.session_id not in dsh._sessions, "cancelled native session was not drained"
        events = list(audit.rows())
        assert any(row['event']=='tool/start' and row.get('tool')=='bash' for row in events)
        assert any(row['event']=='dsh/stop' and row.get('stop_reason')=='cancelled' for row in events)
        assert any(row['event']=='turn/failed' and row.get('error_type')=='DshTurnCancelled' for row in events)
        assert audit.snapshot('owner')['events']['dsh/dispatched']==1
        assert len({row['turn_id'] for row in events})==1
        assert all(row.get('session_id')==mapped.session_id for row in events if row['event'] not in ('turn/queued','turn/start'))
        assert 'printf ready' not in json.dumps(events)
        assert await service.cancel(conversation) is False
        # The exact native session remains resumable after the confirmed drain.
        dsh.runtime.resume(mapped.session_id)
        dsh.runtime.close_session(mapped.session_id)
        print(json.dumps({"service_cancel":True,"native_cancel":True,"pending_preserved":True,
                          "send_count":len(sent),"resumed_same_session":True}))
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except BaseException:
                pass
        dsh.close()

with TemporaryDirectory(prefix="owner-service-cancel-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    try:
        asyncio.run(scenario(base))
    finally:
        unmount(base / "workspace")
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(f"Owner service cancellation exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"service_cancel": true' in stdout, stdout
