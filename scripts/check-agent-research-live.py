#!/usr/bin/env python3
"""Explicit real-provider research acceptance in an isolated safe Session."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
from dsh_client import DshClient


def run(provider: str, model: str, base: Path) -> dict:
    started = time.monotonic()
    try:
        client = DshClient(
            dsh_bin=ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh", dsh_home=base / "home",
            workspace=base / "workspace", patch=ROOT / "dsh/profile/v2-agent-group.patch.yml",
            instructions=ROOT / "dsh/profile/agent-group.md", provider=provider, model=model,
        )
        try:
            sid = client.create_session()
            client.select_scene(sid, "research")
            def turn(prompt):
                updates = []
                with client.runtime.client.subscribe_session_notifications(sid) as sub:
                    answer = client.run(sid, prompt)
                    sub.drain(lambda notification: updates.append(notification.payload.get("update", {})))
                return answer, updates
            answer, updates = turn(
                "Synthetic research acceptance, no QQ connection. Load the web-research skill, then use "
                "web_search to find the official DeepSeek Harness repository and documentation. "
                "Give a concise answer with the actual source links. Do not use a send tool or create files.")
            def completed(updates, name, skill=None):
                ids = {item["toolCallId"] for item in updates if item.get("sessionUpdate") == "tool_call"
                       and item.get("title") == name
                       and (skill is None or item.get("rawInput", {}).get("name") == skill)}
                return any(item.get("toolCallId") in ids and item.get("status") == "completed"
                           for item in updates if item.get("sessionUpdate") == "tool_call_update")
            assert completed(updates, "skill", "web-research"), "research skill not loaded"
            assert completed(updates, "web_search"), "native search not completed"
            links = re.findall(r"https?://[^\s)<>\]]+", answer)
            def official_repository(url):
                parsed = urlsplit(url)
                return (parsed.scheme == "https" and parsed.hostname == "github.com"
                        and parsed.port in (None, 443)
                        and parsed.username is None and parsed.password is None
                        and parsed.path.lower().rstrip("/") == "/deepseek-ai/deepseek-harness")
            assert any(official_repository(link) for link in links), "primary repository not cited"
            page, fetch_updates = turn(
                "Continue the synthetic acceptance. Use web_fetch to read https://www.deepseek.com . "
                "Briefly identify one thing visible in the fetched page and cite that exact URL. "
                "Do not infer from memory, search again, send messages, or create files.")
            assert completed(fetch_updates, "web_fetch"), "public fetch not completed"
            page_links = re.findall(r"https?://[^\s)<>\]]+", page)
            client.select_scene(sid, "casual-chat")
            assert client.scenes.current(sid)["scene"] == "casual-chat"
            client.close_session(sid)
            # A link in model prose alone is not proof of a source. Check the
            # native persisted tool metadata against what was actually cited.
            search_urls, fetches = [], []
            for path in (base / "home").rglob("*.jsonl"):
                for line in path.read_text().splitlines():
                    event = json.loads(line)
                    if event.get("type") != "tool/result":continue
                    data = event["data"]
                    if data["message"].get("isError"):continue
                    meta = data.get("meta", {})
                    if isinstance(meta, dict):
                        search_urls.extend(source["url"] for source in meta.get("sources", []))
                        if "url" in meta and "statusCode" in meta:fetches.append(meta)
            def normalize(url):
                parsed = urlsplit(url)
                path = parsed.path.rstrip("/")
                if parsed.hostname == "github.com":path = path.lower()
                return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, parsed.query, ""))
            assert any(normalize(link) in {normalize(url) for url in search_urls}
                       and official_repository(link) for link in links), "cited repository absent from actual native sources"
            assert any(normalize(item["url"]) == normalize("https://www.deepseek.com")
                       and item["statusCode"] == 200 and any(normalize(link) == normalize(item["url"]) for link in page_links)
                       for item in fetches), "no confirmed fetched page citation"
            return {"passed":True,"provider":provider,"model":model,"native_research_skill_loaded":True,
                    "native_search_completed":True,"primary_repository_cited":True,"public_page_fetched_and_cited":True,
                    "citations_match_durable_tool_sources":True,
                    "scene_restored":True,"completed_user_turns":2,"qq_connections":0,
                    "elapsed_seconds":round(time.monotonic()-started,2),
                    "limitation":"Synthetic safe-chat research; not real group traffic or arbitrary-site reachability."}
        finally:
            client.close()
    except Exception as error:
        # Preserve the failed run and a safe classification; do not publish
        # provider messages, authorization headers, or raw credentials.
        error.balance_exhausted = "insufficient balance" in str(error).lower()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=os.environ.get("V2_DSH_PROVIDER"))
    parser.add_argument("--model", default=os.environ.get("V2_DSH_MODEL"))
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/agent-research-live-result.json")
    args = parser.parse_args()
    if not args.provider or not args.model:parser.error("explicit provider/model required")
    base = ROOT / ".runtime" / ("agent-research-live-" + uuid.uuid4().hex)
    base.mkdir(mode=0o700)
    try:
        result = run(args.provider, args.model, base)
    except Exception as error:
        result = {"passed":False,"error_type":type(error).__name__,"qq_connections":0,
                  "balance_exhausted":getattr(error,"balance_exhausted",False)}
    result["artifact"] = str(base)
    with (base / "result.json").open("w", encoding="utf-8") as out:
        json.dump(result,out,indent=2);out.write("\n")
    (base / "result.json").chmod(0o600)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:json.dump(result,out,indent=2);out.write("\n")
    print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
