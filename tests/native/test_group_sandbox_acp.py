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
import asyncio, json, os, signal, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
root = Path(os.environ["V2_REPO_ROOT"])
sys.path.insert(0, str(root / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(root / "astrbot-plugins/v2_journal"))
sys.path.insert(0, str(root / "dsh/plugins/agent-profile"))
from turn_source import TurnSourcePublisher
from group_volume import prepare_group, unmount_group
from journal import JournalStore, Conversation, message_from_onebot
from mcp_server import McpServer
from sys import version_info
class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
def request(client, method, params):
    return client.request(method, params, response_model=ObjectResponse).model_dump()
base = Path(os.environ["V2_GROUP_TEST_ROOT"])
identity = "group-acceptance-" + str(os.getpid())
volume = prepare_group(base, "group-test-platform", "group-test-bot", identity)
workspace = Path(volume["workspace"])
host_sentinel = base / "owner-private-sentinel.txt"
host_sentinel.write_text("OWNER_PRIVATE_SENTINEL_9a71d3", encoding="utf-8")
host_probe_executed = base / "host-probe-executed"
http_hits = []
class SentinelHandler(BaseHTTPRequestHandler):
  def do_GET(self):
    http_hits.append(self.path)
    body = b"OWNER_HTTP_SENTINEL"
    self.send_response(200); self.send_header("Content-Length", str(len(body)))
    self.end_headers(); self.wfile.write(body)
  def log_message(self, *_args): pass
sentinel_http = HTTPServer(("127.0.0.1", 0), SentinelHandler)
http_thread = threading.Thread(target=sentinel_http.serve_forever, daemon=True)
http_thread.start()
home = base / ("home-" + volume["key"])
home.mkdir(mode=0o700)
console_state = home / "console-state.json"
def write_console_editors(editors):
  identity_row = {"platform_id":"group-test-platform", "bot_id":"group-test-bot",
    "conversation_key":"group:" + identity}
  key = json.dumps([identity_row["platform_id"], identity_row["bot_id"],
    identity_row["conversation_key"]], ensure_ascii=False, separators=(",", ":"))
  console_state.write_text(json.dumps({"revision":1,
    "defaults":{"model":None,"enabled":True,"editors":[]},
    "sessions":{key:{"identity":identity_row,
      "settings":{"model":None,"enabled":True,"editors":editors}}},"templates":[]}),
    encoding="utf-8")
  console_state.chmod(0o600)
write_console_editors(["10001"])
# Project-controlled launch configuration must not enter the host Agent.
(workspace / ".env").write_text("V2_AUDIT_PROJECT_ENV_SENTINEL=group-workspace\n")
(home / ".env").write_text("V2_AUDIT_HOME_ENV_SENTINEL=private-home\n")
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
  "V2_CONSOLE_STATE_FILE":str(console_state),
  "V2_QQ_OWNERS":json.dumps([{"platform_id":"group-test-platform",
    "bot_id":"group-test-bot","user_id":"90000"}]),
  "V2_GROUP_HOST_SENTINEL":str(host_sentinel),
  "V2_GROUP_HOST_PROBE_EXECUTED":str(host_probe_executed),
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
def append_message(kind, peer, mid, text):
  raw = {"post_type":"message", "message_type":kind, "self_id":"group-test-bot",
    "user_id":"10001", "message_id":mid,
    "message":[{"type":"text", "data":{"text":text}}]}
  raw["group_id" if kind == "group" else "user_id"] = peer
  journal.append(message_from_onebot("group-test-platform", raw))
journal = JournalStore(base / "canonical-journal.sqlite3")
append_message("group", identity, "group-marker", "group-visible-history")
append_message("group", "other-group", "other-marker", "OTHER_GROUP_PRIVATE_SENTINEL")
append_message("private", "1105927155", "owner-marker", "OWNER_PRIVATE_HISTORY_SENTINEL")
mcp_server = McpServer(journal)
mcp_loop = asyncio.new_event_loop()
mcp_thread = threading.Thread(target=mcp_loop.run_forever, daemon=True)
mcp_thread.start()
def mcp_call(awaitable):
  return asyncio.run_coroutine_threadsafe(awaitable, mcp_loop).result(timeout=10)
mcp_call(mcp_server.start(0))
sent_messages = []
async def send_message(text): sent_messages.append(text)
async def group_info(): return {"name":"isolated test group", "member_count":2}
mcp_turn = mcp_server.turn(Conversation("group-test-platform", "group-test-bot", "group:" + identity),
                           send_message, group_info)
mcp_servers = mcp_call(mcp_turn.__aenter__())
try:
  client.start()
  request(client, "initialize", {"protocolVersion":1,"clientCapabilities":{},
    "clientInfo":{"name":"group-bash-acceptance","version":"0.1.0"}})
  sid = request(client, "session/new", {"cwd":str(workspace),"mcpServers":mcp_servers})["sessionId"]
  def run(prompt, command="", timeout=None, workdir=None, tool_name="bash", arguments=None,
          requester_id="10001"):
    previous_calls = len(log.read_text().splitlines()) if log.exists() else 0
    tool_call_id = "group-audit-" + str(previous_calls + 1)
    command_file.write_text(json.dumps({"command":command, "toolName":tool_name,
      **({} if arguments is None else {"arguments":arguments}),
      **({} if timeout is None else {"timeoutMs":timeout}),
      **({} if workdir is None else {"workdir":workdir})}))
    nonce = publisher.publish(sid, {
      "platform_id":"group-test-platform", "bot_id":"group-test-bot",
      "conversation_key":"group:" + identity, "requester_id":requester_id,
      "inbound_message_id":"msg-" + prompt,
    }, prompt)
    try:
      result = request(client, "session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":prompt}]})
      assert result["stopReason"] == "end_turn", result
    finally:
      assert publisher.cleanup(sid, nonce)
    new_calls = [json.loads(line) for line in log.read_text().splitlines()[previous_calls:]]
    tool_results = [message for call in new_calls for message in call["messages"]
                    if message.get("role") == "tool" and message.get("toolCallId") == tool_call_id]
    assert len(tool_results) == 1, (tool_call_id, tool_results)
    return "\n".join(block.get("text", "") for block in tool_results[0].get("content", [])
                     if block.get("type") == "text")
  # Actual native tool metadata and a successful workspace operation.
  run("group success", "printf '%s\\n' \"$HOME\" > home.txt; pwd > cwd.txt; printf ok > marker.txt; "
      "test ! -e /etc/shadow && test -z \"${GROUP_SECRET_SENTINEL:-}\"")
  assert (workspace / "home.txt").read_text().strip() == "/workspace"
  assert (workspace / "cwd.txt").read_text().strip() == "/workspace"
  assert (workspace / "marker.txt").read_text() == "ok"
  # The current console editors list controls the actual mount mode. A removed
  # editor retains shell reads while shell redirection cannot alter the volume.
  write_console_editors([])
  denied_write = run("member write denied", "cat marker.txt >/dev/null && printf altered > revoked-write.txt")
  assert "read-only file system" in denied_write.lower(), denied_write
  assert not (workspace / "revoked-write.txt").exists()
  # A trusted deployed Bot Owner can write even when absent from group editors.
  owner_write = run("trusted owner write", "printf owner > owner-write.txt; printf OWNER_WRITE_OK",
                    requester_id="90000")
  assert "OWNER_WRITE_OK" in owner_write
  assert (workspace / "owner-write.txt").read_text() == "owner"
  write_console_editors(["10001"])
  assert not host_sitecustomize_marker.exists(), "untrusted Python sitecustomize ran before sandbox setup"
  # Lock the exact complete model-facing inventory for this pinned Profile,
  # including SDK resource tools (visible but denied) and deployed scoped MCP capabilities.
  initial = json.loads(log.read_text().splitlines()[0])
  inventory = set(initial["tools"])
  expected = {"bash", "skill", "web_search", "web_fetch", "host_probe",
              "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
              "mcp__astrbot__history_recent", "mcp__astrbot__history_before",
              "mcp__astrbot__history_search", "mcp__astrbot__history_read_image", "mcp__astrbot__current_group_info",
              "mcp__astrbot__qq_send_origin", "mcp__astrbot__qq_file_receive", "mcp__astrbot__qq_file_send",
              "mcp__astrbot__current_group_members", "mcp__astrbot__qq_prepare_reply",
              "mcp__astrbot__meme_categories", "mcp__astrbot__meme_list", "mcp__astrbot__meme_send", "mcp__astrbot__plugin_status",
              "mcp__astrbot__bili_dynamics", "mcp__astrbot__bili_subscribe",
              "mcp__astrbot__bili_subscriptions", "mcp__astrbot__bili_unsubscribe",
              "mcp__astrbot__twitter_subscribe", "mcp__astrbot__twitter_subscriptions",
              "mcp__astrbot__twitter_unsubscribe"}
  assert inventory == expected, sorted(inventory)
  schemas = {tool["name"]:tool for tool in initial["toolSchemas"]}
  assert set(schemas) == expected
  assert {"file_path", "path"}.isdisjoint(schemas["web_fetch"]["parameters"].get("properties", {}))
  assert "name" in schemas["skill"]["parameters"]["properties"]
  meme_schema = schemas["mcp__astrbot__meme_list"]["parameters"]
  assert "category" in meme_schema["required"]
  assert "query" not in meme_schema["properties"]
  assert meme_schema["properties"]["limit"]["maximum"] == 50
  assert meme_schema["properties"]["offset"]["minimum"] == 0
  assert schemas["mcp__astrbot__meme_categories"]["parameters"]["properties"] == {}

  # SDK resource tools are loaded by the pinned client, but must fail closed
  # under the group guard even when an MCP server exposes resources.
  denied_resources = []
  for name in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
    result = run("group SDK resource tool denied " + name, tool_name=name,
                 arguments={"server":"astrbot", "uri":"file:///etc/passwd"})
    assert "authorization denied: unavailable tool" in result, (name, result)
    denied_resources.append(name)
  # The five profile-allowed MCP tools are host services by design. Exercise
  # every schema against an isolated journal and an in-memory send callback.
  search_result = json.loads(run("group scoped MCP search", tool_name="mcp__astrbot__history_search",
                                 arguments={"query":"PRIVATE_SENTINEL"}))
  assert search_result == {"entries":[]}, search_result
  recent_result = json.loads(run("group scoped MCP recent", tool_name="mcp__astrbot__history_recent",
                                 arguments={"limit":10}))
  assert [row["content"]["text"] for row in recent_result["entries"]] == ["group-visible-history"]
  group_conversation = Conversation("group-test-platform", "group-test-bot", "group:" + identity)
  group_row = journal.recent(group_conversation, 10)[0]
  before_result = json.loads(run("group scoped MCP before", tool_name="mcp__astrbot__history_before",
                                 arguments={"before_id":group_row.journal_id + 1, "limit":10}))
  assert [row["content"]["text"] for row in before_result["entries"]] == ["group-visible-history"]
  info_result = json.loads(run("group scoped MCP current info", tool_name="mcp__astrbot__current_group_info"))
  assert info_result["id"] == identity and info_result["member_count"] == 2
  send_result = json.loads(run("group scoped MCP send mock", tool_name="mcp__astrbot__qq_send_origin",
                               arguments={"text":"local mock only"}))
  assert send_result["submitted"] is True and send_result["platform_confirmed"] is False
  assert sent_messages == ["local mock only"]
  # The sandbox guard denies a newly registered extension before its host-side
  # implementation can read the Owner sentinel.
  host_probe_result = run("group extension host read denied", tool_name="host_probe")
  assert "authorization denied: unavailable tool" in host_probe_result
  assert not host_probe_executed.exists()
  # Skill lookup accepts catalog names only; a path-shaped name cannot turn the
  # host-side read-only skills provider into an arbitrary filesystem reader.
  skill_result = run("group skill path traversal denied", tool_name="skill",
                     arguments={"name":str(host_sentinel)})
  assert "invalid skill name" in skill_result
  # web_fetch is intentionally a controlled host service. Its native provider
  # rejects file: URLs before reading host files; only public HTTP(S) is allowed.
  file_url_result = run("group web file URL denied", tool_name="web_fetch",
                        arguments={"url":host_sentinel.as_uri()})
  assert 'unsupported URL scheme "file:"' in file_url_result
  loopback_result = run("group web loopback URL denied", tool_name="web_fetch",
                        arguments={"url":f"http://127.0.0.1:{sentinel_http.server_port}/owner-data"})
  assert "non-public IP address" in loopback_result, loopback_result
  calls = [json.loads(line) for line in log.read_text().splitlines()]
  transcript = json.dumps([call["messages"] for call in calls], ensure_ascii=False)
  assert "OWNER_PRIVATE_SENTINEL_9a71d3" not in transcript, transcript[-5000:]
  assert "OWNER_PRIVATE_HISTORY_SENTINEL" not in transcript, transcript[-5000:]
  assert "OTHER_GROUP_PRIVATE_SENTINEL" not in transcript, transcript[-5000:]
  assert not http_hits, http_hits
  assert not host_probe_executed.exists()
  # Inspect the actual live scope before the native subprocess provider reaps it.
  command_file.write_text(json.dumps({"command":"cat /proc/self/cgroup > cgroup.txt; sleep 2"}))
  cgroup_prompt = "group cgroup limits"
  cgroup_nonce = publisher.publish(sid, {
    "platform_id":"group-test-platform", "bot_id":"group-test-bot",
    "conversation_key":"group:" + identity, "requester_id":"10001",
    "inbound_message_id":"msg-cgroup",
  }, cgroup_prompt)
  with ThreadPoolExecutor(1) as pool:
    pending = pool.submit(request, client, "session/prompt", {
      "sessionId":sid,"prompt":[{"type":"text","text":cgroup_prompt}],
    })
    deadline = time.monotonic() + 8
    # Shell redirection creates the file before cat writes its content.
    # Wait for the actual cgroup line, rather than racing the empty file.
    cgroup_lines = []
    while not any(line.startswith("0::") for line in cgroup_lines):
      assert not pending.done(), pending.result()
      assert time.monotonic() < deadline, "native Bash did not publish its cgroup path"
      if (workspace / "cgroup.txt").exists():
        cgroup_lines = (workspace / "cgroup.txt").read_text().splitlines()
      time.sleep(0.02)
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
    "conversation_key":"group:" + identity, "requester_id":"10001",
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
    "conversation_key":"group:" + identity, "requester_id":"10001",
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
    "conversation_key":"group:" + identity, "requester_id":"10001",
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
    kill_lines = []
    while not any(line.startswith("0::") for line in kill_lines):
      assert not pending.done(), pending.result()
      assert time.monotonic() < deadline, "runtime-kill Bash did not publish its cgroup path"
      if (workspace / "runtime-kill-cgroup").exists():
        kill_lines = (workspace / "runtime-kill-cgroup").read_text().splitlines()
      time.sleep(0.02)
    kill_cgroup = next(line.split("::",1)[1] for line in kill_lines if line.startswith("0::"))
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
  evidence = {
    "native_group_bash": True,
    "profile_tool_schemas": sorted(inventory - {"host_probe"}),
    "test_only_tool_schemas": ["host_probe"],
    "mcp_tools_called": sorted(name for name in expected if name.startswith("mcp__astrbot__")),
    "sdk_resource_tools_denied": denied_resources,
    "unknown_extension_denied": "authorization denied: unavailable tool" in host_probe_result,
    "skill_path_denied": "invalid skill name" in skill_result,
    "web_file_url_denied": 'unsupported URL scheme "file:"' in file_url_result,
    "web_loopback_url_denied": not http_hits,
    "owner_and_other_group_sentinels_absent": all(
      marker not in transcript for marker in ("OWNER_PRIVATE_HISTORY_SENTINEL", "OTHER_GROUP_PRIVATE_SENTINEL")
    ),
    "mock_send_calls": len(sent_messages),
    "cgroup": cgroup,
    "pids_created": pids_created,
    "pids_failed": pids_failed,
    "memory_limit": 536870912,
  }
  (base / "tool-audit-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
  print(json.dumps(evidence, ensure_ascii=False))
except BaseException:
  print("\n".join(client._stderr_lines), file=sys.stderr)
  raise
finally:
  client.close()
  if 'kill_unit' in locals() and kill_unit and Path('/sys/fs/cgroup' + kill_cgroup).exists():
    subprocess.run(['/usr/bin/systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGKILL', kill_unit],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
  publisher.close()
  mcp_call(mcp_turn.__aexit__(None, None, None))
  mcp_call(mcp_server.close())
  mcp_loop.call_soon_threadsafe(mcp_loop.stop)
  mcp_thread.join(timeout=3)
  mcp_loop.close()
  sentinel_http.shutdown()
  http_thread.join(timeout=3)
  sentinel_http.server_close()
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
