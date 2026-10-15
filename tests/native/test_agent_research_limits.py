"""Native DSH web-tool limits and failure behavior through a real ACP runtime."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_native_research_limits_and_faults():
    worker = r'''
import json, os, shutil, sys
from pathlib import Path
from tempfile import TemporaryDirectory
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict
class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
root = Path(os.environ["V2_REPO_ROOT"])
with TemporaryDirectory(prefix="agent-research-fault-", dir=root / ".runtime") as tmp:
    base = Path(tmp); home = base / "home"; workspace = base / "workspace"
    home.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    persona = home / "personas"; scene = home / "scenes"; skills = base / "skills"
    persona.mkdir(mode=0o700); scene.mkdir(mode=0o700); skills.mkdir(mode=0o700)
    probe = base / "probe.jsonl"
    web = base / "web.patch.yml"
    web.write_text("- id: web\n  config:\n    searchProvider: fault-search\n    fetchProvider: fault-fetch\n", encoding="utf-8")
    plugin = base / "probe.patch.yml"
    plugin.write_text("- insert:\n    - id: agent-research-fault-probe\n      name: " + str(root / "tests/native/agent-research-fault-probe.mjs") + "\n", encoding="utf-8")
    cfg = HarnessConfig(
        dsh_bin=str(root / ".runtime/dsh-runtime/node_modules/.bin/dsh"), dsh_home=str(home), cwd=str(workspace),
        profile="sdk-minimal", patches=(str(root / "dsh/profile/v2-agent-group.patch.yml"), str(web), str(plugin)),
        env={"LC_MESSAGES":"C", "DSH_SYSTEM_PROMPT":(root / "dsh/profile/agent-group.md").read_text(),
             "V2_DSH_PROVIDER":"agent-research-fault-acceptance", "V2_DSH_MODEL":"scripted",
             "V2_RESEARCH_FAULT_OUTPUT":str(probe), "V2_PERSONA_STATE_ROOT":str(persona),
             "V2_SCENE_STATE_ROOT":str(scene), "V2_AGENT_SKILLS_DIR":str(skills),
             "DEEPSEEK_API_KEY":"", "DEEPSEEK_SEARCH_BASE_URL":"",
             "HTTP_PROXY":"", "HTTPS_PROXY":""}, request_timeout_seconds=90)
    client = HarnessClient(cfg)
    started = __import__('time').monotonic()
    try:
        client.start()
        def req(method, params): return client.request(method, params, response_model=ObjectResponse).model_dump()
        req("initialize", {"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"fault-test","version":"0.1"}})
        for label in ("query-budget", "search-cap", "fetch-cap", "search-failure", "search-timeout"):
            sid = req("session/new", {"cwd":str(workspace),"mcpServers":[]})["sessionId"]
            result = req("session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":label}]})
            assert result["stopReason"] == "end_turn", (label, result)
            req("session/close", {"sessionId":sid})
    except BaseException:
        print("\n".join(client._stderr_lines), file=sys.stderr); raise
    finally: client.close()
    elapsed = __import__('time').monotonic() - started
    rows = [json.loads(line) for line in probe.read_text().splitlines()]
    models = {}
    for row in rows:
        if row["kind"] == "model": models.setdefault(row["sessionId"], []).append(row)
    assert len(models) == 5, models
    def serialized(row): return json.dumps(row["messages"], ensure_ascii=False)
    def find_input(label):
        return next(serialized(calls[1]) for calls in models.values() if label in serialized(calls[1]))
    budget, fetch_cap, failure, timeout = (find_input(label) for label in ("query-budget", "fetch-cap", "search-failure", "search-timeout"))
    assert "at most 2 queries" in budget, budget
    fetch_messages = next(calls[1]["messages"] for calls in models.values() if "fetch-cap" in serialized(calls[1]))
    fetch_result = next(message for message in fetch_messages if message.get("role") == "tool")
    fetch_text = "\n".join(part.get("text", "") for part in fetch_result.get("content", []))
    assert len(fetch_text) <= 32000, len(fetch_text)
    assert "Content truncated." in fetch_text, fetch_text[-200:]
    search_cap_messages = next(calls[1]["messages"] for calls in models.values() if "search-cap" in serialized(calls[1]))
    search_cap_result = next(message for message in search_cap_messages if message.get("role") == "tool")
    search_cap_text = "\n".join(part.get("text", "") for part in search_cap_result.get("content", []))
    assert search_cap_text.count("https://fixture.test/source-") == 8, search_cap_text
    assert "Showing the first 8 sources" in search_cap_text, search_cap_text
    assert "fixture search unavailable" in failure, failure
    assert "timed out after 60000ms" in timeout, timeout
    assert "https://fixture.test/source" not in failure + timeout
    finals = {row["sessionId"]: row["text"] for row in rows if row["kind"] == "final"}
    assert len(finals) == 5, finals
    for session_id, calls in models.items():
        final_input = serialized(calls[1])
        assert "cannot verify a successful result" in finals[session_id], finals
        assert "Fixture" not in finals[session_id] and "fixture.test/source" not in finals[session_id], finals
        tool_errors = [message for message in calls[1]["messages"] if message.get("role") == "tool" and message.get("isError")]
        if "search-failure" in final_input or "search-timeout" in final_input or "at most 2 queries" in final_input:
            assert tool_errors, final_input
    searches = [r for r in rows if r["kind"] == "search"]
    assert [r["query"] for r in searches] == ["search-cap", "search-failure", "search-timeout"], searches
    assert all(r["maxResults"] == 8 for r in searches), searches
    assert any(r["kind"] == "timeout-aborted" for r in rows), rows
    print(json.dumps({"cases":["3-query rejection before provider", "8-result search cap and truncation", "32k fetch output truncation", "search failure returned as tool error", "60000ms native tool timeout and abort"], "elapsed_seconds":round(elapsed,2), "provider_search_calls":len(searches), "public_network":False, "qq":False}))
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen([str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=100)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try: stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); stdout, stderr = process.communicate()
        raise AssertionError(f"native research fault test exceeded 100 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"60000ms native tool timeout and abort"' in stdout
