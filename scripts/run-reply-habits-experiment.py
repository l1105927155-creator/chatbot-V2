#!/usr/bin/env python3
"""Run isolated live DSH reply-habits experiment cells through local OneBot."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / ".runtime/reply-habits-20261029"
FIXTURES = EXPERIMENT / "fixtures"
GROUP = "91000"  # Simulated inside the local sandbox; never a live QQ connection.
BOT = "99000"
OWNER = "1105927155"
PLATFORM = "v2-sandbox"
DEFAULT_REMINDER = "按当前人格直接回应这件事。每条QQ消息表达一个简短完整的意思；需要补充时，用qq_send_origin依次发送前面的内容，最终正文保留最后一条。已发内容从下一句续接；回应完整就结束。"
OLD_RUNNER = ROOT / ".runtime/qq-antislop-20261027/runner.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Reuse the existing isolated runtime's lifecycle and API helpers.
_old = load("reply_habits_existing_runner", OLD_RUNNER)
commands = _old.commands
runtime = _old.runtime
agent_service = _old.agent_service
helpers = commands.live_helpers


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def archive_volume(image: Path) -> dict:
    """Lossless offline sparse artifact; must be re-reserved before remounting."""
    if image.is_symlink() or image.parent.joinpath("workspace").is_mount():
        raise RuntimeError("refusing to archive a live or indirect sandbox image")
    before = sha256(image)
    archived = image.with_suffix(".sparse")
    subprocess.run(["cp", "--sparse=always", "--reflink=never", "--preserve=mode,timestamps",
                    str(image), str(archived)], check=True)
    if sha256(archived) != before or archived.stat().st_size != image.stat().st_size:
        raise RuntimeError("offline sandbox image archive changed content")
    os.replace(archived, image)
    return {"path": str(image), "sha256": before, "bytes": image.stat().st_size,
            "allocated_bytes": image.stat().st_blocks * 512,
            "offline_artifact": True, "requires_full_preallocation_before_remount": True}


def manifest() -> dict:
    p = FIXTURES / "manifest.json"
    if not p.is_file():
        raise RuntimeError(f"experiment fixtures are missing: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    required = {"production_router", "workspace", "provider", "model", "shared_profile_frozen", "journal_path", "cases", "guides"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError("manifest missing fields: " + ", ".join(sorted(missing)))
    if set(data["guides"]) != set("ABCDEF"):
        raise RuntimeError("manifest guides must define arms A through F")
    return data


def private_stage(details: dict, data: dict, arm: str, case: dict, log_file: Path) -> dict:
    """Use only this cell's staged repo; overlay frozen production code and experiment files."""
    repo = Path(details["staged_repo"])
    deploy = Path(data["production_router"]).resolve(strict=True).parent.parent
    dsh_source = (deploy / "dsh").resolve(strict=True)
    staged_dsh = repo / "dsh"
    if staged_dsh.is_symlink():
        staged_dsh.unlink()
    elif staged_dsh.exists():
        shutil.rmtree(staged_dsh)
    shutil.copytree(dsh_source, staged_dsh, symlinks=False,
                    ignore=shutil.ignore_patterns(".runtime", "node_modules", "__pycache__", "*.pyc"))
    (staged_dsh / "node_modules").symlink_to(ROOT / ".runtime/dsh-runtime/node_modules", target_is_directory=True)
    (staged_dsh / ".runtime").mkdir(mode=0o700, exist_ok=True)
    (staged_dsh / ".runtime/dsh-runtime").symlink_to(ROOT / ".runtime/dsh-runtime", target_is_directory=True)
    for name in ("dsh-home-chat", "dsh-workspace-chat", "dsh-home-owner", "dsh-workspace-owner"):
        (staged_dsh / ".runtime" / name).mkdir(mode=0o700, exist_ok=True)

    deployed_plugins = deploy / "astrbot-plugins"
    source_plugins = repo / "astrbot-plugins"
    frozen = {p.name: p for p in deployed_plugins.iterdir() if p.is_dir()}
    copied = {}
    for dst in list(source_plugins.iterdir()):
        if dst.name == "v2_sandbox_fixture":
            continue
        src = frozen.get(dst.name)
        if src is None:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            else:
                shutil.rmtree(dst)
            continue
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        copied[dst.name] = sha256(src / "__init__.py") if (src / "__init__.py").is_file() else "tree-copy"

    # AstrBot discovers plugins from its private data directory, separate from
    # the source repository's plugin tree. Mirror the same frozen deployment there.
    astrbot_plugins = Path(details["astrbot_root"]) / "data/plugins"
    astrbot_plugins.mkdir(mode=0o700, parents=True, exist_ok=True)
    for dst in list(astrbot_plugins.iterdir()):
        if dst.name == "v2_sandbox_fixture":
            continue
        if dst.name not in frozen:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            elif dst.is_dir():
                shutil.rmtree(dst)
    for name, src in frozen.items():
        dst = astrbot_plugins / name
        staged_target = source_plugins / name
        if name == "v2_sandbox_fixture":
            continue
        if name in {"v2_tasks", "v2_memory"}:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            elif dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (dst / ".v2-sandbox-snapshot").write_text(str(staged_target.resolve()), encoding="utf-8")
            continue
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.exists():
            shutil.rmtree(dst)
        dst.symlink_to(staged_target, target_is_directory=True)

    # Freeze failure semantics for result-continuation fixtures. The current
    # task service can create tasks that the historical request could not;
    # letting retries use that newer behavior would change the test's facts.
    fault = ("permission" if case["id"] in {"permission", "diary-before-send", "diary-after-send"}
             else "service" if case["id"] == "service" else None)
    if fault:
        server = source_plugins / "v2_dsh_router/mcp_server.py"
        text = server.read_text(encoding="utf-8")
        error = ('PermissionError("sandbox fixture: frozen task permission refusal")' if fault == "permission"
                 else 'RuntimeError("sandbox fixture: task service connection timed out")')
        for marker in ("                return await self.task_manager.list(turn.conversation, task_id)",
                       "                return await self.task_manager.manage("):
            if text.count(marker) != 1:
                raise RuntimeError("cannot install exact scoped task-service fixture")
            text = text.replace(marker, "                raise " + error + "\n" + marker, 1)
        server.write_text(text, encoding="utf-8")

    # Overlay the experiment plugin into the private DSH plugin area.
    experiment_plugin = ROOT / "sandbox/reply-habits-experiment.mjs"
    if not experiment_plugin.is_file():
        raise RuntimeError(f"experiment plugin not found: {experiment_plugin}")
    staged_plugin = repo / "sandbox/reply-habits-experiment.mjs"
    staged_plugin.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(experiment_plugin, staged_plugin)

    profile = staged_dsh / "profile/v2-agent-group-sandbox.patch.yml"
    if not profile.is_file():
        raise RuntimeError(f"expected group profile is missing: {profile}")
    arm_config = {
        "mode": arm,
        "seedPath": str(case["checkpoint_path"]) if case.get("checkpoint_path") else None,
        "logFile": str(log_file),
        "reminderText": data.get("reminder", data.get("reminderText", DEFAULT_REMINDER)),
    }
    # YAML uses JSON scalars, which are valid YAML and preserve arbitrary guide text safely.
    probe = repo / "sandbox/probe.mjs"
    insertion = (
        "\n- insert:\n    - id: reply-habits-experiment\n"
        f"      name: {json.dumps(str(staged_plugin))}\n      config:\n"
        f"        mode: {json.dumps(arm)}\n"
        f"        seedPath: {json.dumps(arm_config['seedPath'])}\n"
        f"        logFile: {json.dumps(str(log_file))}\n"
        f"        reminderText: {json.dumps(arm_config['reminderText'], ensure_ascii=False)}\n"
        f"    - id: v2-sandbox-probe\n      name: {json.dumps(str(probe))}\n"
        "      config:\n"
        "        controlUrl: !!js process.env.SANDBOX_CONTROL_URL\n"
        "        token: !!js process.env.SANDBOX_CONTROL_TOKEN\n"
        "        mode: !!js process.env.SANDBOX_DSH_MODE\n"
        "        snapshotMode: text\n"
        f"        artifactDirectory: {json.dumps(str(log_file.parent / 'request-snapshots'))}\n"
    )
    profile.write_text(profile.read_text(encoding="utf-8") + insertion, encoding="utf-8")
    return {"deployment_root": str(deploy), "production_dsh_source": str(dsh_source),
            "production_router": str(Path(data["production_router"]).resolve()),
            "production_plugins": sorted(copied), "experiment_plugin_sha256": sha256(experiment_plugin),
            "arm": arm, "case": case["id"], "task_service_fixture": fault}


class Cell(helpers.LiveOwner):
    def __init__(self, directory: Path, data: dict, case: dict, arm: str, stage: str, repeat: str):
        super().__init__(directory)
        self.owner = OWNER
        self.data, self.case, self.arm, self.stage, self.repeat = data, case, arm, stage, repeat
        self.key = "group:" + GROUP
        self.process = None
        self.log = None
        self.workspace = None
        self.group_volume = None

    def save(self, name: str, value: object) -> None:
        path = self.directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def start_private(self) -> None:
        details = runtime.prepare(self.directory, ROOT / ".runtime/astrbot",
                                  {"onebot": self.onebot, "ui": self.ui, "mcp": self.mcp},
                                  "live", OWNER, snapshot_mode="text")
        self.details = details
        self.production = private_stage(details, self.data, self.arm, self.case, self.directory / "experiment-plugin.jsonl")

        profile = self.data["shared_profile_frozen"]
        if isinstance(profile, dict):
            profile_path = profile.get("profile") or profile.get("profile_path")
            credentials_path = profile.get("credentials") or profile.get("credentials_path")
        else:
            profile_path = str(profile)
            credentials_path = None
        if not profile_path:
            raise RuntimeError("shared_profile_frozen must provide a profile path")
        shared_credentials = str(credentials_path) if credentials_path else None
        service_config = json.loads((ROOT / ".runtime/agent-service.json").read_text())
        if not shared_credentials:
            shared_credentials = str(Path(service_config["shared_models_home"]) / ".credentials.yaml")
        credential_file = Path(service_config["credential_env_file"])
        python = ROOT / ".runtime/astrbot/.venv/bin/python"
        read_credentials = subprocess.run(
            [str(python), "-c",
             "from dotenv import dotenv_values; import json,sys; print(json.dumps({k:v for k,v in dotenv_values(sys.argv[1]).items() if v is not None}))",
             str(credential_file)],
            cwd=ROOT / ".runtime/astrbot", capture_output=True, text=True, check=True)
        # Credentials travel only over this captured child pipe and into the
        # sandbox process environment; they are never written to experiment artifacts.
        credential_env = json.loads(read_credentials.stdout)

        # runtime.prepare intentionally strips credentials and shared profile paths. Restore only
        # the frozen model profile into this private process, as the earlier sandbox runner does.
        launch_file = self.directory / "sandbox-runtime-copy.json"
        self.save("sandbox-runtime-copy.json", details)
        childenv = os.environ.copy()
        childenv.update({"V2_DSH_PROVIDER": str(self.data["provider"]),
                         "V2_DSH_MODEL": str(self.data["model"]),
                         "V2_SHARED_MODELS_PROFILE": str(profile_path),
                         "V2_SHARED_MODELS_CREDENTIALS": shared_credentials,
                         "V2_REPLAY_SHARED_PROFILE": str(profile_path),
                         "V2_REPLAY_SHARED_CREDS": shared_credentials})
        childenv.update(credential_env)
        plug = Path(details["staged_repo"]) / "dsh/plugins/agent-profile"
        sys.path.insert(0, str(plug))
        try:
            volume = load("reply_habits_group_volume", plug / "group_volume.py")
        finally:
            sys.path.pop(0)
        self.group_volume = volume
        paths = volume.prepare_group(Path(details["staged_repo"]) / ".runtime/group-storage", PLATFORM, BOT, GROUP)
        self.workspace = Path(paths["workspace"])
        source_workspace = Path(self.data["workspace"]).resolve(strict=True)
        shutil.copytree(source_workspace, self.workspace, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        self.workspace.chmod(0o700)
        guide = Path(self.data["guides"][self.arm]).resolve(strict=True)
        shutil.copy2(guide, self.workspace / "AGENTS.md")
        self.workspace.joinpath("AGENTS.md").chmod(0o644)

        self.log = (self.directory / "runtime.log").open("ab")
        self.process = subprocess.Popen([str(ROOT / ".runtime/astrbot/.venv/bin/python"), str(Path(__file__).resolve()),
                                         "--launch", str(launch_file)], cwd=ROOT, env=childenv,
                                        stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.http("/api/state").get("connected"), "isolated OneBot", timeout=90)
        self.wait(lambda: self.journal_path().exists(), "canonical Journal", timeout=90)
        def router_ready():
            with urlopen(f"http://127.0.0.1:{self.mcp}/healthz", timeout=3) as response:
                return json.load(response).get("ready")
        self.wait(router_ready, "DSH router and MCP ready", timeout=120)
        state = self.http("/api/scenario")
        admitted = list(state.get("admitted", []))
        group_key = "group:" + GROUP
        if group_key not in admitted:
            self.request("PUT", "/api/scenario", {"revision": state["revision"], "admitted": [*admitted, group_key]})
        imported_rows = self.import_source_journal() if self.case["id"] in {"diary-before-send", "diary-after-send"} else []
        self.register_fixture_members(imported_rows)

    def register_fixture_members(self, rows: list[dict]) -> None:
        actors = {str(row["actor_id"]): str(row.get("actor_name") or row["actor_id"])
                  for row in rows if str(row.get("actor_id")) != str(row.get("bot_id"))}
        actors[OWNER] = "测试成员"
        current = {str(member["id"]) for member in self.http("/api/state").get("members", [])}
        for actor, name in actors.items():
            if actor not in current:
                self.request("POST", "/api/members", {"id": actor, "nickname": name[:60]})

    def import_source_journal(self) -> list[dict]:
        source_rows = json.loads(Path(self.data["journal_path"]).read_text(encoding="utf-8"))
        limit = 34192 if self.case["id"] == "diary-before-send" else 34197
        source_rows = [row for row in source_rows if int(row["journal_id"]) <= limit]
        imported_ids = {int(row["journal_id"]) for row in source_rows}
        expected_boundary = limit
        if expected_boundary not in imported_ids:
            raise RuntimeError(f"source Journal fixture lacks expected checkpoint boundary {expected_boundary}")
        excluded = {34197, 34198} if limit == 34192 else {34198}
        if imported_ids & excluded:
            raise RuntimeError("future source Journal rows would leak into checkpoint history")
        db = sqlite3.connect(self.journal_path(), timeout=20)
        if db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]:
            db.close()
            raise RuntimeError("refusing source Journal import into a non-empty sandbox Journal")
        imported = []
        for row in source_rows:
            content = _old.imported_content(row["content_json"], str(row["bot_id"]))
            db.execute(
                """INSERT INTO journal(journal_id,platform_id,bot_id,conversation_key,conversation_type,
                   direction,actor_id,actor_name,source,message_id,reply_to,content_json,created_at,observed_at_ns)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["journal_id"], PLATFORM, BOT, "group:" + GROUP, "group", row["direction"],
                 BOT if str(row["actor_id"]) == str(row["bot_id"]) else row["actor_id"], row["actor_name"],
                 "reply_habits_source_fixture", row["message_id"], row["reply_to"], content,
                 row["created_at"], row["observed_at_ns"]),
            )
            imported.append(row["journal_id"])
        db.commit()
        db.close()
        self.journal_import = {"count": len(imported), "journal_ids": imported,
                  "preserved_original_ids": True, "source_group": self.data.get("source_group"),
                  "sandbox_group": GROUP, "upper_bound": limit,
                  "boundary_present": expected_boundary in imported_ids,
                  "excluded_future_ids": sorted(excluded), "excluded_future_ids_absent": True}
        self.save("source-journal-import.json", self.journal_import)
        return source_rows

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def run(self) -> dict:
        try:
            self.start_private()
            # Use one input submission per cell. Never resubmit after timeout or an ambiguous failure.
            before_out = {str(e["message_id"]) for e in self.events() if e["direction"] == "outbound"}
            before_models = sum(r.get("event") == "model/start" for r in self.evidence()["lifecycle"])
            text = self.case["text"]
            event = self.request("POST", "/api/send", {"conversation": {"type": "group", "id": GROUP},
                               "sender_id": OWNER, "message": [{"type": "at", "data": {"qq": BOT}},
                               {"type": "text", "data": {"text": text}}]})["event"]
            self.wait(lambda: any(str(r["message_id"]) == str(event["message_id"]) for r in self.rows()),
                      "input Journal row", timeout=30)
            self.wait(lambda: sum(r.get("event") == "model/start" for r in self.evidence()["lifecycle"]) > before_models,
                      "model start", timeout=360)
            self.wait(lambda: any(r.get("event") == "turn/completed" for r in self.trace(event)),
                      "turn completed", timeout=360)
            ev = self.request("GET", "/api/evidence/export")
            outputs = [e for e in self.events() if e["direction"] == "outbound"
                       and e["conversation"] == event["conversation"] and str(e["message_id"]) not in before_out]
            if not outputs:
                raise RuntimeError("completed turn has no outbound messages")
            joined = []
            for out in outputs:
                receipt = [r for r in self.trace(event) if r.get("event") == "send/receipt"
                           and str(r.get("message_id")) == str(out["message_id"])]
                journal = [r for r in self.rows() if r.get("direction") == "outbound"
                           and str(r.get("message_id")) == str(out["message_id"])]
                if len(receipt) != 1 or len(journal) != 1:
                    raise RuntimeError(f"send receipt / Journal join failed for {out['message_id']}")
                joined.append({"message_id": out["message_id"], "message": out["event"].get("message"),
                               "receipt_count": len(receipt), "journal_count": len(journal)})
            result = {"case": self.case["id"], "stage": self.stage, "arm": self.arm, "repeat": self.repeat,
                      "input": event, "outbound": outputs, "receipt_journal_joins": joined,
                      "native_lifecycle": [r for r in ev["lifecycle"] if r.get("event") == "model/start"],
                      "request_snapshots": str(self.directory / "request-snapshots"),
                      "source_journal_import": getattr(self, "journal_import", None),
                      "production_snapshot": self.production,
                      "simulated_group_id": GROUP, "real_qq_connected": False}
            self.save("result.json", result)
            return result
        except Exception as exc:
            self.save("failure.json", {"error": type(exc).__name__, "message": str(exc),
                                        "traceback": traceback.format_exc()})
            raise
        finally:
            process = self.process
            cleanup = {"supervisor_pid": process.pid if process else None}
            try:
                self.stop()
                cleanup["supervisor_exited"] = process is None or process.poll() is not None
                if not cleanup["supervisor_exited"]:
                    raise RuntimeError("sandbox supervisor did not exit")
                cleanup["workspace_mount_before_cleanup"] = bool(self.workspace and self.workspace.is_mount())
                if self.workspace and self.workspace.is_mount():
                    self.group_volume.unmount_group(self.workspace)
                mounted = bool(self.workspace and self.workspace.is_mount())
                cleanup["workspace_mounted_after_cleanup"] = mounted
                if mounted:
                    raise RuntimeError("sandbox group volume remains mounted after cleanup")
                if self.workspace:
                    image = self.workspace.parent / "workspace.ext4"
                    if image.is_file() and not image.is_symlink():
                        artifact = archive_volume(image)
                        self.save("offline-volume-artifact.json", artifact)
                        cleanup["volume_archive"] = artifact
                    else:
                        cleanup["volume_archive"] = None
                else:
                    cleanup["volume_archive"] = None
                cleanup["success"] = True
                self.save("cleanup-result.json", cleanup)
            except Exception as cleanup_error:
                cleanup.update({"cleanup_succeeded": False,
                                "error": type(cleanup_error).__name__,
                                "message": str(cleanup_error),
                                "traceback": traceback.format_exc()})
                self.save("cleanup-failure.json", cleanup)
                raise


def launch(details_path: str) -> None:
    details = json.loads(Path(details_path).read_text(encoding="utf-8"))
    original = runtime._child_environment

    def environment(*args, **kwargs):
        value = original(*args, **kwargs)
        for key, source in (("V2_SHARED_MODELS_PROFILE", "V2_REPLAY_SHARED_PROFILE"),
                            ("V2_SHARED_MODELS_CREDENTIALS", "V2_REPLAY_SHARED_CREDS")):
            if os.environ.get(source):
                value[key] = os.environ[source]
        return value

    runtime._child_environment = environment
    raise SystemExit(runtime._run(details, Path(details["astrbot_source"]), details["ports"], "live", owner_volume_mib=32))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--arm", choices=list("ABCDEF"))
    parser.add_argument("--repeat", default="r0")
    parser.add_argument("--stage", choices=("screening", "holdout"), default="screening")
    parser.add_argument("--launch", metavar="DETAILS")
    parser.add_argument("--prepare-only", action="store_true", help="prepare and validate an isolated cell without starting DSH")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--scripted-preflight", action="store_true")
    args = parser.parse_args()
    if args.launch:
        launch(args.launch)
        return 0
    if args.matrix or args.scripted_preflight:
        parser.error("matrix/scripted-preflight orchestration is not implemented; select --case and --arm")
    data = manifest()
    if args.stage == "holdout":
        data = {**data, "workspace": data["holdout_workspace"], "guides": data["holdout_guides"]}
    cases = [c for c in data["cases"] if c.get("stage", "screening") == args.stage]
    case = next((c for c in cases if c["id"] == args.case), None) if args.case else None
    if not case:
        parser.error("select a valid --case for the chosen --stage")
    if not args.arm:
        parser.error("--arm A-F is required")
    if args.arm not in data["guides"]:
        parser.error("selected guide is absent from manifest")
    if case.get("checkpoint_path") and not Path(case["checkpoint_path"]).is_file():
        parser.error("checkpoint_path does not exist")
    if args.workers < 1 or args.workers > 3:
        parser.error("workers must be between 1 and 3")
    (EXPERIMENT / "runs").mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = EXPERIMENT / "runs" / args.stage / case["id"] / args.arm / args.repeat
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.exists() and any(directory.iterdir()):
        parser.error(f"cell directory already has artifacts; refusing rerun: {directory}")
    directory.mkdir(mode=0o700, exist_ok=True)
    cell = Cell(directory, data, case, args.arm, args.stage, args.repeat)
    if args.prepare_only:
        details = runtime.prepare(directory, ROOT / ".runtime/astrbot",
                                  {"onebot": cell.onebot, "ui": cell.ui, "mcp": cell.mcp},
                                  "live", OWNER, snapshot_mode="text")
        staged = private_stage(details, data, args.arm, case, directory / "experiment-plugin.jsonl")
        cell.save("prepare-validation.json", {"prepared": True, "live_model_started": False,
                                               "runtime": details, "stage": staged,
                                               "guide_sha256": sha256(Path(data["guides"][args.arm]))})
        print(json.dumps({"prepared": True, "live_model_started": False, "directory": str(directory)}, ensure_ascii=False))
        return 0
    result = cell.run()
    print(json.dumps({"status": "cell-complete", "case": case["id"], "stage": args.stage,
                      "arm": args.arm, "repeat": args.repeat, "outbound_count": len(result["outbound"]),
                      "artifact": str(directory)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
