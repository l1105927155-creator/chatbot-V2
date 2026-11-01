#!/usr/bin/env python3
"""Run one frozen reply-relevance study cell in an isolated sandbox."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OWNER = "90009"
GROUP = "91000"
BOT = "99000"
PLATFORM = "v2-sandbox"
KEY = "group:" + GROUP


def load_runtime():
    spec = importlib.util.spec_from_file_location("reply_relevance_runtime", ROOT / "sandbox/runtime.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def load_live_owner():
    path = ROOT / "scripts/check-sandbox-live-owner.py"
    spec = importlib.util.spec_from_file_location("study_live_owner_helpers", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.LiveOwner


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_tree(root: Path, max_text_bytes: int = 128 * 1024) -> dict[str, dict]:
    """Inventory a workspace without following links; retain small text files privately."""
    root = Path(root)
    result = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        kept_dirs = []
        for name in sorted(dirnames):
            path = base / name
            if path.is_symlink():
                result[path.relative_to(root).as_posix()] = {
                    "kind": "symlink", "sha256": None, "bytes": None, "text": None}
            else:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = {"kind": "symlink", "sha256": None, "bytes": None, "text": None}
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            row = {"kind": "file", "sha256": digest(path), "bytes": size,
                   "text": None, "text_omitted": None}
            if size <= max_text_bytes:
                raw = path.read_bytes()
                try:
                    decoded = raw.decode("utf-8")
                    if "\0" not in decoded:
                        row["text"] = decoded
                        row["text_omitted"] = False
                    else:
                        row["text_omitted"] = "binary"
                except UnicodeDecodeError:
                    row["text_omitted"] = "binary"
            else:
                try:
                    sample = path.open("rb").read(4096).decode("utf-8")
                    row["text_omitted"] = "binary" if "\0" in sample else "per_file_limit"
                except UnicodeDecodeError:
                    row["text_omitted"] = "binary"
            result[relative] = row
    return dict(sorted(result.items()))


def private_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)


def private_write_if_absent(path: Path, raw: bytes) -> None:
    try:
        private_write(path, raw)
    except FileExistsError:
        pass


def proc_starttime(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        tail = stat[stat.rfind(")") + 2:].split()
        return tail[19]  # field 22; tail begins at field 3
    except (OSError, IndexError):
        return None


def process_descendants(root_pid: int) -> dict[str, str]:
    """Capture descendants by PID and kernel starttime, avoiding PID reuse ambiguity."""
    found: dict[str, str] = {}
    pending = [root_pid]
    seen = {root_pid}
    while pending:
        parent = pending.pop()
        try:
            raw = Path(f"/proc/{parent}/task/{parent}/children").read_text()
        except OSError:
            continue
        for token in raw.split():
            try:
                child = int(token)
            except ValueError:
                continue
            if child in seen:
                continue
            seen.add(child)
            starttime = proc_starttime(child)
            if starttime is not None:
                found[str(child)] = starttime
                pending.append(child)
    return found


def surviving_descendants(captured: dict[str, str]) -> dict[str, str]:
    return {pid: starttime for pid, starttime in captured.items()
            if proc_starttime(int(pid)) == starttime}


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings_in(child)


def load_credential_env(path: Path) -> None:
    """Load KEY=VALUE lines without ever copying them into study artifacts."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or name not in {"DEEPSEEK_API_KEY", "V2_SHARED_MODELS_CREDENTIALS"}:
            raise ValueError("credential env file accepts only DEEPSEEK_API_KEY and V2_SHARED_MODELS_CREDENTIALS")
        os.environ[name] = value.strip().strip("\"'")


def assert_frozen(manifest: dict) -> None:
    hashes = manifest.get("frozen_hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("manifest frozen_hashes must be a non-empty absolute-path map")
    for raw, expected in hashes.items():
        path = Path(raw)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise RuntimeError(f"frozen input is missing, indirect, or not a file: {path}")
        if digest(path) != expected:
            raise RuntimeError(f"frozen input hash changed: {path}")


def stage_frozen(details: dict, manifest: dict, cell: dict, mode: str) -> None:
    repo = Path(details["staged_repo"])
    source = Path(manifest["runtime_source"]).resolve(strict=True)
    dsh_source, plugins_source = source / "dsh", source / "astrbot-plugins"
    if not dsh_source.is_dir() or not plugins_source.is_dir():
        raise RuntimeError("runtime_source must contain dsh/ and astrbot-plugins/")

    target = repo / "dsh"
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(dsh_source, target, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".runtime"))
    for path in (target / "node_modules", target / ".runtime"):
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    fixed_modules = ROOT / ".runtime/dsh-runtime/node_modules"
    if not fixed_modules.is_dir():
        raise RuntimeError(f"pinned DSH node_modules are unavailable: {fixed_modules}")
    (target / "node_modules").symlink_to(fixed_modules, target_is_directory=True)
    (target / ".runtime").mkdir(mode=0o700)
    (target / ".runtime/dsh-runtime").symlink_to(ROOT / ".runtime/dsh-runtime", target_is_directory=True)

    staged_plugins = repo / "astrbot-plugins"
    fixture_source = staged_plugins / "v2_sandbox_fixture"
    fixture_backup = repo / "sandbox/fixture-plugin-snapshot"
    if fixture_source.is_dir() and not fixture_backup.exists():
        shutil.copytree(fixture_source, fixture_backup)
    if staged_plugins.is_symlink():
        staged_plugins.unlink()
    elif staged_plugins.exists():
        shutil.rmtree(staged_plugins)
    shutil.copytree(plugins_source, staged_plugins, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not (staged_plugins / "v2_sandbox_fixture").exists() and fixture_backup.is_dir():
        shutil.copytree(fixture_backup, staged_plugins / "v2_sandbox_fixture")
    # runtime.prepare already made AstrBot plugin links; repoint them to this frozen copy.
    data_plugins = Path(details["astrbot_root"]) / "data/plugins"
    frozen_names = {plugin.name for plugin in staged_plugins.iterdir()}
    for existing in data_plugins.iterdir():
        if existing.name.startswith("v2_") and existing.name not in frozen_names:
            if existing.is_symlink() or not existing.is_dir():
                existing.unlink()
            else:
                shutil.rmtree(existing)
    for plugin in staged_plugins.iterdir():
        dest = data_plugins / plugin.name
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        if plugin.name in {"v2_tasks", "v2_memory"}:
            shutil.copytree(plugin, dest)
            (dest / ".v2-sandbox-snapshot").write_text(str(plugin.resolve()), encoding="utf-8")
        else:
            dest.symlink_to(plugin, target_is_directory=True)

    profile = Path(manifest["shared_profile"]).resolve(strict=True)
    if profile.is_symlink() or not profile.is_file():
        raise RuntimeError("shared_profile must be a regular frozen patch file")
    dest = target / "profile/v2-shared-models.patch.yml"
    shutil.copy2(profile, dest)
    dest.chmod(0o600)
    guide = Path(manifest["guides"][cell["arm"]]).resolve(strict=True)
    workspace = Path(details["staged_repo"])
    shutil.copy2(guide, workspace / "AGENTS.md")

    # Restore the exact sandbox probe stanza that _stage_dsh_repo appends to the
    # standard group patch. The frozen runtime's profile gets only this fixture.
    group_patch = target / "profile/v2-agent-group-sandbox.patch.yml"
    if not group_patch.exists():
        raise RuntimeError("frozen DSH runtime is missing group sandbox patch")
    probe = workspace / "sandbox/probe.mjs"
    text = group_patch.read_text(encoding="utf-8")
    text += (
        "\n- insert:\n"
        "    - id: v2-sandbox-probe\n"
        f"      name: {json.dumps(str(probe))}\n"
        "      config:\n"
        "        controlUrl: !!js process.env.SANDBOX_CONTROL_URL\n"
        "        token: !!js process.env.SANDBOX_CONTROL_TOKEN\n"
        "        mode: !!js process.env.SANDBOX_DSH_MODE\n"
        "        snapshotMode: text\n"
        "        snapshotMetadata: {studyCell: " + json.dumps(cell["id"]) + "}\n"
        f"        artifactDirectory: {json.dumps(str(Path(manifest['_run_dir']) / 'request-snapshots'))}\n"
    )
    group_patch.write_text(text, encoding="utf-8")


class Cell(load_live_owner()):
    def __init__(self, directory: Path, details: dict, mode: str):
        super().__init__(directory)
        self.directory, self.details, self.mode = directory, details, mode
        self.manifest_provider = None
        self.manifest_model = None
        self.ports = details["ports"]
        self.base = f"http://127.0.0.1:{self.ports['ui']}"
        self.onebot, self.ui, self.mcp = (self.ports[k] for k in ("onebot", "ui", "mcp"))
        self.process = None
        self.log = None
        self.group_workspace = None
        self.group_volume_module = None
        self.cleanup = None

    def request(self, method: str, path: str, data=None, timeout=20):
        req = Request(self.base + path,
                      data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
                      method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"{method} {path} HTTP {exc.code}: {exc.read().decode()[:1000]}") from exc

    def wait(self, predicate, label, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"runtime exited while waiting for {label}; see runtime.log")
            try:
                value = predicate()
                if value:
                    return value
            except (URLError, OSError, sqlite3.OperationalError):
                pass
            time.sleep(.2)
        raise TimeoutError(f"timed out waiting for {label}")

    def start(self):
        cmd = [sys.executable, str(Path(__file__).resolve()), "--launch",
               str(self.directory / "launch.json")]
        inherited = {"PATH", "LANG", "TERM", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                     "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "V2_DSH_PROVIDER", "V2_DSH_MODEL",
                     "DEEPSEEK_API_KEY", "V2_SHARED_MODELS_PROFILE", "V2_SHARED_MODELS_CREDENTIALS",
                     "V2_GROUP_OWNER_WRITE_DEMOTIONS"}
        inherited.update(key for key in os.environ if key.startswith("LC_"))
        launch_env = {key: os.environ[key] for key in inherited if os.environ.get(key)}
        self.log = (self.directory / "runtime.log").open("ab")
        self.process = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True, env=launch_env)
        self.wait(lambda: self.request("GET", "/api/state", timeout=3).get("connected"), "runtime ready")
        self.wait(lambda: self.health().get("ready"), "execution domains ready")

    def health(self):
        with urlopen(f"http://127.0.0.1:{self.ports['mcp']}/healthz", timeout=3) as response:
            return json.load(response)

    def stop(self):
        descendants = process_descendants(self.process.pid) if self.process else {}
        forced_signal = None
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                forced_signal = "SIGTERM"
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    forced_signal = "SIGKILL"
                    self.process.send_signal(signal.SIGKILL)
                    self.process.wait(timeout=5)
        remaining = surviving_descendants(descendants)
        self.cleanup = {"supervisor_exit": self.process.returncode if self.process else None,
                        "graceful_shutdown": forced_signal is None,
                        "forced_supervisor_signal": forced_signal,
                        "descendants_before_stop": descendants,
                        "descendants_remaining": remaining,
                        "descendants_stopped": not remaining,
                        "cleanup_ok": not remaining}
        if self.log:
            self.log.close()
            self.log = None

    def evidence(self):
        return self.request("GET", "/api/evidence/export", timeout=30)

    def journal_path(self):
        return Path(self.details["astrbot_root"]) / "data/plugin_data/v2_journal/journal.sqlite3"

    def rows(self):
        with sqlite3.connect(self.journal_path()) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM journal ORDER BY journal_id")]

    def init_journal(self, rows):
        if self.rows():
            raise RuntimeError("fresh study cell has non-empty Journal")
        with sqlite3.connect(self.journal_path()) as db:
            for row in rows:
                required = {"journal_id", "platform_id", "bot_id", "conversation_key", "conversation_type",
                            "direction", "actor_id", "actor_name", "source", "message_id", "reply_to",
                            "content_json", "created_at", "observed_at_ns"}
                if set(row) != required:
                    raise ValueError("journal fixture row must contain every canonical journal column")
                db.execute("INSERT INTO journal (" + ",".join(required) + ") VALUES (" +
                           ",".join("?" for _ in required) + ")", [row[k] for k in required])
            db.execute("UPDATE sqlite_sequence SET seq=999 WHERE name='journal'")
            if not db.execute("SELECT changes()").fetchone()[0]:
                db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('journal',999)")
            db.commit()

    def prepare_cell(self, manifest, cell):
        self.init_journal(cell["journal"])
        state = self.request("GET", "/api/state")
        members = state.get("members", [])
        member_ids = {str(row.get("id")) if isinstance(row, dict) else str(row) for row in members}
        if str(cell["owner"]) not in member_ids:
            self.request("POST", "/api/members", {"id": str(cell["owner"]), "nickname": "合成Owner"})
        self.request("PUT", "/api/scenario", {"revision": self.request("GET", "/api/scenario")["revision"],
                                                "admitted": [KEY]})
        if cell.get("preflight_command"):
            self.request("POST", "/api/scripts", {"conversation_key": KEY, "kind": "tool",
                "name": "bash", "arguments": {"command": cell["preflight_command"],
                    "description": "隔离预检：验证实际工作区写入及写权限拒绝"},
                "text_after": "PREFLIGHT_DONE"})

    def snapshot_files(self, target):
        return snapshot_tree(target)

    def send(self, text, sender=OWNER):
        return self.request("POST", "/api/send", {"conversation": {"type": "group", "id": GROUP},
            "sender_id": sender, "message": [{"type": "at", "data": {"qq": BOT}},
                                               {"type": "text", "data": {"text": text}}]})["event"]

    def run_turn(self, cell):
        self.current_cell = cell
        workspace = self.group_workspace
        if workspace is None:
            raise RuntimeError("group workspace was not initialized")
        target = workspace / cell["record_file"]
        before = self.snapshot_files(workspace)
        owner = str(cell["owner"])
        event = self.send(cell["request"], sender=owner)
        key = KEY
        try:
            terminal = self.wait(lambda: next((r for r in self.evidence().get("audit", [])
                if r.get("conversation_key") == key
                and str(r.get("inbound_message_id")) == str(event["message_id"])
                and r.get("event") in {"turn/completed", "turn/failed"}), None),
                "terminal router turn", timeout=360)
            if not terminal:
                terminal = {"event": "timeout", "deadline_seconds": 360}
        except TimeoutError:
            terminal = {"event": "timeout", "deadline_seconds": 360,
                        "inbound_message_id": str(event["message_id"])}
        exported = self.evidence()
        after = self.snapshot_files(workspace)
        changed_paths = [path for path in sorted(set(before) | set(after))
                         if before.get(path) != after.get(path)]
        guide_changed = "AGENTS.md" in changed_paths
        simulator_events = self.events()
        bubbles = [e for e in simulator_events if e.get("conversation") == {"type": "group", "id": GROUP}
                   and int(e["message_id"]) > int(event["message_id"]) and e.get("direction") == "outbound"]
        trace = [r for r in exported.get("audit", []) if r.get("conversation_key") == key
                 and str(r.get("inbound_message_id")) == str(event["message_id"])]
        native_root = self.directory / "repo/.runtime"
        native_paths = list(native_root.glob("dsh-home-group-*/**/session.v4.jsonl"))
        native_copy = []
        for path in native_paths:
            dest = self.directory / "native-sessions" / path.relative_to(self.directory / "repo")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            native_copy.append(str(dest.relative_to(self.directory)))
        snapshot_dir = self.directory / "request-snapshots"
        snapshot_paths = [str(p.relative_to(self.directory)) for p in snapshot_dir.rglob("*") if p.is_file()] if snapshot_dir.exists() else []
        expected_provider, expected_model = (("sandbox-scripted", "scripted") if self.mode == "scripted"
                                              else (self.manifest_provider, self.manifest_model))
        guide_text = before.get("AGENTS.md", {}).get("text")
        source_checks = []
        if snapshot_dir.exists():
            for artifact_path in sorted(snapshot_dir.glob("*.json")):
                try:
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                metadata = artifact.get("metadata", {})
                if metadata.get("studyCell") != cell["id"]:
                    continue
                model_facing = list(strings_in(artifact.get("system"))) + list(strings_in(artifact.get("messages")))
                assembled = list(strings_in(artifact.get("assembly")))
                source_checks.append({"artifact": artifact_path.name,
                    "provider": metadata.get("provider"), "model": metadata.get("model"),
                    "route_matches_manifest": (metadata.get("provider"), metadata.get("model")) ==
                        (expected_provider, expected_model),
                    "guide_sha256": hashlib.sha256(guide_text.encode()).hexdigest() if guide_text is not None else None,
                    "guide_text_in_model_input": bool(guide_text and any(guide_text in text for text in model_facing)),
                    "guide_text_in_assembly_observed": bool(guide_text and any(guide_text in text for text in assembled))})
        source_verification = {"expected_provider": expected_provider, "expected_model": expected_model,
            "snapshots_found": len(source_checks),
            "all_routes_match": bool(source_checks) and all(row["route_matches_manifest"] for row in source_checks),
            "guide_sha256": hashlib.sha256(guide_text.encode()).hexdigest() if guide_text is not None else None,
            "guide_verified_in_native_snapshot": bool(source_checks) and all(
                row["guide_text_in_model_input"] for row in source_checks),
            "snapshots": source_checks}
        sends = [r for r in trace if r.get("event") == "send/start"]
        receipts = [r for r in trace if r.get("event") == "send/receipt"]
        inbound_row = next((r for r in self.rows() if str(r["message_id"]) == str(event["message_id"])
                            and r["conversation_key"] == key and r["platform_id"] == PLATFORM
                            and r["bot_id"] == BOT and r["direction"] == "inbound"), None)
        outbound = []
        for bubble in bubbles:
            row = next((r for r in self.rows() if str(r["message_id"]) == str(bubble["message_id"])
                        and r["conversation_key"] == key and r["direction"] == "outbound"
                        and r["platform_id"] == PLATFORM and r["bot_id"] == BOT), None)
            receipt = next((r for r in receipts if str(r.get("message_id")) == str(bubble["message_id"])), None)
            start = next((r for r in sends if r.get("attempt_id") == (receipt or {}).get("attempt_id")), None)
            outbound.append({"bubble": bubble, "send_start": start, "receipt": receipt, "journal": row})
        terminal_attempts = {r.get("attempt_id") for r in trace
                             if r.get("event") in {"send/returned", "send/failed"}}
        attempt_ids = {r.get("attempt_id") for r in sends}
        receipts_by_attempt = {}
        receipt_identity = lambda r: (
            str(r.get("platform_id")) == PLATFORM
            and str(r.get("bot_id")) == BOT
            and r.get("conversation_key") == KEY
        )
        for receipt in receipts:
            receipts_by_attempt.setdefault(receipt.get("attempt_id"), []).append(receipt)
        bubble_ids = [str(row["message_id"]) for row in bubbles]
        receipt_ids = [str(row.get("message_id")) for row in receipts]
        delivery_complete = (bool(attempt_ids) and attempt_ids <= terminal_attempts
            and all(r.get("attempt_id") in attempt_ids and receipt_identity(r) for r in receipts)
            and all(receipts_by_attempt.get(attempt) for attempt in attempt_ids)
            and all(x["send_start"] and x["receipt"] and receipt_identity(x["receipt"])
                    and x["journal"] for x in outbound)
            and sorted(receipt_ids) == sorted(bubble_ids))
        guide_verified = source_verification["guide_verified_in_native_snapshot"]
        route_verified = source_verification["all_routes_match"]
        denial_verified = not cell["deny_writes"] or before.get(cell["record_file"]) == after.get(cell["record_file"])
        controlled_inputs = route_verified and guide_verified and denial_verified and not guide_changed
        protocol_complete = (terminal.get("event") == "turn/completed" and inbound_row and delivery_complete
                             and route_verified and guide_verified and denial_verified)
        status = "complete" if protocol_complete else "incomplete"
        result = {"cell_id": cell["id"], "mode": self.mode, "protocol_status": status,
            "terminal": terminal, "request_event": event, "request_journal": inbound_row,
            "tool_audit": trace, "send_start_receipt_journal": outbound, "bubbles": bubbles,
            "before_files": before, "after_files": after, "changed_paths": changed_paths,
            "guide_changed_during_turn": guide_changed,
            "controlled_inputs": controlled_inputs,
            "primary_contrast_eligible": status == "complete" and controlled_inputs,
            "protocol_gates": {"terminal_completed": terminal.get("event") == "turn/completed",
                "inbound_journal": bool(inbound_row), "delivery_join": delivery_complete,
                "route_verified": route_verified, "guide_verified": guide_verified,
                "deny_writes_target_unchanged": denial_verified},
            "guide_change_interpretation": ("AGENTS.md changed during this turn; this is endogenous turn behavior, "
                "not a pre-existing A/B input difference and does not automatically invalidate the cell"
                if guide_changed else None),
            "native_session_snapshots": native_copy,
            "request_snapshots": snapshot_paths, "source_verification": source_verification,
            "evidence_export": "evidence-export.json",
            "evidence": exported, "write_denial_expected": bool(cell["deny_writes"]),
            "write_target_unchanged": not cell["deny_writes"] or
                before.get(cell["record_file"]) == after.get(cell["record_file"])}
        private_write(self.directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2).encode())
        if terminal.get("event") in {"turn/failed", "timeout"}:
            private_write_if_absent(self.directory / "failure.json", json.dumps({"cell_id": cell["id"],
                "terminal": terminal, "evidence": exported}, ensure_ascii=False, indent=2).encode())
        return result


def choose_cell(manifest, cell_id):
    matches = [c for c in manifest["cells"] if c.get("id") == cell_id]
    if len(matches) != 1:
        raise ValueError(f"manifest must contain exactly one cell id {cell_id!r}")
    cell = matches[0]
    if cell.get("study") not in {"P0", "P1"} or cell.get("arm") not in {"A", "B"}:
        raise ValueError("invalid study or arm")
    if not isinstance(cell.get("repeat"), int) or cell["repeat"] < 0:
        raise ValueError("cell repeat must be a non-negative integer")
    if str(cell.get("owner")) != OWNER:
        raise ValueError("all study cells must use the startup-authorized Owner 90009")
    return cell


def run(manifest_path: Path, cell_id: str, mode: str) -> int:
    manifest_path = manifest_path.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("unsupported study manifest version")
    if manifest.get("provider") != "wb" or manifest.get("model") != "deepseek-v4.1-flash":
        raise RuntimeError("study manifest must use wb/deepseek-v4.1-flash")
    cell = choose_cell(manifest, cell_id)
    assert_frozen(manifest)
    manifest["_run_dir"] = str(manifest_path.parent / "runs" / cell_id)
    directory = Path(manifest["_run_dir"])
    parent = manifest_path.parent / "runs"
    if parent.is_symlink() or directory.exists() or directory.is_symlink():
        raise RuntimeError(f"refusing to overwrite or follow an existing run path: {directory}")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or directory.resolve(strict=False) != directory:
        raise RuntimeError("run path contains an indirect directory link")
    directory.mkdir(mode=0o700)
    cell_obj = None
    try:
        runtime = load_runtime()
        ports = {}
        for key in ("onebot", "ui", "mcp"):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            ports[key] = sock.getsockname()[1]
            sock.close()
        details = runtime.prepare(directory, ROOT / ".runtime/astrbot", ports, mode, OWNER,
                                 snapshot_mode="text")
        stage_frozen(details, manifest, cell, mode)
        # Require the requested route to be present in both the launch env and
        # native group patch; the patch is frozen and the provider/model are explicit.
        os.environ["V2_DSH_PROVIDER"] = manifest["provider"]
        os.environ["V2_DSH_MODEL"] = manifest["model"]
        os.environ["V2_SHARED_MODELS_PROFILE"] = str(Path(details["staged_repo"]) / "dsh/profile/v2-shared-models.patch.yml")
        if mode == "live" and "V2_SHARED_MODELS_CREDENTIALS" not in os.environ and "DEEPSEEK_API_KEY" not in os.environ:
            raise RuntimeError("credentials must be provided by --credentials/--credential-env-file or inherited env")
        if mode == "scripted":
            os.environ["V2_DSH_PROVIDER"] = "sandbox-scripted"
            os.environ["V2_DSH_MODEL"] = "scripted"
            if os.environ.get("REPLY_STUDY_PLAN_BASH"):
                cell["preflight_command"] = os.environ["REPLY_STUDY_PLAN_BASH"]
        if cell["deny_writes"]:
            os.environ["V2_GROUP_OWNER_WRITE_DEMOTIONS"] = json.dumps([{
                "platform_id": PLATFORM, "bot_id": BOT, "group_id": GROUP, "user_id": OWNER}], separators=(",", ":"))
        else:
            os.environ.pop("V2_GROUP_OWNER_WRITE_DEMOTIONS", None)
        launch = {"details": details, "astrbot_source": str(ROOT / ".runtime/astrbot"),
                  "ports": ports, "mode": mode}
        private_write(directory / "launch.json", json.dumps(launch).encode())
        private_write(directory / "manifest-cell.json", json.dumps({k: v for k, v in cell.items()
                      if k not in {"request", "initial_record", "journal"}}, ensure_ascii=False, indent=2).encode())
        cell_obj = Cell(directory, details, mode)
        cell_obj.manifest_provider = manifest["provider"]
        cell_obj.manifest_model = manifest["model"]
        gv_path = Path(details["staged_repo"]) / "dsh/plugins/agent-profile/group_volume.py"
        sys.path.insert(0, str(gv_path.parent))
        gv_spec = importlib.util.spec_from_file_location("reply_study_group_volume", gv_path)
        gv = importlib.util.module_from_spec(gv_spec)
        gv_spec.loader.exec_module(gv)
        cell_obj.group_volume_module = gv
        storage = Path(details["staged_repo"]) / ".runtime/group-storage"
        paths = gv.paths(storage, PLATFORM, BOT, GROUP)
        cell_obj.group_workspace = paths["workspace"]
        gv.prepare_group(storage, PLATFORM, BOT, GROUP)
        shutil.copytree(Path(manifest["workspace"]).resolve(strict=True), paths["workspace"],
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", ".runtime", "__pycache__", "*.pyc"))
        shutil.copy2(Path(manifest["guides"][cell["arm"]]), paths["workspace"] / "AGENTS.md")
        if cell.get("initial_record") is not None:
            record = paths["workspace"] / cell["record_file"]
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(cell["initial_record"], encoding="utf-8")
        cell_obj.start()
        cell_obj.prepare_cell(manifest, cell)
        result = cell_obj.run_turn(cell)
        private_write(directory / "evidence-export.json", json.dumps(result["evidence"], ensure_ascii=False, indent=2).encode())
        return 0 if result["protocol_status"] == "complete" else 2
    except Exception as exc:
        failure = {"cell_id": cell_id, "mode": mode, "error_type": type(exc).__name__,
                   "error": str(exc), "traceback": traceback.format_exc()}
        try:
            if cell_obj:
                failure["evidence"] = cell_obj.evidence()
        except Exception as evidence_exc:
            failure["evidence_error"] = str(evidence_exc)
        private_write_if_absent(directory / "failure.json",
            json.dumps(failure, ensure_ascii=False, indent=2).encode())
        return 1
    finally:
        if cell_obj:
            cell_obj.stop()
            vol = Path(details["staged_repo"]) / ".runtime/group-storage"
            if cell_obj.cleanup and cell_obj.cleanup["descendants_remaining"]:
                cell_obj.cleanup.update({"group_mounts_unmounted": False,
                                         "volume_archive_sha256": None,
                                         "volume_archive_allocated_bytes": None})
                private_write_if_absent(directory / "cleanup.json",
                    json.dumps(cell_obj.cleanup, indent=2).encode())
                raise RuntimeError("runtime supervisor left recorded child processes running; refusing volume cleanup/archive")
            mounts = [p / "workspace" for p in vol.glob("*") if os.path.ismount(p / "workspace")]
            # The helper targets Owner volume; unmount this instance's group volumes directly.
            for mount in mounts:
                if os.path.ismount(mount):
                    cell_obj.group_volume_module.unmount_group(mount)
            image = next(vol.glob("*/workspace.ext4"), None)
            if image and image.is_file() and not image.is_symlink():
                sparse = image.with_suffix(".sparse")
                checksum = digest(image)
                subprocess.run(["cp", "--sparse=always", "--reflink=never", "--preserve=mode,timestamps",
                                str(image), str(sparse)], check=True)
                if digest(sparse) != checksum or sparse.stat().st_size != image.stat().st_size:
                    raise RuntimeError("offline group volume archive failed verification")
                os.replace(sparse, image)
            cleanup = cell_obj.cleanup or {}
            cleanup.update({"group_mounts_unmounted": not any(os.path.ismount(p / "workspace") for p in vol.glob("*")),
                "volume_archive_sha256": digest(image) if image and image.is_file() else None,
                "volume_archive_allocated_bytes": image.stat().st_blocks * 512 if image and image.is_file() else None})
            private_write_if_absent(directory / "cleanup.json", json.dumps(cleanup, indent=2).encode())


def launch(path: Path) -> int:
    details = json.loads(path.read_text(encoding="utf-8"))
    runtime = load_runtime()
    original = runtime._child_environment
    allowed = ("V2_SHARED_MODELS_PROFILE", "V2_SHARED_MODELS_CREDENTIALS",
               "V2_GROUP_OWNER_WRITE_DEMOTIONS")

    def study_child_environment(*args, **kwargs):
        child = original(*args, **kwargs)
        child.update({key: os.environ[key] for key in allowed if os.environ.get(key)})
        return child

    runtime._child_environment = study_child_environment
    return runtime._run(details["details"], Path(details["astrbot_source"]), details["ports"], details["mode"])


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path)
    p.add_argument("--cell")
    p.add_argument("--mode", choices=("live", "scripted"), default="live")
    p.add_argument("--credentials", type=Path, help="shared-model credentials JSON path")
    p.add_argument("--credential-env-file", type=Path)
    p.add_argument("--plan-bash", help="scripted preflight bash command")
    p.add_argument("--launch", type=Path, help=argparse.SUPPRESS)
    return p


def main():
    args = parser().parse_args()
    if args.launch:
        return launch(args.launch)
    if not args.manifest or not args.cell:
        parser().error("--manifest and --cell are required")
    if args.credential_env_file:
        load_credential_env(args.credential_env_file)
    if args.credentials:
        os.environ["V2_SHARED_MODELS_CREDENTIALS"] = str(args.credentials.resolve(strict=True))
    if args.mode == "scripted" and args.plan_bash:
        os.environ["REPLY_STUDY_PLAN_BASH"] = args.plan_bash
    return run(args.manifest, args.cell, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
