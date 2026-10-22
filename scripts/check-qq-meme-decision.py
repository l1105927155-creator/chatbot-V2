#!/usr/bin/env python3
"""Observe natural-chat meme decisions using live inference and isolated QQ."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import subprocess
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wake = load("meme_decision_wake", ROOT / "scripts/check-qq-wake.py")
sys.path.insert(0, str(ROOT / "dsh/plugins/agent-profile"))
import group_volume  # noqa: E402

# Fixed before inference: no requests for images, tools, or emotional reactions.
EPISODES = [
    ("下班闲聊", ["小v，我终于下班啦，今天可以光明正大躺着了",
              "刚坐下猫就把我的位置占了，现在它才是这个家的主人",
              "我跟它讲道理，它打了个哈欠，感觉我输了",
              "行吧，给它加餐，我去坐小板凳",
              "不过外卖到了，是我喜欢的炸鸡，今天还是我赢",
              "给你也留一块，隔空分你一点快乐"]),
    ("游戏趣事", ["小v，刚才打游戏差一点就赢了，我现在还不服气",
              "我朋友说他来带飞，结果第一个掉下去了，笑死",
              "他还嘴硬说这是在探路，确实很有探索精神",
              "下一把我俩互相救，最后一起掉下去，配合得非常默契",
              "算了算了，快乐也是一种胜利",
              "要是你在，估计已经笑得不想认我们了"]),
    ("小小好事", ["小v，今天买饮料抽到了免单，运气突然就来了",
              "我还专门看了两遍，怕是自己看错了，嘿嘿",
              "省下来的钱准备拿去买个小蛋糕",
              "店里最后一块草莓的居然还在，今天真的被偏爱了",
              "吃到了，奶油一点也不腻，满足",
              "你今天有没有什么想开心一下的小事情"]),
    ("犯困打趣", ["小v，午睡睡过头了，刚醒还以为明天了",
              "看了一眼手机才知道只过了四十分钟，我这觉真划算",
              "头发睡得像刚打完一架，而且没打赢",
              "我决定先喝杯水，假装自己是个有条理的人",
              "好了，现在精神了，脑子终于重新上线",
              "刚醒就来找你聊天，我是不是挺给你面子的"]),
    ("亲近闲聊", ["小v，外面下雨了，我窝在家里听雨，还挺舒服的",
              "毯子和热可可都准备好了，现在差个一起聊天的",
              "你来了就齐了，今天不想干正事，只想偷懒",
              "刚才热可可差点烫到，优雅失败了",
              "你可别笑我，我刚才已经假装什么都没发生了",
              "好啦，跟你聊一会儿心情确实好多了"]),
]


def rate(k, n):
    if not n:
        return {"count": k, "total": n, "rate": None, "wilson95": None}
    z = 1.95996398454
    p = k / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"count": k, "total": n, "rate": p,
            "wilson95": [max(0, center - half), min(1, center + half)]}


class Check(wake.Check):
    def __init__(self, directory, release, source, sender, resume=False):
        super().__init__(directory, release, "live")
        self.owner = sender
        self.key = "group:91000"
        self.source = source
        self.resume = resume
        self.samples = json.loads((directory / "samples.json").read_text()) if resume else []

    def start(self):
        if self.resume:
            self.source_hashes = json.loads((self.directory / "design.json").read_text())["source_hashes"]
            details = json.loads((self.directory / "sandbox-runtime.json").read_text())
            self.workspace = Path(details["staged_repo"])
            self.group_workspace = Path(group_volume.paths(
                self.workspace / ".runtime/group-storage", "v2-sandbox", "99000", "91000")["workspace"])
            self.snapshot = self.directory / "source-workspace"
            self.onebot, self.ui, self.mcp = [details["ports"][k] for k in ["onebot", "ui", "mcp"]]
            self.base = f"http://127.0.0.1:{self.ui}"
            self.log = (self.directory / "runtime.log").open("ab")
            self.process = subprocess.Popen([
                str(ROOT / ".runtime/astrbot/.venv/bin/python"), str(Path(__file__).resolve()),
                "--launch-private", str(self.directory)],
                cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            self.wait(lambda: self.http("/api/state").get("connected"), "resumed OneBot connected", timeout=90)
            def ready():
                with wake.urlopen(f"http://127.0.0.1:{self.mcp}/healthz", timeout=3) as response:
                    return json.load(response)["ready"]
            self.wait(ready, "resumed Agent ready", timeout=90)
            return
        original = wake.runtime.prepare

        def prepare(*args, **kwargs):
            details = original(*args, **kwargs)
            stage = Path(details["staged_repo"])
            shutil.copytree(ROOT / "meme", stage / "meme")
            snapshot = self.directory / "source-workspace"
            snapshot.mkdir()
            for name in ["AGENTS.md", "persona.md", "Owner.json", "apps/affinity", "skills/affinity"]:
                source = self.source / name
                target = snapshot / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(source, target)
            self.source_hashes = self.hashes(self.source)
            group_volume.prepare_group(stage / ".runtime/group-storage", "v2-sandbox", "99000", "91000")
            self.group_workspace = Path(group_volume.paths(
                stage / ".runtime/group-storage", "v2-sandbox", "99000", "91000")["workspace"])
            self.snapshot = snapshot
            self.reset_files()
            shared_home = Path(json.loads((ROOT / ".runtime/agent-service.json").read_text())["shared_models_home"])
            shared = stage / ".runtime/model-fixture"
            shared.mkdir()
            shutil.copy2(shared_home / "profiles/web/cordis.patch.yml", shared / "cordis.patch.yml")
            credentials = yaml.safe_load((shared_home / ".credentials.yaml").read_text())
            isolated = {"version": credentials["version"], "records": {},
                        "refs": {"WB_API_KEY": credentials["refs"]["WB_API_KEY"]}}
            (shared / ".credentials.yaml").write_text(yaml.safe_dump(isolated))
            (shared / ".credentials.yaml").chmod(0o600)
            return details

        wake.runtime.prepare = prepare
        try:
            details = prepare(self.directory, ROOT / ".runtime/astrbot",
                              {"onebot": self.onebot, "ui": self.ui, "mcp": self.mcp}, "live", self.owner)
            stage = Path(details["staged_repo"])
            target = stage / "astrbot-plugins/v2_dsh_router"
            shutil.rmtree(target)
            shutil.copytree(self.release / "astrbot-plugins/v2_dsh_router", target,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            self.log = (self.directory / "runtime.log").open("ab")
            self.process = subprocess.Popen([
                str(ROOT / ".runtime/astrbot/.venv/bin/python"), str(Path(__file__).resolve()),
                "--launch-private", str(self.directory)], cwd=ROOT,
                stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            self.wait(lambda: self.http("/api/state").get("connected"), "OneBot connected", timeout=90)
            def ready():
                with wake.urlopen(f"http://127.0.0.1:{self.mcp}/healthz", timeout=3) as response:
                    return json.load(response)["ready"]
            self.wait(ready, "Agent ready", timeout=90)
            self.workspace = stage
        finally:
            wake.runtime.prepare = original

    @staticmethod
    def hashes(workspace):
        return {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
                for name in ["AGENTS.md", "persona.md", "apps/affinity/data/members.json"]}

    def reset_files(self):
        shutil.copytree(self.snapshot, self.group_workspace, dirs_exist_ok=True)
        assert self.hashes(self.group_workspace) == self.hashes(self.snapshot)

    def skill_calls(self, event):
        starts = {r["call_id"] for r in self.trace(event)
                  if r["event"] == "tool/start" and r.get("tool") == "skill"}
        if not starts:
            return []
        sid = next(r["session_id"] for r in self.trace(event) if r["event"] == "dsh/dispatched")
        found = []
        for path in (self.workspace / ".runtime").glob("dsh-home-*/**/session.v4.jsonl"):
            if path.parent.name != sid:
                continue
            calls, results = {}, {}
            for line in path.read_text().splitlines():
                row = json.loads(line)
                d = row.get("data", {})
                if row["type"] == "tool/call":
                    calls[d["callId"]] = d
                elif row["type"] == "tool/result":
                    results[d["message"].get("toolCallId")] = d["message"]
            for cid, call in calls.items():
                if hashlib.sha256(cid.encode()).hexdigest()[:24] in starts:
                    assert cid in results
                    found.append({"call": call, "result": {"message": results[cid]}})
        assert len(found) == len(starts)
        return found

    @staticmethod
    def send_confirmed(pairs):
        for pair in pairs:
            message = pair["result"]["message"]
            text = "".join(block.get("text", "") for block in message.get("content", [])
                           if block.get("type") == "text")
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not message.get("isError") and isinstance(payload, dict) and payload.get("sent") is True:
                return True
        return False

    def observe(self, episode, index, text, event=None):
        started = time.monotonic()
        event = event or self.wake(self.key, text, sender=self.owner)
        self.wait(lambda: any(r["event"] in {"turn/completed", "turn/failed"}
                             for r in self.trace(event)), "chat terminal", timeout=210)
        trace = self.trace(event)
        tools = [r.get("tool") for r in trace if r["event"] == "tool/start"]
        skill_calls = self.skill_calls(event)
        meme_skills = [pair for pair in skill_calls
                       if json.loads(pair["call"]["arguments"]).get("name") == "meme"]
        loaded = any(not pair["result"]["message"].get("isError") for pair in meme_skills)
        attempted = bool(meme_skills) or any(name.startswith("mcp__astrbot__meme_") for name in tools)
        sent = self.correlated(event, "mcp__astrbot__meme_send")
        outputs = self.outputs(event)
        images = [row for row in outputs if any(s["type"] == "image" for s in row["event"]["message"])]
        receipts = {str(r.get("message_id")) for r in trace if r["event"] == "send/receipt"}
        success = bool(self.send_confirmed(sent) and images
                       and any(str(row["message_id"]) in receipts for row in images))
        row = {"episode": episode, "index": index, "input": text,
               "message_id": str(event["message_id"]), "completed": any(r["event"] == "turn/completed" for r in trace),
               "meme_skill_loaded": loaded, "attempted": attempted,
               "retrieval_attempted": any(name in tools for name in ["mcp__astrbot__meme_categories", "mcp__astrbot__meme_list"]),
               "send_attempted": "mcp__astrbot__meme_send" in tools,
               "success": success, "tools": tools, "outputs": outputs,
               "skill_calls": skill_calls, "send_results": sent,
               "elapsed_seconds": round(time.monotonic() - started, 2)}
        self.samples.append(row)
        self.save("samples.json", self.samples)
        print(json.dumps({k: v for k, v in row.items() if k not in {
            "outputs", "skill_calls", "send_results", "tools"}}, ensure_ascii=False), flush=True)
        if not row["completed"]:
            raise RuntimeError("actual model turn failed; no samples silently excluded")

    def run(self):
        error = None
        try:
            if any(not row["completed"] for row in self.samples):
                raise RuntimeError("cannot resume an evaluation containing an incomplete model turn")
            self.start()
            self.save("design.json", {"episodes": EPISODES, "source_hashes": self.source_hashes,
                      "provider": os.environ["V2_DSH_PROVIDER"], "model": os.environ["V2_DSH_MODEL"],
                      "sender": self.owner, "attempt_definition": "meme Skill load or any meme tool start"})
            position = 0
            for episode, messages in EPISODES:
                if len(self.samples) >= position + len(messages):
                    position += len(messages)
                    continue
                if len(self.samples) == position:
                    self._deterministic("/new", "已开始新的 Agent 对话", key=self.key, sender=self.owner)
                    self.reset_files()
                for index, text in enumerate(messages, 1):
                    position += 1
                    if position <= len(self.samples):
                        continue
                    recovered = None
                    if self.resume:
                        matches = [{**row["event"], "conversation": row["conversation"]}
                                   for row in self.events() if row["direction"] == "inbound"
                                   and any(seg.get("data", {}).get("text") == text
                                           for seg in row["event"].get("message", []))]
                        assert len(matches) <= 1
                        recovered = matches[0] if matches else None
                    self.observe(episode, index, text, event=recovered)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if self.process:
                try:
                    self.save("evidence.json", self.request("GET", "/api/evidence/export"))
                except Exception:
                    pass
            self.stop()
        n = len(self.samples)
        attempts = sum(s["attempted"] for s in self.samples)
        successes = sum(s["success"] for s in self.samples)
        result = {"finished": n == sum(len(messages) for _, messages in EPISODES)
                  and all(row["completed"] for row in self.samples) and error is None, "error": error,
                  "attempt_rate": rate(attempts, n), "success_rate": rate(successes, n),
                  "success_given_attempt": rate(successes, attempts),
                  "success_given_send": rate(successes, sum(s["send_attempted"] for s in self.samples)),
                  "retrieval_rate": rate(sum(s["retrieval_attempted"] for s in self.samples), n),
                  "send_attempt_rate": rate(sum(s["send_attempted"] for s in self.samples), n),
                  "production_source_unchanged": self.hashes(self.source) == self.source_hashes,
                  "real_qq_connections": 0}
        self.save("result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--launch-private", type=Path)
    parser.add_argument("--source-workspace", type=Path)
    parser.add_argument("--sender")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not all(os.environ.get(k) for k in ["V2_DSH_PROVIDER", "V2_DSH_MODEL", "DEEPSEEK_API_KEY"]):
        parser.error("live provider/model/key must be set in process environment")
    os.umask(0o077)
    if args.launch_private:
        details = json.loads((args.launch_private / "sandbox-runtime.json").read_text())
        shared = Path(details["staged_repo"]) / ".runtime/model-fixture"
        original_environment = wake.runtime._child_environment
        def child_environment(**kwargs):
            environment = original_environment(**kwargs)
            if kwargs["dsh_enabled"]:
                environment.update(V2_SHARED_MODELS_PROFILE=str(shared / "cordis.patch.yml"),
                                   V2_SHARED_MODELS_CREDENTIALS=str(shared / ".credentials.yaml"))
            return environment
        wake.runtime._child_environment = child_environment
        raise SystemExit(wake.runtime._run(details, ROOT / ".runtime/astrbot", details["ports"],
                                           "live", owner_volume_mib=32))
    if not all([args.data_dir, args.source_workspace, args.sender]):
        parser.error("data-dir/source-workspace/sender required")
    deployed = (ROOT / ".runtime/astrbot/data/plugins/v2_dsh_router").resolve().parents[1]
    result = Check(args.data_dir.resolve(), deployed, args.source_workspace.resolve(), args.sender, args.resume).run()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["finished"] else 1)
