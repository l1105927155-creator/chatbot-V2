"""Real pinned ACP owner turn executes one native Bash tool under bwrap."""
from __future__ import annotations
import os
import signal
import subprocess
import pytest
from pathlib import Path
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


@pytest.mark.parametrize("mode", [
    "success", "timeout", "cancel", "resume", "runner_failure", "limits",
    "disk_full", "volume_unavailable",
])
def test_owner_acp_bash_is_confined(mode):
    worker = r'''
import json, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from contextlib import ExitStack
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
sys.path.insert(0, str(root / "dsh/plugins/agent-profile"))
from owner_volume import prepare, verified_workspace, unmount, manifest_path
from turn_source import TurnSourcePublisher
from scene_state import SceneStateStore
class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
def request(client, method, params):
    return client.request(method, params, response_model=ObjectResponse).model_dump()
with TemporaryDirectory(prefix="owner-profile-acp-", dir=root / ".runtime") as tmp, ExitStack() as cleanup:
    base = Path(tmp)
    home, workspace = base / "home", base / "workspace"
    home.mkdir(mode=0o700)
    prepare(workspace, base / "workspace.img", 32 * 1024 * 1024)
    cleanup.callback(unmount, workspace)
    volume_fd = verified_workspace(workspace)
    os.close(volume_fd)
    (workspace / "subdir").mkdir()
    publisher = TurnSourcePublisher(home)
    cleanup.callback(publisher.close)
    persona, scene = home / "agent-bot-personas", home / "agent-bot-scenes"
    persona.mkdir(mode=0o700)
    scene.mkdir(mode=0o700)
    trace = base / "trace.jsonl"
    patch = base / "probe.patch.yml"
    patch.write_text("- insert:\n    - id: owner-probe\n      name: "
                     + str(root / "tests/native/agent-profile-owner-probe.mjs") + "\n")
    mode = os.environ["V2_OWNER_TEST_MODE"]
    timeout_probe = mode == "timeout"
    if timeout_probe:
        patch.write_text(patch.read_text() + "- id: bash-sandbox\n  config:\n    timeoutMs: 500\n")
    env = {
        "LC_MESSAGES":"C",
        "DSH_SYSTEM_PROMPT":(root / "dsh/profile/agent-owner.md").read_text(),
        "V2_DSH_PROVIDER":"owner-bash-acceptance",
        "V2_DSH_MODEL":"scripted",
        "V2_AGENT_SKILLS_DIR":str(root / "dsh/skills"),
        "V2_PERSONA_STATE_ROOT":str(persona),
        "V2_SCENE_STATE_ROOT":str(scene),
        "V2_TURN_SOURCE_DIR":str(publisher.directory),
        "V2_TURN_SOURCE_EPOCH":publisher.epoch,
        "V2_QQ_OWNERS":json.dumps([{"platform_id":"qq","bot_id":"bot","user_id":"owner"}]),
        "V2_OWNER_PROBE_OUTPUT":str(trace),
    }
    if timeout_probe:
        env["V2_OWNER_PROBE_COMMAND"] = "(sleep 2; printf orphan > orphan.txt) & sleep 5"
    if mode == "cancel":
        env["V2_OWNER_PROBE_COMMAND"] = "printf ready > started.txt; (sleep 2; printf orphan > orphan.txt) & sleep 10"
    if mode == "runner_failure":
        env["V2_OWNER_FAIL_RUNNER"] = "1"
    if mode == "limits":
        env["V2_OWNER_PROBE_COMMAND"] = "cat /proc/self/cgroup > cgroup.txt; sleep 2; pwd"
    if mode == "disk_full":
        env["V2_OWNER_PROBE_COMMAND"] = (
            "python3 -c 'import os\n"
            "f=open(\"fill.bin\",\"wb\",buffering=0)\n"
            "data=b\"x\"*(1024*1024)\n"
            "try:\n while True: f.write(data)\n"
            "except OSError as e:\n print(e, flush=True)\n f.close()\n"
            " os.unlink(\"fill.bin\")\n"
            " open(\"recovered.txt\",\"wb\").write(b\"ok\")\n"
            " raise SystemExit(1)'"
        )
    settings = HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
        dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal",
        patches=(str(root / "dsh/profile/v2-agent-owner.patch.yml"),str(patch)),
        env=env, request_timeout_seconds=45,
    )
    client = HarnessClient(settings)
    client.start()
    cleanup.callback(client.close)
    try:
        request(client,"initialize",{
            "protocolVersion":1,"clientCapabilities":{},
            "clientInfo":{"name":"owner-bash-acceptance","version":"0.1.0"},
        })
        sid = request(client,"session/new",{"cwd":str(workspace),"mcpServers":[]})["sessionId"]
        SceneStateStore(scene).select(sid, "development")
        manifest = manifest_path(workspace)
        manifest_backup = Path(str(manifest) + ".test-backup")
        if mode == "volume_unavailable":
            def restore_manifest():
                if manifest_backup.exists():
                    os.replace(manifest_backup, manifest)
            os.replace(manifest, manifest_backup)
            cleanup.callback(restore_manifest)
            cleanup.callback(client.close)
        if mode == "disk_full":
            (workspace / "subdir/sentinel.txt").write_text("preserve-me")
        prompt = "owner current journal delta"
        nonce = publisher.publish(sid,{
            "platform_id":"qq","bot_id":"bot","conversation_key":"private:owner",
            "requester_id":"owner","inbound_message_id":"current",
        },prompt)
        params = {"sessionId":sid,"prompt":[{"type":"text","text":prompt}]}
        if mode in ("cancel", "limits"):
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(request, client, "session/prompt", params)
                deadline = time.monotonic() + 8
                ready_file = workspace/"subdir"/("started.txt" if mode == "cancel" else "cgroup.txt")
                while not ready_file.exists() or not ready_file.read_text():
                    assert not pending.done(), pending.result()
                    assert time.monotonic() < deadline, "Bash never started"
                    time.sleep(0.02)
                if mode == "cancel":
                    client.notify("session/cancel", {"sessionId":sid})
                else:
                    group = ready_file.read_text().strip().split("::",1)[1]
                    assert group.startswith("/user.slice/"), group
                    control = Path("/sys/fs/cgroup") / group.lstrip("/")
                    assert (control/"memory.max").read_text().strip() == "536870912"
                    assert (control/"pids.max").read_text().strip() == "64"
                    quota, period = (control/"cpu.max").read_text().split()
                    assert int(quota) == int(period)
                result = pending.result(timeout=8)
            assert result["stopReason"] == ("cancelled" if mode == "cancel" else "end_turn"), result
        else:
            result = request(client,"session/prompt",params)
            assert result["stopReason"] == "end_turn", result
        assert publisher.cleanup(sid,nonce)
        request(client,"session/close",{"sessionId":sid})
        calls=[json.loads(x) for x in trace.read_text().splitlines()]
        assert len(calls)==(1 if mode == "cancel" else 2),calls
        assert calls[0]["runtime"]["sandbox"] == "OwnerSandbox", calls[0]["runtime"]
        assert calls[0]["tools"] == ["bash", "skill", "web_fetch", "web_search"], calls[0]["tools"]
        assert "trusted owner" in json.dumps(calls[0]["messages"])
        assert "Inspect and modify files" in json.dumps(calls[0]["messages"])
        assert "Develop and validate a task" in json.dumps(calls[0]["messages"])
        observed=json.dumps(calls[-1]["messages"])
        if mode in ("timeout", "cancel"):
            time.sleep(3)
            assert not (workspace/"subdir/orphan.txt").exists(), "timed-out descendant survived"
            if timeout_probe:
                assert "timed out after 500ms" in observed.lower(), observed
        elif mode == "runner_failure":
            assert not (workspace/"subdir/result.txt").exists(), "runner failure executed unconfined command"
            assert "SANDBOX_UNAVAILABLE" in observed or "nonexistent-owner-acceptance-runner" in observed, observed
        elif mode == "limits":
            assert "/workspace/subdir" in observed, observed
        elif mode == "disk_full":
            assert "no space left on device" in observed.lower(), observed
            assert "[exit code: 1]" in observed.lower(), observed
            assert (workspace/"subdir/sentinel.txt").read_text() == "preserve-me"
            assert (workspace/"subdir/recovered.txt").read_text() == "ok"
            assert not (workspace/"subdir/fill.bin").exists()
        elif mode == "volume_unavailable":
            assert not (workspace/"subdir/result.txt").exists(), "missing volume manifest fell back to host workspace"
            assert "volume" in observed.lower() or "containment unavailable" in observed.lower(), observed
        else:
            assert (workspace/"subdir/result.txt").read_text()=="native-owner-sandbox"
            assert "/workspace/subdir" in observed,observed
        if mode == "resume":
            client.close()
            (workspace/"subdir/result.txt").write_text("keep-sentinel")
            client = HarnessClient(settings)
            client.start()
            cleanup.callback(client.close)
            request(client,"initialize",{
                "protocolVersion":1,"clientCapabilities":{},
                "clientInfo":{"name":"owner-resume-acceptance","version":"0.1.0"},
            })
            request(client,"session/resume",{"sessionId":sid,"cwd":str(workspace),"mcpServers":[]})
            request(client,"session/prompt",{
                "sessionId":sid,"prompt":[{"type":"text","text":"no current authority after restart"}],
            })
            calls=[json.loads(x) for x in trace.read_text().splitlines()]
            assert len(calls)==4,calls
            assert "authorization denied" in json.dumps(calls[-1]["messages"]), {"runtime":[c.get("runtime") for c in calls],"result":calls[-1]["messages"][-1]}
            assert (workspace/"subdir/result.txt").read_text()=="keep-sentinel"
            fresh_prompt = "new verified owner request after restart"
            current = publisher.publish(sid,{
                "platform_id":"qq","bot_id":"bot","conversation_key":"private:owner",
                "requester_id":"owner","inbound_message_id":"fresh",
            },fresh_prompt)
            request(client,"session/prompt",{
                "sessionId":sid,"prompt":[{"type":"text","text":fresh_prompt}],
            })
            assert publisher.cleanup(sid,current)
            assert (workspace/"subdir/result.txt").read_text()=="native-owner-sandbox"
            (workspace/"subdir/result.txt").write_text("nonowner-sentinel")
            denied_prompt = "different sender cannot inherit prior owner"
            outsider = publisher.publish(sid,{
                "platform_id":"qq","bot_id":"bot","conversation_key":"private:other",
                "requester_id":"other","inbound_message_id":"outsider",
            },denied_prompt)
            request(client,"session/prompt",{
                "sessionId":sid,"prompt":[{"type":"text","text":denied_prompt}],
            })
            assert publisher.cleanup(sid,outsider)
            calls=[json.loads(x) for x in trace.read_text().splitlines()]
            assert len(calls)==8,calls
            assert all(c["runtime"]["sandbox"] == "OwnerSandbox" for c in calls)
            assert "authorization denied" in json.dumps(calls[-1]["messages"])
            assert (workspace/"subdir/result.txt").read_text()=="nonowner-sentinel"
            request(client,"session/close",{"sessionId":sid})
        print(json.dumps({"real_acp_owner_bash":True,"model_calls":len(calls),"mode":mode}))
    except BaseException:
        print("\n".join(client._stderr_lines),file=sys.stderr)
        raise
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "V2_OWNER_TEST_MODE": mode}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
        raise AssertionError(f"owner ACP Bash exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"real_acp_owner_bash": true' in stdout
