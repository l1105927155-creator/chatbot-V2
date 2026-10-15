"""Exercise the sandbox probe inside the pinned DSH ACP runtime."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


def test_sandbox_native_probe_uses_dsh_lifecycle_and_tools():
    truncation = subprocess.run(["node", str(ROOT / "tests/native/sandbox-probe-test.mjs")], cwd=ROOT,
                                text=True, capture_output=True, check=False)
    assert truncation.returncode == 0, truncation.stdout + truncation.stderr
    worker = r'''
import asyncio, json, os, signal, sys
from pathlib import Path
from tempfile import TemporaryDirectory
from aiohttp import web
from deepseek_harness.client import HarnessClient, HarnessConfig
from pydantic import BaseModel, ConfigDict

class ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

async def main():
    scripts = [
        {"kind":"reply", "text":"native probe scripted reply"},
        {"kind":"tool", "name":"web_search", "arguments":{"queries":["fixture query"]},
         "text_after":"native search tool cycle completed"},
        {"kind":"tool", "name":"web_fetch", "arguments":{"url":"https://sandbox.invalid/fixture"},
         "text_after":"native fetch tool cycle completed"},
        {"kind":"tool", "name":"web_search", "arguments":{"queries":["failing query"]},
         "text_after":"native tool error was observed"},
        {"kind":"tool", "name":"bash", "arguments":{"command":"touch forbidden"},
         "text_after":"should not run"},
    ]
    consumed = set()
    rows = []
    search_calls = 0
    async def claim(request):
        nonlocal search_calls
        body = await request.json()
        script = None
        if body.get("target") == "model" and body.get("phase") == "before":
            key = (body.get("session_id"), body.get("native_turn_id"))
            if key not in consumed:
                consumed.add(key)
                if scripts: script = scripts.pop(0)
        fault = None
        if body.get("target") == "search" and body.get("phase") == "before":
            search_calls += 1
            if search_calls == 1:
                fault = {"id":"search-noop-truncate-1", "kind":"truncate", "limit_results":2}
            elif search_calls == 2:
                fault = {"id":"search-failure-1", "kind":"error", "error":"sandbox fixture search failure"}
        if body.get("target") == "fetch" and body.get("phase") == "after":
            fault = {"id":"fetch-truncate-1", "kind":"truncate", "limit_chars":12}
        rows.append({"kind":"claim", **body})
        return web.json_response({"fault":fault, "script":script})
    async def observe(request):
        rows.append({"kind":"event", **await request.json()})
        return web.json_response({"ok":True})
    app = web.Application()
    app.router.add_post("/api/internal/claim", claim)
    app.router.add_post("/api/internal/probe", observe)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    with TemporaryDirectory(prefix="sandbox-native-probe-", dir=Path(os.environ["V2_REPO_ROOT"]) / ".runtime") as temp:
        base = Path(temp); home = base / "home"; workspace = base / "workspace"
        home.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
        persona = home / "personas"; scene = home / "scenes"; skills = base / "skills"
        persona.mkdir(mode=0o700); scene.mkdir(mode=0o700); skills.mkdir(mode=0o700)
        web_patch = base / "web.patch.yml"
        web_patch.write_text("- id: web\n  config:\n    searchProvider: fault-search\n    fetchProvider: fault-fetch\n", encoding="utf-8")
        plugin_patch = base / "probe.patch.yml"
        plugin_patch.write_text(
            "- insert:\n    - id: agent-research-fault-probe\n      name: " +
            str(Path(os.environ["V2_REPO_ROOT"]) / "tests/native/agent-research-fault-probe.mjs") +
            "\n    - id: v2-sandbox-native-probe\n      name: " +
            str(Path(os.environ["V2_REPO_ROOT"]) / "sandbox/probe.mjs") +
            "\n      config:\n        controlUrl: http://127.0.0.1:" + str(port) +
            "\n        token: native-test-token\n        mode: live\n        searchProvider: fault-search\n        fetchProvider: fault-fetch\n", encoding="utf-8")
        cfg = HarnessConfig(
            dsh_bin=str(Path(os.environ["V2_REPO_ROOT"]) / ".runtime/dsh-runtime/node_modules/.bin/dsh"),
            dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal",
            patches=(str(Path(os.environ["V2_REPO_ROOT"]) / "dsh/profile/v2-agent-group.patch.yml"),
                     str(web_patch), str(plugin_patch)),
            env={"LC_MESSAGES":"C", "DSH_SYSTEM_PROMPT":(Path(os.environ["V2_REPO_ROOT"]) / "dsh/profile/agent-group.md").read_text(),
                 "V2_DSH_PROVIDER":"sandbox-scripted", "V2_DSH_MODEL":"scripted",
                 "V2_PERSONA_STATE_ROOT":str(persona), "V2_SCENE_STATE_ROOT":str(scene),
                 "V2_AGENT_SKILLS_DIR":str(skills), "V2_RESEARCH_FAULT_OUTPUT":str(base / "provider.jsonl"),
                 "DEEPSEEK_API_KEY":"", "DEEPSEEK_SEARCH_BASE_URL":"",
                 "HTTP_PROXY":"", "HTTPS_PROXY":""}, request_timeout_seconds=60)
        client = HarnessClient(cfg)
        try:
            client.start()
            async def req(method, params):
                result = await asyncio.to_thread(client.request, method, params, response_model=ObjectResponse)
                return result.model_dump()
            await req("initialize", {"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"sandbox-probe-test","version":"1"}})
            sid = (await req("session/new", {"cwd":str(workspace),"mcpServers":[]}))["sessionId"]
            first = await req("session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":"first prompt"}]})
            assert first["stopReason"] == "end_turn", first
            second = await req("session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":"search now"}]})
            assert second["stopReason"] == "end_turn", second
            third = await req("session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":"fetch now"}]})
            assert third["stopReason"] == "end_turn", third
            fourth = await req("session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":"search failure"}]})
            assert fourth["stopReason"] == "end_turn", fourth
            fifth = await req("session/prompt", {"sessionId":sid,"prompt":[{"type":"text","text":"x" * 40000 + "TAIL_MARKER"}]})
            assert fifth["stopReason"] == "end_turn", fifth
            await req("session/close", {"sessionId":sid})
        except BaseException:
            print(json.dumps({"stderr":client._stderr_lines,
                "events":[{key:row.get(key) for key in ("event","target","phase","tool","status","error_type")}
                          for row in rows]}, default=str), file=sys.stderr)
            raise
        finally:
            client.close()
        assert scripts == [], scripts
        event_rows = [row for row in rows if row.get("kind") == "event"]
        assert any(row.get("event") == "turn/start" for row in event_rows), event_rows
        assert any(row.get("event") == "turn/end" for row in event_rows), event_rows
        model_starts = [row for row in event_rows if row.get("event") == "model/start"]
        assert len(model_starts) >= 4, model_starts
        assert all(row.get("input") and len(row["input"]) <= 32769 for row in model_starts), model_starts
        model_call_ids = {row.get("call_id") for row in model_starts}
        model_claims = [row for row in rows if row.get("kind") == "claim" and row.get("target") == "model"
                        and row.get("phase") == "before"]
        assert model_claims and all(row.get("call_id") in model_call_ids for row in model_claims), model_claims
        assert all(row.get("source") == "native-probe" for row in event_rows), event_rows
        assert any(row.get("event") == "tool/start" and row.get("tool") == "web_search" for row in event_rows), event_rows
        assert any(row.get("event") == "tool/end" and row.get("tool") == "web_search" for row in event_rows), event_rows
        search_claims = [row for row in rows if row.get("kind") == "claim" and row.get("target") == "search"]
        assert search_claims, rows
        search_tool_calls = {row.get("call_id") for row in event_rows
                             if row.get("event") == "tool/start" and row.get("tool") == "web_search"}
        assert all(row.get("call_id") in search_tool_calls for row in search_claims), search_claims
        assert any("fixture.test/source" in row.get("input", "") for row in model_starts), "search result omitted from model input"
        assert any(row.get("input_projection") == "tail" and row.get("input_truncated")
                   and "TAIL_MARKER" in row.get("input", "") for row in model_starts), "tail projection lost newest prompt content"
        assert any(row.get("target") == "fetch" and row.get("phase") == "after" for row in rows if row.get("kind") == "claim"), rows
        assert any(("z" * 12) in row.get("input", "") and ("z" * 13) not in row.get("input", "")
                   for row in model_starts), "fetch truncation missing from model input"
        assert any("sandbox fixture search failure" in row.get("input", "") for row in model_starts), "search error missing from model input"
        assert any(row.get("event") == "tool/end" and row.get("tool") == "web_search" and row.get("status") == "error"
                   for row in event_rows), event_rows
        bash_calls = [row for row in rows if row.get("kind") == "event" and row.get("event") == "tool/start" and row.get("tool") == "bash"]
        assert not bash_calls, bash_calls
        assert not (workspace / "forbidden").exists()
        print(json.dumps({"turn_events":sum(row.get("event") == "turn/start" for row in event_rows),
            "model_requests":len(model_starts), "search_calls":len(search_claims), "hidden_bash_executed":False,
            "real_provider":False, "qq":False}))
    await runner.cleanup()

asyncio.run(main())
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT)}
    process = subprocess.Popen([str(PYTHON), "-c", worker], cwd=ROOT, env=env,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try: stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); stdout, stderr = process.communicate()
        raise AssertionError(f"native sandbox probe test exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"hidden_bash_executed": false' in stdout
