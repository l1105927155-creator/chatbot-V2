"""Pinned DSH ACP -> native Bash -> group cgroup/bwrap and current-source guards."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime/astrbot/.venv/bin/python"


def test_group_profile_executes_real_native_bash_inside_verified_group_scope(tmp_path):
    worker = r'''
import json, os, signal, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(root / "dsh/plugins/agent-profile"))
from turn_source import TurnSourcePublisher
from group_volume import prepare_group, unmount_group
from sys import version_info
class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
def request(client, method, params):
    return client.request(method, params, response_model=ObjectResponse).model_dump()
base = Path(os.environ["V2_GROUP_TEST_ROOT"])
identity = "group-acceptance-" + str(os.getpid())
volume = prepare_group(base, "group-test-platform", "group-test-bot", identity)
workspace = Path(volume["workspace"])
home = base / ("home-" + volume["key"])
home.mkdir(mode=0o700)
spill_root = home / "native-spill-tmp"
spill_root.mkdir(mode=0o700)
site_packages = home / "payload" / "lib" / f"python{version_info.major}.{version_info.minor}" / "site-packages"
site_packages.mkdir(parents=True, mode=0o700)
host_sitecustomize_marker = base / "host-sitecustomize-executed"
(site_packages / "sitecustomize.py").write_text(
  "from pathlib import Path; Path(" + repr(str(host_sitecustomize_marker)) + ").write_text('executed')\n")
publisher = TurnSourcePublisher(home)
persona, scenes = home / "agent-bot-personas", home / "agent-bot-scenes"
persona.mkdir(mode=0o700); scenes.mkdir(mode=0o700)
log, command_file = base / "model-log.jsonl", base / "command.json"
probe_patch = base / "group-probe.patch.yml"
probe_patch.write_text("- insert:\n    - id: agent-profile-group-probe\n      name: "
                     + str(root / "tests/native/agent-profile-group-probe.mjs") + "\n")
timeout_patch = base / "group-timeout.patch.yml"
timeout_patch.write_text("- id: bash-sandbox\n  config:\n    timeoutMs: 60000\n    maxTimeoutMs: 60000\n")
env = {
  "LC_MESSAGES":"C", "DSH_SYSTEM_PROMPT":(root / "dsh/profile/agent-group-sandbox.md").read_text(),
  "V2_DSH_PROVIDER":"group-bash-acceptance", "V2_DSH_MODEL":"scripted",
  "V2_AGENT_SKILLS_DIR":str(root / "dsh/skills"),
  "V2_PERSONA_STATE_ROOT":str(persona), "V2_SCENE_STATE_ROOT":str(scenes),
  "V2_TURN_SOURCE_DIR":str(publisher.directory), "V2_TURN_SOURCE_EPOCH":publisher.epoch,
  "V2_GROUP_WORKSPACE_ROOT":str(workspace), "V2_GROUP_PLATFORM_ID":"group-test-platform",
  "V2_GROUP_BOT_ID":"group-test-bot", "V2_GROUP_ID":identity,
  "V2_GROUP_PROBE_LOG":str(log), "V2_GROUP_COMMAND":str(command_file),
  "TMPDIR":str(spill_root),
  "PYTHONUSERBASE":str(site_packages.parents[2]),
  "GROUP_SECRET_SENTINEL":"must-be-cleared",
}
settings = HarnessConfig(
  dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"), dsh_home=str(home),
  cwd=str(home), profile="sdk-minimal",
  patches=(str(root / "dsh/profile/v2-agent-group-sandbox.patch.yml"), str(probe_patch), str(timeout_patch)),
  env=env, request_timeout_seconds=30,
)
client = HarnessClient(settings)
try:
  client.start()
  request(client, "initialize", {"protocolVersion":1,"clientCapabilities":{},
    "clientInfo":{"name":"group-bash-acceptance","version":"0.1.0"}})
  sid = request(client, "session/new", {"cwd":str(workspace),"mcpServers":[]})["sessionId"]
  def run(prompt, command, timeout=None, workdir=None):
    command_file.write_text(json.dumps({"command":command,
      **({} if timeout is None else {"timeoutMs":timeout}),
      **({} if workdir is None else {"workdir":workdir})}))
    nonce = publisher.publish(sid, {
      "platform_id":"group-test-platform", "bot_id":"group-test-bot",
      "conversation_key":"group:" + identity, "requester_id":"member-1",
      "inbound_message_id":"msg-" + prompt,
    }, prompt)
    try:
      result = request(client, "session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":prompt}]})
      assert result["stopReason"] == "end_turn", result
    finally:
      assert publisher.cleanup(sid, nonce)
  # Actual native tool metadata and a successful workspace operation.
  run("group success", "printf '%s\\n' \"$HOME\" > home.txt; pwd > cwd.txt; printf ok > marker.txt; "
      "test ! -e /etc/shadow && test -z \"${GROUP_SECRET_SENTINEL:-}\"")
  assert (workspace / "home.txt").read_text().strip() == "/workspace"
  assert (workspace / "cwd.txt").read_text().strip() == "/workspace"
  assert (workspace / "marker.txt").read_text() == "ok"
  assert not host_sitecustomize_marker.exists(), "untrusted Python sitecustomize ran before sandbox setup"
  # Inspect the actual live scope before the native subprocess provider reaps it.
  command_file.write_text(json.dumps({"command":"cat /proc/self/cgroup > cgroup.txt; sleep 2"}))
  cgroup_prompt = "group cgroup limits"
  cgroup_nonce = publisher.publish(sid, {
    "platform_id":"group-test-platform", "bot_id":"group-test-bot",
    "conversation_key":"group:" + identity, "requester_id":"member-1",
    "inbound_message_id":"msg-cgroup",
  }, cgroup_prompt)
  with ThreadPoolExecutor(1) as pool:
    pending = pool.submit(request, client, "session/prompt", {
      "sessionId":sid,"prompt":[{"type":"text","text":cgroup_prompt}],
    })
    deadline = time.monotonic() + 8
    while not (workspace / "cgroup.txt").exists():
      assert not pending.done(), pending.result()
      assert time.monotonic() < deadline, "native Bash did not publish its cgroup path"
      time.sleep(0.02)
    cgroup_lines = (workspace / "cgroup.txt").read_text().splitlines()
    assert any(line.startswith("0::") for line in cgroup_lines), cgroup_lines
    cgroup = next(line.split("::",1)[1] for line in cgroup_lines if line.startswith("0::"))
    control = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    assert control.name.startswith("dsh-subprocess-"), cgroup
    assert (control / "memory.max").read_text().strip() == "536870912"
    assert (control / "memory.swap.max").read_text().strip() == "0"
    assert (control / "pids.max").read_text().strip() == "64"
    quota, period = (control / "cpu.max").read_text().split()
    assert int(quota) == int(period) and int(period) > 0
    assert pending.result(timeout=8)["stopReason"] == "end_turn"
  assert publisher.cleanup(sid, cgroup_nonce)
  # Network namespace, hard process limit, and disk cap are exercised by the
  # actual native Bash process in the same per-invocation managed scope.
  run("group network", "/usr/bin/python3 -c 'import socket; s=socket.socket(); s.settimeout(1); "
      "r=s.connect_ex((\"1.1.1.1\",53)); open(\"net-result\",\"w\").write(str(r)); assert r != 0'")
  assert int((workspace / "net-result").read_text()) != 0
  pids_command = "/usr/bin/python3 - <<'PY'\nimport subprocess\nps=[]\nfailed=0\nfor i in range(100):\n try: ps.append(subprocess.Popen(['sleep','20']))\n except OSError: failed+=1; break\nopen('pid-result','w').write(str((len(ps),failed)))\nfor p in ps: p.kill()\nfor p in ps: p.wait()\nassert failed > 0\nPY"
  run("group pids", pids_command)
  pids_created, pids_failed = eval((workspace / "pid-result").read_text(), {"__builtins__":{}})
  assert pids_created < 64 and pids_failed > 0, (pids_created,pids_failed)
  run("group memory", "/usr/bin/python3 -c 'open(\"mem-start\",\"w\").write(\"yes\"); "
      "x=bytearray(700*1024*1024); open(\"mem-end\",\"w\").write(\"no\")'")
  assert (workspace / "mem-start").exists()
  assert not (workspace / "mem-end").exists(), "512 MiB memory cgroup permitted a 700 MiB allocation"
  # User-provided timeouts are clamped by pinned dsh-bash-sandbox; the short
  # requested deadline exercises native termination and descendant cleanup.
  run("group timeout", "printf started > timeout-start; (sleep 2; printf late > timeout-late) & sleep 5", timeout=500)
  time.sleep(2.2)
  assert not (workspace / "timeout-late").exists()
  # Normal Bash exit also ends the PID namespace, including background children.
  run("group normal descendants", "(sleep 1; printf late > normal-late) & exit 0")
  time.sleep(1.2)
  assert not (workspace / "normal-late").exists()
  # A model cannot widen the 60 s profile cap or escape workspace-write mode.
  run("group timeout ceiling", "printf denied > over-limit", timeout=60001)
  assert not (workspace / "over-limit").exists()
  (workspace / "escape-workdir").symlink_to("/usr/bin")
  run("group workdir symlink", "printf escaped > workdir-escaped", workdir="escape-workdir")
  assert not (workspace / "workdir-escaped").exists()
  # Full outputs remain in bounded tool-result tails and never reference host tmp spills.
  run("group output", "/usr/bin/python3 -c 'print(\"x\"*250000)'")
  calls = [json.loads(line) for line in log.read_text().splitlines()]
  assert calls and "bash" in calls[0]["tools"]
  assert not ({"read", "write", "edit", "glob", "grep"} & set(calls[0]["tools"]))
  serialized = json.dumps(calls, ensure_ascii=False)
  assert "spillPath" not in serialized and "/tmp/dsh-subprocess-" not in serialized, serialized[-3000:]
  assert "[output truncated; full output: (unavailable)]" in serialized, serialized[-5000:]
  assert not list(spill_root.rglob("*.log")), list(spill_root.rglob("*.log"))
  # Cancel a running native Bash turn and verify its descendant cannot perform a late write.
  command_file.write_text(json.dumps({"command":"printf started > cancel-start; "
      "(sleep 2; printf late > cancel-late) & sleep 10"}))
  cancel_prompt = "group cancel descendants"
  cancel_nonce = publisher.publish(sid, {
    "platform_id":"group-test-platform", "bot_id":"group-test-bot",
    "conversation_key":"group:" + identity, "requester_id":"member-2",
    "inbound_message_id":"msg-cancel",
  }, cancel_prompt)
  with ThreadPoolExecutor(1) as pool:
    pending = pool.submit(request, client, "session/prompt", {
      "sessionId":sid,"prompt":[{"type":"text","text":cancel_prompt}],
    })
    deadline = time.monotonic() + 8
    while not (workspace / "cancel-start").exists():
      assert not pending.done(), pending.result()
      assert time.monotonic() < deadline, "cancel Bash did not start"
      time.sleep(0.02)
    client.notify("session/cancel", {"sessionId":sid})
    cancelled = pending.result(timeout=10)
    assert cancelled["stopReason"] == "cancelled", cancelled
  assert publisher.cleanup(sid, cancel_nonce)
  time.sleep(2.2)
  assert not (workspace / "cancel-late").exists()
  # Actual token binding: a stale/missing source must be rejected by native tools.
  stale = publisher.publish(sid, {
    "platform_id":"group-test-platform", "bot_id":"group-test-bot",
    "conversation_key":"group:" + identity, "requester_id":"member-1",
    "inbound_message_id":"source-missing",
  }, "group stale source")
  publisher.cleanup(sid, stale)
  command_file.write_text(json.dumps({"command":"printf escaped > stale-escaped"}))
  denied = request(client, "session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":"group stale source"}]})
  assert denied["stopReason"] == "end_turn"
  assert not (workspace / "stale-escaped").exists()
  assert publisher.cleanup(sid, stale) is False
  # Kill the actual DSH runtime while the managed native scope has a delayed
  # descendant write. This tests runtime death, not a graceful session close.
  command_file.write_text(json.dumps({"command":"printf started > runtime-kill-start; "
      "cat /proc/self/cgroup > runtime-kill-cgroup; "
      "(sleep 2; printf late > runtime-kill-late) & sleep 10"}))
  kill_prompt = "group runtime sigkill descendants"
  kill_nonce = publisher.publish(sid, {
    "platform_id":"group-test-platform", "bot_id":"group-test-bot",
    "conversation_key":"group:" + identity, "requester_id":"member-2",
    "inbound_message_id":"msg-runtime-sigkill",
  }, kill_prompt)
  kill_unit = None
  with ThreadPoolExecutor(1) as pool:
    pending = pool.submit(request, client, "session/prompt", {
      "sessionId":sid,"prompt":[{"type":"text","text":kill_prompt}],
    })
    deadline = time.monotonic() + 8
    while not (workspace / "runtime-kill-start").exists():
      assert not pending.done(), pending.result()
      assert time.monotonic() < deadline, "runtime-kill Bash did not start"
      time.sleep(0.02)
    kill_cgroup = next(line.split("::",1)[1] for line in
      (workspace / "runtime-kill-cgroup").read_text().splitlines() if line.startswith("0::"))
    kill_unit = Path(kill_cgroup).name
    assert kill_unit.startswith("dsh-subprocess-") and kill_unit.endswith(".scope"), kill_unit
    os.kill(client._proc.pid, signal.SIGKILL)
    client._proc.wait(timeout=8)
    try:
      pending.result(timeout=8)
    except Exception:
      pass
  assert publisher.cleanup(sid, kill_nonce)
  time.sleep(2.3)
  assert not (workspace / "runtime-kill-late").exists(), "SIGKILL left a descendant able to mutate group workspace"
  scope_dir = Path("/sys/fs/cgroup") / kill_cgroup.lstrip("/")
  assert not scope_dir.exists(), f"SIGKILL left managed subprocess scope {kill_unit} alive"
  calls = [json.loads(line) for line in log.read_text().splitlines()]
  assert all("bash" in call["tools"] for call in calls)
  print(json.dumps({"native_group_bash":True,"cgroup":cgroup,"pids_created":pids_created,
                    "pids_failed":pids_failed,"memory_limit":536870912}))
except BaseException:
  print("\n".join(client._stderr_lines), file=sys.stderr)
  raise
finally:
  client.close()
  if 'kill_unit' in locals() and kill_unit and Path('/sys/fs/cgroup' + kill_cgroup).exists():
    subprocess.run(['/usr/bin/systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGKILL', kill_unit],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
  publisher.close()
  if os.path.ismount(workspace): unmount_group(workspace)
'''
    worker_root = tmp_path / "isolated"
    worker_root.mkdir(mode=0o700)
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "V2_GROUP_TEST_ROOT": str(worker_root)}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"group native Bash acceptance exceeded 150 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"native_group_bash": true' in stdout
