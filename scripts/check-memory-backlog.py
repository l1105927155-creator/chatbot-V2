#!/usr/bin/env python3
"""Run frozen production memory backlog through the complete isolated /memory path.

The frozen inputs and all generated evidence are private runtime artifacts. A failed
attempt is recorded and stops; pass --resume to start another explicit attempt.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import gc
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))
parse_model_json = json.loads
DEFAULT_ACCEPTANCE = ROOT / ".runtime/memory-backlog-acceptance-20261102"
DEFAULT_RUN = ROOT / ".runtime/mb-1102"
SANDBOX_IDENTITY = ("v2-sandbox", "99000", "group:91000")
PROVIDER = "wb"
MODEL = "deepseek-v4.1-flash"
OWNER = "90009"
GROUP = "group:91000"
REQUIRED_ROWS = 3881
REQUIRED_MEMORIES = 102



@contextmanager
def sqlite_connection(*args, **kwargs):
    """Commit or roll back work and release the descriptor before unmounting."""
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def private_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    path.chmod(0o600)


def frozen_snapshot(acceptance: Path) -> tuple[dict, list[dict]]:
    manifest_path = acceptance / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("group"), manifest.get("after_id"), manifest.get("through_id"),
            manifest.get("pending_messages"), manifest.get("existing_memories")) != (
            "697823265", 38048, 46211, REQUIRED_ROWS, REQUIRED_MEMORIES):
        raise ValueError("frozen acceptance manifest does not match the approved backlog")
    for name, digest in manifest.get("files", {}).items():
        path = acceptance / name
        # SQLite creates/updates -shm merely by opening a database. The empty WAL
        # and main database are the durable frozen input; -shm is disposable cache.
        if name.endswith("-shm"):
            continue
        if name.endswith("-wal") and (not path.exists() or path.stat().st_size == 0):
            if digest != hashlib.sha256(b"").hexdigest():
                raise ValueError("frozen SQLite WAL was expected to be empty")
            continue
        if not path.is_file() or sha256(path) != digest:
            raise ValueError(f"frozen input fingerprint mismatch: {name}")
    with sqlite_connection((acceptance / "journal.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM journal WHERE platform_id=? AND bot_id=? AND conversation_key=? "
            "AND journal_id>? AND journal_id<=? ORDER BY journal_id",
            (*manifest["identity"], manifest["after_id"], manifest["through_id"]))]
    if len(rows) != REQUIRED_ROWS:
        raise ValueError(f"frozen Journal row count changed: {len(rows)}")
    return manifest, rows


def validate_success_coverage(rows: list[dict], batches: list[dict], cursor: int,
                              frozen_through: int) -> None:
    """Require every frozen Journal row to be inside an actually completed range."""
    successful = [(row.get("after_id"), row.get("through_id")) for row in batches
                  if row.get("status") == "completed"]
    if cursor < frozen_through:
        raise AssertionError(f"cursor {cursor} has not reached frozen high water {frozen_through}")
    for row in rows:
        jid = row["journal_id"]
        if not any(type(after) is int and type(through) is int and after < jid <= through
                   for after, through in successful):
            raise AssertionError(f"frozen Journal row {jid} is not covered by a completed batch")


def audit_response_commit(call: dict, revisions: list[dict], source_rows: dict) -> None:
    """Compare captured model changes to every persisted change, independently."""
    prompt = _call_payload(call)
    index = {}
    for row in prompt['messages']:
        for ref in row.get('evidence', []):
            index[ref['evidence_id']] = {**ref, 'journal_id':_message_journal_id(row),
                                         'actor_id':row.get('actor_id'),
                                         'direction':row.get('direction')}
    for memory in prompt['existing_memories']:
        for ref in memory.get('evidence', []):
            index[ref['evidence_id']] = ref
    output = parse_model_json(call['text'])
    expected = []
    for change in output['changes']:
        claims = []
        for claim in change['claims']:
            sources = []
            seen = set()
            for key in claim['sources']:
                if type(key) not in (int, str):
                    raise AssertionError('model evidence reference has wrong JSON type')
                ref = index[key]
                if type(key) is not type(ref['evidence_id']):
                    raise AssertionError('model evidence reference differs from prompt ID type')
                source_id = ref['journal_id']
                source_row = source_rows[source_id]
                if isinstance(ref.get('quote'), str):
                    source = {'journal_id':source_id,'quote':ref['quote'].strip(),
                              'actor_id':source_row['actor_id'],'direction':source_row['direction']}
                elif isinstance(ref.get('observation'), dict):
                    source = {'journal_id':source_id,'observation':ref['observation'],
                              'actor_id':source_row['actor_id'],'direction':source_row['direction']}
                else:
                    raise AssertionError('model source has no literal quote or typed observation')
                pair = json.dumps(source,ensure_ascii=False,sort_keys=True)
                if pair in seen:
                    continue
                seen.add(pair)
                sources.append(source)
            claims.append({'text':claim['text'].strip(),'sources':sources})
        expected.append({'id':change.get('id') or 'new', 'content':change['content'].strip(),
                         'members':sorted({member.strip() for member in change['members']}),
                         'weight':change['weight'],'status':change.get('status','active'),'claims':claims})
    actual = []
    for revision in revisions:
        memory = json.loads(revision['after_json'])
        actual.append({key:('new' if revision['before_json'] is None else memory['id']) if key=='id'
                       else memory[key] for key in ('id','content','members','weight','status','claims')})
    normalize = lambda items:Counter(json.dumps(item,ensure_ascii=False,sort_keys=True) for item in items)
    if normalize(expected) != normalize(actual):
        raise AssertionError(f"model response and committed memory changes differ for call {call['id']}")


def _call_payload(call: dict) -> dict:
    try:
        return json.loads(call["messages"][0]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise AssertionError(f"model call {call.get('id')} has no auditable JSON prompt") from error


def _call_completed(call: dict) -> bool:
    return call.get("status") == "completed" and call.get("finish", {}).get("kind") == "stop"


def _message_journal_id(message: dict) -> int:
    """Read only explicit source identity fields; never infer identity from a quote."""
    value = message.get("journal_id")
    if type(value) is int and value > 0:
        return value
    ref = message.get("message_ref")
    if isinstance(ref, str) and ref.startswith("journal:"):
        suffix = ref[len("journal:"):]
        if suffix.isdecimal():
            parsed = int(suffix)
            if parsed > 0 and str(parsed) == suffix:
                return parsed
    raise AssertionError("model prompt message has no canonical Journal identity")


def _call_timestamp(call: dict, key: str) -> float:
    value = call.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssertionError(f"model call {call.get('id')} has no numeric {key}")
    # The observer stores JavaScript Date.now() milliseconds. Accept seconds too
    # for old synthetic/private captures, without confusing their ordering.
    return float(value) / 1000 if value > 100_000_000_000 else float(value)


def _commit_timestamp(commit: dict) -> float:
    from datetime import datetime
    value = commit.get("created_at")
    if not isinstance(value, str):
        raise AssertionError(f"commit {commit.get('revision')} has no timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as error:
        raise AssertionError(f"commit {commit.get('revision')} has an invalid timestamp") from error


def _protected_change(change: dict) -> dict:
    return {"id": change.get("id"), "members": change.get("members"),
            "weight": change.get("weight"), "status": change.get("status", "active")}


def _review_source_index(prompt: dict, source_rows: dict) -> dict:
    """Resolve local review refs through explicit message_ref and verify Journal facts."""
    evidence_module = load("backlog_review_evidence", ROOT / "astrbot-plugins/v2_memory/evidence.py")
    result = {}
    for message in prompt.get("messages", []):
        jid = _message_journal_id(message)
        row = source_rows.get(jid)
        if row is None:
            raise AssertionError(f"fact-review message references unknown Journal row {jid}")
        if (message.get("actor_id"), message.get("direction")) != (row.get("actor_id"), row.get("direction")):
            raise AssertionError(f"fact-review message identity differs from Journal row {jid}")
        expected_time = evidence_module.format_message_time(row.get("created_at"))
        if message.get("occurred_at") != expected_time:
            raise AssertionError(f"fact-review message time differs from Journal row {jid}")
        content = json.loads(row["content_json"])
        for item in message.get("evidence", []):
            if not isinstance(item, dict):
                raise AssertionError("fact-review evidence entry is not an object")
            key = item.get("evidence_id")
            if type(key) is not int or key < 1 or key in result:
                raise AssertionError("fact-review evidence reference is not a unique positive integer")
            common = {"journal_id": jid, "actor_id": row["actor_id"],
                      "direction": row["direction"]}
            if set(item) == {"evidence_id", "quote", "kind"} and isinstance(item.get("quote"), str):
                source = {**common, "quote": item["quote"].strip()}
            elif (set(item) == {"evidence_id", "observation", "kind"}
                  and item.get("kind") == "host_observation"
                  and isinstance(item.get("observation"), dict)):
                source = {**common, "observation": item["observation"]}
            else:
                raise AssertionError("fact-review evidence has an invalid quote or typed-observation shape")
            check_source = {**source, "quote": item["quote"]} if "quote" in source else source
            if not evidence_module.source_evidence_is_current(content, check_source):
                raise AssertionError(f"fact-review evidence is not present in canonical Journal row {jid}")
            result[key] = source
    return result


def _reviewed_change_to_stored(change: dict, prompt: dict, source_rows: dict) -> dict:
    evidence_index = _review_source_index(prompt, source_rows)
    claims = []
    for claim in change.get("claims", []):
        sources, seen = [], set()
        for ref in claim.get("sources", []):
            if type(ref) is not int or ref not in evidence_index:
                raise AssertionError("fact-review output used an invalid local source reference")
            source = evidence_index[ref]
            key = json.dumps(source, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                sources.append(source)
        claims.append({"text": claim["text"].strip(), "sources": sources})
    return {"id": change.get("id") or "new", "content": change["content"].strip(),
            "members": sorted({member.strip() for member in change["members"]}),
            "weight": change["weight"], "status": change.get("status", "active"),
            "claims": claims}


def audit_review_commit(generation: dict, reviews: list[dict], revisions: list[dict],
                        source_rows: dict) -> dict:
    """Audit one generation plus its N single-memory reviews against one commit."""
    generated = parse_model_json(generation["text"]).get("changes")
    if not isinstance(generated, list):
        raise AssertionError("generation response has no changes array")
    if len(reviews) != len(generated):
        raise AssertionError("fact-review response count differs from generated change count")

    draft_counts = Counter()
    expected = []
    review_ids = []
    for call in sorted(reviews, key=lambda item: _call_timestamp(item, "started_at")):
        prompt = _call_payload(call)
        drafts = prompt.get("draft_changes")
        final = parse_model_json(call["text"]).get("changes")
        if not isinstance(drafts, list) or len(drafts) != 1:
            raise AssertionError("each fact-review call must contain exactly one draft memory")
        if not isinstance(final, list) or len(final) != 1:
            raise AssertionError("each fact-review response must contain exactly one memory")
        draft, change = drafts[0], final[0]
        if _protected_change(draft) != _protected_change(change):
            raise AssertionError("fact review changed id, members, weight, or status")
        draft_counts[json.dumps(_protected_change(draft), ensure_ascii=False, sort_keys=True)] += 1
        expected.append(_reviewed_change_to_stored(change, prompt, source_rows))
        review_ids.append(call["id"])
    generated_counts = Counter(json.dumps(_protected_change(item), ensure_ascii=False, sort_keys=True)
                               for item in generated)
    if draft_counts != generated_counts:
        raise AssertionError("fact-review drafts do not preserve the generated memory set")

    actual = []
    for revision in revisions:
        memory = json.loads(revision["after_json"])
        actual.append({key: ("new" if revision["before_json"] is None else memory["id"])
                       if key == "id" else memory[key]
                       for key in ("id", "content", "members", "weight", "status", "claims")})
    normalize = lambda items: Counter(json.dumps(item, ensure_ascii=False, sort_keys=True)
                                      for item in items)
    if normalize(expected) != normalize(actual):
        raise AssertionError("fact-review responses and committed memory changes differ")
    return {"review_call_ids": review_ids, "review_count": len(reviews),
            "reviewed_changes": len(expected)}


def install_commit_audit(path: Path) -> None:
    """Sandbox-only atomic observations; the production schema is unchanged."""
    with sqlite_connection(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS acceptance_commits(
            revision INTEGER PRIMARY KEY, after_id INTEGER, through_id INTEGER,
            observed_by TEXT, created_at TEXT);
        CREATE TRIGGER IF NOT EXISTS acceptance_cursor_commit AFTER UPDATE OF cursor ON workspace
        WHEN NEW.cursor != OLD.cursor
        BEGIN INSERT INTO acceptance_commits VALUES(NEW.revision,OLD.cursor,NEW.cursor,
             'sqlite_commit_trigger',strftime('%Y-%m-%dT%H:%M:%fZ','now')); END;
        """)


def _safe_copy_credentials(source: Path, destination: Path) -> None:
    """Make a temporary, mode-0600 copy so DSH never opens the production file."""
    source_meta = source.stat(follow_symlinks=False)
    if not source.is_file() or source.is_symlink() or source_meta.st_uid != os.getuid():
        raise PermissionError("shared-model credentials must be an owned regular file")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    destination.chmod(0o600)


def _profile_model(profile: Path, provider: str, model: str) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to verify the shared model profile") from exc
    value = yaml.safe_load(profile.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("shared model profile is not a patch list")
    default = next((entry.get("config") for entry in value
                    if isinstance(entry, dict) and entry.get("id") == "agent-default-model"), None)
    llm = next((entry.get("config") for entry in value
                if isinstance(entry, dict) and entry.get("id") == "llm-pi-ai"), None)
    models = (((llm or {}).get("providers") or {}).get(provider) or {}).get("models")
    names = {row.get("id") for row in models or [] if isinstance(row, dict)}
    if not (isinstance(default, dict) and default.get("provider") == provider
            and isinstance(default.get("model"), str) and default["model"] in names
            and model in names):
        raise ValueError(f"profile does not expose requested route {provider}/{model}")


MEMORY_TAP = r'''import { mkdirSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
export const name = 'isolated-memory-observer'
export const inject = ['llm']
export function apply(ctx, config) {
  ctx.on('llm/stream', (options, next) => {
    if (options.sessionId) return next()
    return (async function* () {
      mkdirSync(config.directory, {recursive:true,mode:0o700})
      const id = randomUUID(), path = join(config.directory, id+'.json')
      const record = {id, provider:options.provider, model:options.model,
        started_at:Date.now(), system:options.system, messages:options.messages,
        maxTokens:options.maxTokens, text:'', status:'running'}
      const save = () => writeFileSync(path, JSON.stringify(record), {mode:0o600})
      save()
      try {
        for await (const chunk of next()) {
          if(chunk.type === 'text-delta') record.text += chunk.text
          if(chunk.type === 'usage') record.usage = chunk.usage
          if(chunk.type === 'finish') record.finish = chunk.reason
          yield chunk
        }
        record.status = 'completed'
      } catch(error) {
        record.status='failed'; record.error_type=error.name
        throw error
      } finally { record.finished_at=Date.now(); save() }
    })()
  })
}
'''


def frozen_import_rows(acceptance: Path, manifest: dict, pending: list[dict]) -> list[dict]:
    source_ids = set()
    with sqlite_connection(acceptance / "memory.sqlite3") as db:
        for sources, claims in db.execute("SELECT source_ids_json,claims_json FROM memories"):
            source_ids.update(json.loads(sources))
            for claim in json.loads(claims or '[]') or []:
                source_ids.update(source['journal_id'] for source in claim['sources'])
    result = {row['journal_id']: row for row in pending}
    with sqlite_connection((acceptance / "journal.sqlite3").resolve().as_uri()+"?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for source_id in source_ids:
            row = db.execute("SELECT * FROM journal WHERE journal_id=? AND platform_id=? AND bot_id=? AND conversation_key=?",
                             (source_id, *manifest['identity'])).fetchone()
            if row is None:
                raise ValueError(f"prior memory source unavailable in frozen group: {source_id}")
            result[source_id] = dict(row)
    return sorted(result.values(), key=lambda row: row['journal_id'])


# Imported after helper declarations to keep the pure helper tests independent of
# AstrBot, DSH and sandbox startup.
class BacklogCheck:
    def __init__(self, directory: Path):
        commands = load("backlog_group_memory", ROOT / "scripts/check-group-memory.py")
        self.impl = commands.GroupMemoryCheck(directory, mode="live")
        self.directory = directory
        self.check = self.impl
        self.impl.workspace = directory / "repo"
        self.imported = False
        self.baseline_run_id = 0
        self.progress_path = directory / "acceptance-progress.json"
        self.result_path = directory / "acceptance-result.json"
        self.log_path = ROOT / ".runtime" / (directory.name + ".runtime.log")
        # Keep private credential copies beside, not inside, an unmanifested
        # sandbox data directory; runtime.prepare intentionally refuses adopters.
        self.temp_access = directory.parent / (directory.name + ".private-model-access")
        self._last_snapshot = None

    def prepare_access(self, provider: str, model: str) -> dict:
        service = load("memory_backlog_agent_service", ROOT / "scripts/agent-service.py")
        config = service._load_config()
        env = service._runtime_env(config)
        profile_source = Path(env["V2_SHARED_MODELS_PROFILE"])
        credentials_source = Path(env["V2_SHARED_MODELS_CREDENTIALS"])
        _profile_model(profile_source, provider, model)
        profile_copy = self.temp_access / "profiles/web/cordis.patch.yml"
        credentials_copy = self.temp_access / ".credentials.yaml"
        self.temp_access.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.temp_access, 0o700)
        profile_copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile_copy.write_bytes(profile_source.read_bytes())
        subscription_catalog = profile_source.parent.parent.parent / 'state/v2-subscription-models.json'
        if subscription_catalog.is_file():
            _safe_copy_credentials(subscription_catalog, self.temp_access / 'state/v2-subscription-models.json')
        profile_copy.chmod(0o600)
        _safe_copy_credentials(credentials_source, credentials_copy)
        # Confirm the credential file contains every profile-referenced key without
        # ever printing or serializing a value. Parsing is intentionally non-shell.
        import yaml
        secret_data = yaml.safe_load(credentials_copy.read_text(encoding="utf-8"))
        profile_data = yaml.safe_load(profile_copy.read_text(encoding="utf-8"))
        provider_cfg = next(entry["config"]["providers"][provider] for entry in profile_data
                            if isinstance(entry, dict) and entry.get("id") == "llm-pi-ai")
        env_key = provider_cfg.get("apiKeyEnv")
        refs = secret_data.get("refs") if isinstance(secret_data, dict) else None
        if not isinstance(env_key, str) or not env_key or not isinstance(refs, dict) or not refs.get(env_key):
            raise ValueError("shared model credential is missing the selected provider key")
        selected = {
            "V2_DSH_PROVIDER": provider,
            "V2_DSH_MODEL": model,
            "V2_SHARED_MODELS_PROFILE": str(profile_copy),
            "V2_SHARED_MODELS_CREDENTIALS": str(credentials_copy),
        }
        proxy = env.get("V2_SUBSCRIPTION_PROXY_URL")
        if proxy:
            selected["V2_SUBSCRIPTION_PROXY_URL"] = proxy
        return {"selected_env": selected,
                "profile_source": str(profile_source),
                "profile_sha256_before": sha256(profile_source),
                "credentials_source": str(credentials_source),
                "credentials_sha256_before": sha256(credentials_source),
                "private_credential_copy": str(credentials_copy),
                "credential_copy_removed_after_run": False}

    def start(self, provider: str, model: str) -> dict:
        access = self.prepare_access(provider, model)
        impl = self.check
        data = self.directory
        tap = data / "memory-observer.mjs"
        launch = (
            "import importlib.util, json, os, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "def load(name, path):\n"
            " spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod\n"
            f"root=Path({str(ROOT)!r}); data=Path({str(data)!r})\n"
            "runtime=load('memory_backlog_runtime', root/'sandbox/runtime.py')\n"
            "details=runtime.prepare(data, root/'.runtime/astrbot', "
            f"{{'onebot': {impl.onebot}, 'ui': {impl.ui}, 'mcp': {impl.mcp}}}, 'live', {OWNER!r}, snapshot_mode='text')\n"
            f"tap=Path({str(tap)!r}); tap.write_text({MEMORY_TAP!r}, encoding='utf-8'); tap.chmod(0o600)\n"
            "patch=Path(details['staged_repo'])/'dsh/profile/v2-agent-group-sandbox.patch.yml'\n"
            f"patch.open('a').write({chr(10) + '- insert:' + chr(10) + '    - id: isolated-memory-observer' + chr(10) + '      name: ' + json.dumps(str(tap)) + chr(10) + '      config:' + chr(10) + '        directory: ' + json.dumps(str(data / 'memory-calls')) + chr(10)!r})\n"
            "original=runtime._child_environment\n"
            f"selected={access['selected_env']!r}\n"
            "def child_environment(*args, **kwargs):\n"
            " env=original(*args, **kwargs)\n"
            " for key in ('DEEPSEEK_API_KEY','V2_DSH_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','GEMINI_API_KEY','DASHSCOPE_API_KEY','SILICONFLOW_API_KEY'): env.pop(key,None)\n"
            " if kwargs.get('dsh_enabled'): env.update(selected)\n"
            " return env\n"
            "runtime._child_environment=child_environment\n"
            "raise SystemExit(runtime._run(details, root/'.runtime/astrbot', details['ports'], 'live', owner_volume_mib=32))\n"
        )
        self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.impl.log_path = self.log_path
        self.impl.log = self.log_path.open("ab")
        self.impl.process = subprocess.Popen([sys.executable, "-c", launch], cwd=ROOT,
                                             stdin=subprocess.DEVNULL, stdout=self.impl.log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
        self.wait_forever(lambda: self.impl.http("/api/state").get("connected"), "sandbox OneBot")
        self.wait_forever(lambda: self.impl.journal_path().exists(), "sandbox Journal")
        self.wait_forever(lambda: (data / "astrbot/data/plugin_data/v2_memory/groups.json").is_file(),
                          "sandbox memory manager")
        self.wait_forever(lambda: len(self.impl._memory_databases()) == 1, "sandbox memory database")
        return access

    def wait_forever(self, predicate, label):
        last_report = time.monotonic()
        while True:
            process = self.impl.process
            if process and process.poll() is not None:
                raise RuntimeError(f"sandbox runtime exited during {label} (exit={process.returncode})")
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, sqlite3.OperationalError, TimeoutError):
                pass
            if time.monotonic() - last_report >= 60:
                if self.impl._memory_databases():
                    self.capture_progress(phase="waiting", detail=label)
                print(json.dumps({"phase":"waiting", "detail":label}), flush=True)
                last_report = time.monotonic()
            time.sleep(1)

    def db_snapshot(self) -> tuple[Path, dict, list[dict], list[dict], list[dict]]:
        paths = self.impl._memory_databases()
        if len(paths) != 1:
            raise RuntimeError(f"expected one group memory database; got {len(paths)}")
        path = paths[0]
        with sqlite_connection(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            workspace = dict(db.execute("SELECT * FROM workspace").fetchone())
            memories = [dict(row) for row in db.execute("SELECT * FROM memories ORDER BY id")]
            revisions = [dict(row) for row in db.execute("SELECT * FROM revisions ORDER BY revision,memory_id")]
            runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id")]
        return path, workspace, memories, revisions, runs

    def import_frozen(self, acceptance: Path) -> tuple[dict, list[dict]]:
        manifest, frozen_rows = frozen_snapshot(acceptance)
        marker_path = self.directory / "backlog-import.json"
        path, workspace, _memories, _revisions, _runs = self.db_snapshot()
        journal_path = self.impl.journal_path()
        if marker_path.exists():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("frozen_manifest_sha256") != sha256(acceptance / "manifest.json"):
                raise ValueError("resume data belongs to another frozen snapshot")
            self.imported = True
            self.baseline_run_id = int(marker["baseline_run_id"])
            return manifest, frozen_rows
        if workspace["cursor"] != 0:
            raise RuntimeError("unmarked non-empty backlog sandbox cannot be adopted; use fresh --data-dir")
        journal_identity = self.impl.rows()
        if journal_identity:
            raise RuntimeError("fresh backlog sandbox Journal is unexpectedly non-empty")
        # Seed Journal rows with original IDs, actor IDs, content and timestamps.
        # Only the sandbox platform/bot/conversation identity is remapped.
        with sqlite_connection(journal_path) as db:
            for source in frozen_import_rows(acceptance, manifest, frozen_rows):
                row = dict(source)
                row.update(platform_id=SANDBOX_IDENTITY[0], bot_id=SANDBOX_IDENTITY[1],
                           conversation_key=SANDBOX_IDENTITY[2], conversation_type="group")
                columns = tuple(row)
                db.execute("INSERT INTO journal (" + ",".join(columns) + ") VALUES (" +
                           ",".join("?" for _ in columns) + ")", [row[k] for k in columns])
            db.execute("INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES('journal',?)",
                       (manifest["through_id"],))
            db.commit()
        # Copy the frozen memory state into the initialized sandbox store, while
        # binding it to sandbox identity and disabling background incremental work.
        with sqlite_connection(path) as dst, sqlite_connection(acceptance / "memory.sqlite3") as src:
            src.backup(dst)
            dst.execute("UPDATE workspace SET platform_id=?,bot_id=?,conversation_key=?,cursor=?,enabled=1,"
                        "incremental_enabled=0,failed_after=NULL,failed_through=NULL,"
                        "incremental_error_code=NULL,incremental_recovery_checked=0",
                        (SANDBOX_IDENTITY[0], SANDBOX_IDENTITY[1], SANDBOX_IDENTITY[2], manifest["after_id"]))
            dst.commit()
            self.baseline_run_id = dst.execute("SELECT COALESCE(MAX(id),0) FROM runs").fetchone()[0]
        persona_path = path.parent.parent / "persona.md"
        persona_path.write_bytes((acceptance / "persona.md").read_bytes())
        persona_path.chmod(0o600)
        install_commit_audit(path)
        self.imported = True
        private_json(marker_path, {"frozen_manifest_sha256": sha256(acceptance / "manifest.json"),
                                   "baseline_run_id": self.baseline_run_id,
                                   "frozen_after_id": manifest["after_id"],
                                   "frozen_through_id": manifest["through_id"],
                                   "frozen_rows": len(frozen_rows), "sandbox_identity": SANDBOX_IDENTITY})
        return manifest, frozen_rows

    def set_route(self, provider: str, model: str):
        # Resolve /provider selection through the real native command and verify
        # the resulting outbound acknowledgment before the memory run starts.
        event = self.impl.wake(GROUP, f"/provider {provider} {model}", sender=OWNER, mention=False)
        out = self.wait_forever(lambda: next((row for row in self.impl.events()
            if row["direction"] == "outbound" and row["conversation"] == event["conversation"]
            and int(row["message_id"]) > int(event["message_id"])), None), "model route selection")
        if f"{provider} / {model}" not in json.dumps(out, ensure_ascii=False):
            raise RuntimeError("sandbox native provider command did not confirm requested route")

    def capture_progress(self, *, phase: str, detail: str | None = None) -> dict:
        path, workspace, memories, revisions, runs = self.db_snapshot()
        new_runs = [run for run in runs if int(run["id"]) > self.baseline_run_id]
        attempts = []
        for run in new_runs:
            attempts.append({key: run.get(key) for key in (
                "id", "status", "after_id", "through_id", "duration_ms", "model", "usage_json",
                "error_code", "created_at")})
        value = {"phase": phase, "detail": detail, "updated_at": time.time(),
                 "cursor": workspace["cursor"], "revision": workspace["revision"],
                 "new_run_count": len(new_runs), "attempts": attempts,
                 "memory_count": len(memories), "revision_count": len(revisions)}
        private_json(self.progress_path, value)
        return value

    def run_command(self, provider: str, model: str, acceptance: Path) -> tuple[dict, list[dict], dict]:
        manifest, frozen_rows = self.import_frozen(acceptance)
        path, workspace, memories, revisions, runs = self.db_snapshot()
        self.set_route(provider, model)
        capacity = self.native_capacity(provider, model)
        private_json(self.directory / "capacity-preflight.json", capacity)
        before_run_id = max([self.baseline_run_id, *(int(run["id"]) for run in runs)])
        command_event = self.impl.wake(GROUP, "/memory", sender=OWNER, mention=False)
        command_row = self.wait_forever(lambda: next((row for row in self.impl.rows()
            if row["message_id"] == str(command_event["message_id"])
            and row["conversation_key"] == GROUP), None), "canonical /memory command")
        private_json(self.directory / "last-command.json", {
            "message_id": command_event["message_id"], "journal_id": command_row["journal_id"],
            "started_at": time.time(), "provider": provider, "model": model,
            "target_frozen_through_id": manifest["through_id"]})
        self.capture_progress(phase="running", detail=f"/memory command journal {command_row['journal_id']}")
        last_cursor = workspace["cursor"]
        last_report = time.monotonic()
        output = None
        while output is None:
            process = self.impl.process
            if process and process.poll() is not None:
                raise RuntimeError(f"sandbox exited while /memory was running (exit={process.returncode})")
            rows_now = self.impl.rows()
            output = next((row for row in self.impl.events()
                           if row["direction"] == "outbound"
                           and row["conversation"] == command_event["conversation"]
                           and int(row["message_id"]) > int(command_event["message_id"])), None)
            progress = self.capture_progress(phase="running")
            if progress["cursor"] != last_cursor:
                last_cursor = progress["cursor"]
                private_json(self.directory / "live-progress.json", progress)
            if time.monotonic() - last_report >= 60:
                print(json.dumps({"phase": "running", "cursor": progress["cursor"],
                                  "attempts": progress["new_run_count"], "last_status":
                                  progress["attempts"][-1]["status"] if progress["attempts"] else None},
                                 ensure_ascii=False), flush=True)
                last_report = time.monotonic()
            time.sleep(1)
        self.wait_forever(lambda: any(row["message_id"] == str(output["message_id"])
            and row["direction"] == "outbound" for row in self.impl.rows()), "canonical /memory response")
        path, workspace, memories, revisions, runs = self.db_snapshot()
        new_runs = [run for run in runs if int(run["id"]) > before_run_id]
        success = "记忆整理完成" in json.dumps(output, ensure_ascii=False)
        permitted_models = {f"{provider}/{model}", f"{provider}:{model}", model,
                            f"{provider}/{model}+wb/glm-5.3-flash"}
        wrong_routes = [run["id"] for run in new_runs
                        if run.get("status") != "running" and run.get("model") not in permitted_models]
        if wrong_routes:
            raise AssertionError(f"memory batch used an unexpected model route; run IDs={wrong_routes}")
        attempt = {"command_message_id": command_event["message_id"],
                   "command_journal_id": command_row["journal_id"],
                   "response": output, "success": success,
                   "runs": [{key: row.get(key) for key in ("id", "status", "after_id", "through_id",
                       "duration_ms", "model", "usage_json", "error_code", "created_at")}
                       for row in new_runs],
                   "cursor_after": workspace["cursor"]}
        self._save_model_evidence(attempt)
        all_new = [run for run in runs if int(run["id"]) > self.baseline_run_id]
        result = {"command_attempt": attempt, "workspace": workspace,
                  "memory_count": len(memories), "revision_count": len(revisions),
                  "new_runs": [{key: row.get(key) for key in ("id", "status", "after_id", "through_id",
                       "duration_ms", "model", "usage_json", "error_code", "created_at")}
                       for row in all_new], "runs_db": str(path)}
        if success:
            with sqlite_connection(path) as db:
                committed_ranges = [{"status":"completed", "after_id":after, "through_id":through}
                                    for after, through in db.execute("SELECT after_id,through_id FROM acceptance_commits ORDER BY revision")]
            validate_success_coverage(frozen_rows, committed_ranges, workspace["cursor"],
                                       manifest["through_id"])
            if workspace["cursor"] != command_row["journal_id"]:
                raise AssertionError("successful /memory did not reach its own Journal row")
            if not any(run["status"] == "completed" and run["through_id"] >= command_row["journal_id"]
                       for run in all_new):
                raise AssertionError("successful /memory has no completed batch through command row")
        else:
            raise RuntimeError(f"/memory returned failure; cursor held at {workspace['cursor']}; explicit --resume required")
        result["integrity_audit"] = self.audit_success(acceptance, frozen_rows)
        return frozen_rows, result["new_runs"], result

    def audit_success(self, acceptance: Path, frozen_rows: list[dict]) -> dict:
        calls = [json.loads(path.read_text()) for path in (self.directory / "memory-calls").glob("*.json")]
        covered = set()
        for call in calls:
            if not _call_completed(call):
                continue
            prompt = _call_payload(call)
            for row in prompt.get("messages", []):
                covered.add(_message_journal_id(row))
        missing = {row["journal_id"] for row in frozen_rows} - covered
        if missing:
            raise AssertionError(f"frozen rows missing from completed model requests: {len(missing)}")
        _path, workspace, memories, revisions, _runs = self.db_snapshot()
        with sqlite_connection(acceptance / "memory.sqlite3") as db:
            prior_revision = db.execute("SELECT revision FROM workspace").fetchone()[0]
            prior = {row[0]: set(json.loads(row[1])) for row in db.execute("SELECT id,source_ids_json FROM memories")}
        saved = {row['id']: row for row in memories}
        if not set(prior).issubset(saved):
            raise AssertionError("prior memory IDs were lost")
        for memory_id, sources in prior.items():
            if not sources.issubset(set(json.loads(saved[memory_id]['source_ids_json']))):
                raise AssertionError("prior memory sources were lost")
        evidence = load("backlog_evidence_audit", ROOT / "astrbot-plugins/v2_memory/evidence.py")
        source_rows = {row['journal_id']: row for row in self.impl.rows()}
        manifest, _pending = frozen_snapshot(acceptance)
        for source in frozen_import_rows(acceptance, manifest, frozen_rows):
            expected = {**source, "platform_id":SANDBOX_IDENTITY[0], "bot_id":SANDBOX_IDENTITY[1],
                        "conversation_key":SANDBOX_IDENTITY[2], "conversation_type":"group"}
            if source_rows.get(source['journal_id']) != expected:
                raise AssertionError(f"sandbox Journal changed frozen source {source['journal_id']}")
        with sqlite_connection(_path) as db:
            db.row_factory = sqlite3.Row
            commits = [dict(row) for row in db.execute("SELECT * FROM acceptance_commits ORDER BY revision")]
        current = manifest['after_id']
        linked = []
        used = set()
        generation_calls = []
        for call in calls:
            try:
                prompt = _call_payload(call)
            except AssertionError:
                continue
            if "existing_memories" in prompt and "messages" in prompt and "draft_changes" not in prompt:
                generation_calls.append(call)
        generation_calls.sort(key=lambda item: _call_timestamp(item, "started_at"))
        for commit in commits:
            assert commit['after_id'] == current, "commit chain skipped a range"
            expected_ids = [row['journal_id'] for row in source_rows.values()
                            if current < row['journal_id'] <= commit['through_id']]
            matches = []
            for call in generation_calls:
                if call['id'] in used or not _call_completed(call):
                    continue
                prompt = _call_payload(call)
                actual_ids = [_message_journal_id(row) for row in prompt['messages']]
                if actual_ids == expected_ids:
                    matches.append(call)
            if not matches:
                raise AssertionError(f"commit {commit['revision']} has no exact completed model input")
            matching_responses = []
            for candidate in matches:
                try:
                    generation_prompt = _call_payload(candidate)
                    generated_changes = parse_model_json(candidate["text"]).get("changes")
                    if not isinstance(generated_changes, list):
                        raise AssertionError("generation response has no changes array")
                    start = _call_timestamp(candidate, "finished_at")
                    end = _commit_timestamp(commit)
                    later_generations = [
                        _call_timestamp(other, "started_at") for other in generation_calls
                        if other["id"] != candidate["id"]
                        and _call_timestamp(other, "started_at") > start
                        and _call_timestamp(other, "started_at") < end]
                    if later_generations:
                        end = min(end, min(later_generations))
                    review_calls = []
                    for call in calls:
                        try:
                            review_prompt = _call_payload(call)
                        except AssertionError:
                            continue
                        if "draft_changes" not in review_prompt or "messages" not in review_prompt:
                            continue
                        if (_call_timestamp(call, "started_at") > start
                                and _call_timestamp(call, "finished_at") <= end):
                            review_calls.append(call)
                    commit_revisions = [r for r in revisions if r["revision"] == commit["revision"]]
                    if review_calls:
                        if any(not _call_completed(call) for call in review_calls):
                            raise AssertionError("commit followed an unsuccessful fact-review call")
                        review_audit = audit_review_commit(candidate, review_calls,
                                                           commit_revisions, source_rows)
                        audit_kind = "fact_review"
                    else:
                        # Captures produced before the mandatory fact-review stage
                        # retain the original one-generation/one-commit contract.
                        audit_response_commit(candidate, commit_revisions, source_rows)
                        review_audit = {"review_count": 0, "reviewed_changes": 0}
                        audit_kind = "generation_only"
                except (AssertionError, KeyError, ValueError, TypeError):
                    continue
                matching_responses.append((candidate, audit_kind, review_calls, review_audit))
            if not matching_responses:
                raise AssertionError(f"commit {commit['revision']} does not preserve any matching model response")
            call, audit_kind, review_calls, review_audit = max(
                matching_responses, key=lambda item:_call_timestamp(item[0], "finished_at"))
            used.add(call['id'])
            used.update(item["id"] for item in review_calls)
            linked.append({**commit,'call_id':call['id'],'message_count':len(expected_ids),
                           'audit_kind':audit_kind,
                           'review_call_ids':[item["id"] for item in review_calls],
                           'review_count':review_audit["review_count"],
                           'reviewed_changes':review_audit["reviewed_changes"]})
            current = commit['through_id']
        assert current == workspace['cursor']
        private_json(self.directory / 'commits-to-model-calls.json', linked)
        checked = 0
        for revision in revisions:
            if revision['revision'] <= prior_revision:
                continue
            memory = json.loads(revision['after_json'])
            for claim in memory['claims'] or []:
                for source in claim['sources']:
                    row = source_rows[source['journal_id']]
                    assert (row['platform_id'],row['bot_id'],row['conversation_key']) == SANDBOX_IDENTITY
                    assert (row['actor_id'],row['direction']) == (source['actor_id'],source['direction'])
                    content = json.loads(row['content_json'])
                    assert evidence.source_evidence_is_current(content, source)
                    assert source['journal_id'] in memory['source_ids']
                    checked += 1
        report = {"commits_linked_to_model_requests":len(linked), "frozen_source_rows_unchanged":len(frozen_import_rows(acceptance,manifest,frozen_rows)), "model_requests":len(calls), "frozen_rows_in_completed_model_inputs":len(covered.intersection(
            {row['journal_id'] for row in frozen_rows})), "new_revision_citations_verified":checked,
            "fact_review_responses_verified":sum(item["review_count"] for item in linked),
            "fact_review_changes_verified":sum(item["reviewed_changes"] for item in linked),
            "prior_memory_ids_retained":len(prior), "final_cursor":workspace['cursor']}
        private_json(self.directory / 'integrity-audit.json', report)
        return report

    def native_capacity(self, provider: str, model: str) -> dict:
        """Call the live DSH memory-capacity socket without generating model text."""
        sandbox_artifact_root = str((self.directory / "astrbot/data").resolve())
        deadline = time.monotonic() + 90
        target = None
        while time.monotonic() < deadline:
            if self.impl.process and self.impl.process.poll() is not None:
                raise RuntimeError("sandbox runtime exited before native capacity preflight")
            for proc in Path("/proc").iterdir():
                if not proc.name.isdigit() or int(proc.name) == os.getpid():
                    continue
                try:
                    if proc.stat().st_uid != os.getuid():
                        continue
                    entries = (proc / "environ").read_bytes().split(b"\0")
                except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                    continue
                env = {}
                for entry in entries:
                    key, sep, value = entry.partition(b"=")
                    if sep and key in {b"SANDBOX_ARTIFACT_ROOT", b"V2_MEMORY_CONTROL_SOCKET",
                                       b"V2_MEMORY_CONTROL_TOKEN"}:
                        env[key.decode("ascii")] = value.decode("utf-8", "strict")
                if (env.get("SANDBOX_ARTIFACT_ROOT") == sandbox_artifact_root
                        and env.get("V2_MEMORY_CONTROL_SOCKET") and env.get("V2_MEMORY_CONTROL_TOKEN")):
                    target = env
                    break
            if target:
                break
            time.sleep(0.25)
        if not target:
            raise RuntimeError("could not locate the isolated DSH memory-capacity endpoint")
        path = Path(target["V2_MEMORY_CONTROL_SOCKET"])
        token = target["V2_MEMORY_CONTROL_TOKEN"]
        if not path.is_absolute() or "/v2-memory-control-" not in str(path):
            raise PermissionError("isolated DSH memory socket path failed validation")
        parent = path.parent
        meta = parent.stat(follow_symlinks=False)
        if meta.st_uid != os.getuid() or meta.st_mode & 0o077 or not parent.is_dir():
            raise PermissionError("isolated DSH memory socket directory is not private")
        request = json.dumps({"action": "capacity", "provider": provider,
                              "model": model, "token": token}, separators=(",", ":")).encode() + b"\n"
        result = bytearray()
        started = time.monotonic()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path))
            connection.sendall(request)
            while b"\n" not in result:
                if self.impl.process and self.impl.process.poll() is not None:
                    raise RuntimeError("sandbox runtime exited during native capacity preflight")
                if time.monotonic() - started > 120:
                    raise TimeoutError("native model capacity lookup exceeded its native 120-second deadline")
                try:
                    chunk = connection.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    raise RuntimeError("native capacity endpoint closed without a response")
                result.extend(chunk)
                if len(result) > 64 * 1024:
                    raise ValueError("native capacity response exceeded its byte limit")
        value = json.loads(bytes(result).split(b"\n", 1)[0])
        if (value.get("ok") is not True or value.get("provider") != provider
                or value.get("model") != model
                or not isinstance(value.get("contextWindow"), int)
                or not isinstance(value.get("defaultMaxTokens"), int)
                or not 0 < value.get("defaultMaxTokens", 0) < value.get("contextWindow", 0)
                or value.get("capacitySource") not in {"native_adapter", "native_subscription_catalog"}):
            code = value.get("code") if isinstance(value, dict) else "invalid response"
            raise RuntimeError(f"native model capacity preflight failed: {code}")
        router_source = ROOT / 'astrbot-plugins/v2_dsh_router'
        sys.path.insert(0, str(router_source))
        client = load('backlog_native_transport', router_source / 'dsh_client.py')
        runtime = object.__new__(client.AcpRuntime)
        runtime._memory_control_path = str(path)
        runtime._memory_control_token = token
        transported = runtime.memory_capacity(provider=provider, model=model)
        for key in ('provider','model','contextWindow','defaultMaxTokens','capacitySource'):
            if transported.get(key) != value.get(key):
                raise AssertionError('Python capacity transport changed native metadata')
        return {"provider": provider, "model": model,
                "python_transport_verified": True,
                "context_window": value["contextWindow"],
                "default_max_tokens": value["defaultMaxTokens"],
                "capacity_source": value["capacitySource"],
                "preflight_kind": "native_metadata_only_no_generation"}

    def _save_model_evidence(self, attempt: dict) -> None:
        # DSH text snapshots carry the real model prompt/response. Keep the full
        # export only in this mode-0700 private acceptance directory.
        evidence = self.impl.evidence()
        private_json(self.directory / "model-evidence.json", evidence)
        private_json(self.directory / "last-attempt-evidence.json", attempt)

    def stop_and_cleanup(self) -> dict:
        result = {"stopped": False, "mounts_released": False,
                  "export_required": self.imported, "export_completed": False}
        try:
            if self.imported:
                export = self.directory / "final-state"
                export.mkdir(mode=0o700, exist_ok=True)
                for name, source in (("journal.sqlite3", self.impl.journal_path()),
                                     ("memory.sqlite3", self.impl._memory_databases()[0])):
                    temporary = export / (name + ".new")
                    temporary.unlink(missing_ok=True)
                    with sqlite_connection(source) as src, sqlite_connection(temporary) as dst:
                        src.backup(dst)
                        if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise AssertionError("final SQLite export failed integrity check")
                        if name == "memory.sqlite3":
                            expected = src.execute("SELECT cursor,revision FROM workspace").fetchone()
                            actual = dst.execute("SELECT cursor,revision FROM workspace").fetchone()
                            if actual != expected:
                                raise AssertionError("final memory export differs from current state")
                            result["exported_cursor"], result["exported_revision"] = actual
                    temporary.chmod(0o600)
                    os.replace(temporary, export / name)
                result["export_completed"] = True
        except Exception as error:
            result["export_error"] = f"{type(error).__name__}: {error}"
        # The inherited inspection helper uses sqlite connection contexts that
        # leave unreachable descriptors until collection. Close them before stop
        # attempts to unmount the isolated workspace.
        gc.collect()
        try:
            self.check.stop()
            result["stopped"] = self.check.process is None
        finally:
            if self.temp_access.exists():
                shutil.rmtree(self.temp_access)
        group_root = self.directory / "repo/.runtime/group-storage"
        mounted = [p for p in group_root.glob("*/workspace") if os.path.ismount(p)] if group_root.exists() else []
        if mounted:
            helper = load("backlog_group_volume_cleanup", ROOT / "dsh/plugins/agent-profile/owner_volume.py")
            for mount in mounted:
                helper.unmount(mount)
        result["unmounted_paths"] = [str(path) for path in mounted]
        result["mounts_released"] = not any(os.path.ismount(p) for p in group_root.glob("*/workspace")) \
            if group_root.exists() else True
        result["temporary_credentials_removed"] = not self.temp_access.exists()
        if self.log_path.exists():
            shutil.copy2(self.log_path, self.directory / "runtime.log")
            (self.directory / "runtime.log").chmod(0o600)
        private_json(self.directory / "cleanup.json", result)
        return result


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance-dir", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--provider", default=PROVIDER)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--resume", action="store_true", help="explicitly run another attempt in an existing sandbox")
    parser.add_argument("--prepare-only", action="store_true", help="validate frozen inputs/profile/disk without starting DSH")
    parser.add_argument("--capacity-preflight", action="store_true",
                        help="start a temporary isolated runtime and call native model capacity; never generate text")
    parser.add_argument("--harness-test", action="store_true", help="run pure harness checks without models or mounts")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    os.umask(0o077)
    if args.harness_test:
        manifest, rows = frozen_snapshot(args.acceptance_dir.resolve())
        assert len(rows) == REQUIRED_ROWS and rows[0]["journal_id"] > manifest["after_id"]
        validate_success_coverage(rows[:2], [{"status": "completed", "after_id": 38048,
                                               "through_id": rows[1]["journal_id"]}],
                                  rows[1]["journal_id"], rows[1]["journal_id"])
        try:
            validate_success_coverage(rows[:2], [], rows[1]["journal_id"], rows[1]["journal_id"])
        except AssertionError:
            pass
        else:
            raise AssertionError("coverage helper accepted uncovered rows")
        print("memory backlog harness helpers passed")
        return 0
    acceptance = args.acceptance_dir.resolve(strict=True)
    manifest, frozen_rows = frozen_snapshot(acceptance)
    free = shutil.disk_usage(args.data_dir.parent if args.data_dir.parent.exists() else ROOT).free
    if free < 3 * 1024 ** 3:
        raise RuntimeError(f"less than 3 GiB free before sandbox setup: {free}")
    if args.prepare_only:
        service = load("memory_backlog_agent_service", ROOT / "scripts/agent-service.py")
        config = service._load_config()
        env = service._runtime_env(config)
        _profile_model(Path(env["V2_SHARED_MODELS_PROFILE"]), args.provider, args.model)
        print(json.dumps({"prepared": True, "frozen_rows": len(frozen_rows),
                          "frozen_through_id": manifest["through_id"],
                          "provider": args.provider, "model": args.model,
                          "free_bytes": free, "live_model_started": False}, ensure_ascii=False))
        return 0
    if args.capacity_preflight:
        preflight_dir = args.data_dir
        if preflight_dir.exists():
            raise RuntimeError("capacity preflight requires a fresh temporary --data-dir")
        preflight_dir.mkdir(mode=0o700, parents=True)
        runner = BacklogCheck(preflight_dir.resolve())
        record = {"provider": args.provider, "model": args.model,
                  "preflight_kind": "native_metadata_only_no_generation",
                  "frozen_backlog_verified": len(frozen_rows),
                  "started_at": time.time(), "passed": False}
        access = None
        try:
            access = runner.start(args.provider, args.model)
            record["source_hashes_before"] = {"profile": access["profile_sha256_before"],
                                                "credentials": access["credentials_sha256_before"]}
            runner.set_route(args.provider, args.model)
            record["capacity"] = runner.native_capacity(args.provider, args.model)
            record["source_hashes_before"] = {
                "profile": access["profile_sha256_before"],
                "credentials": access["credentials_sha256_before"]}
            record["passed"] = True
        except BaseException as error:
            record.update(error_type=type(error).__name__, error=str(error))
            import traceback
            record["traceback"] = traceback.format_exc()
        finally:
            record["finished_at"] = time.time()
            try:
                record["cleanup"] = runner.stop_and_cleanup()
                if not record["cleanup"]["stopped"] or not record["cleanup"]["mounts_released"] \
                        or not record["cleanup"]["temporary_credentials_removed"] \
                        or (record["cleanup"]["export_required"] and not record["cleanup"]["export_completed"]):
                    record["passed"] = False
            except BaseException as error:
                record["passed"] = False
                record["cleanup_error"] = f"{type(error).__name__}: {error}"
            if access:
                profile = Path(access["profile_source"])
                credentials = Path(access["credentials_source"])
                record["source_hashes_after"] = {"profile": sha256(profile),
                                                   "credentials": sha256(credentials)}
                record["production_sources_unchanged"] = (
                    record["source_hashes_before"]["profile"] == record["source_hashes_after"]["profile"]
                    and record["source_hashes_before"]["credentials"] == record["source_hashes_after"]["credentials"])
                record["passed"] = record["passed"] and record["production_sources_unchanged"]
            safe_record = acceptance / ("capacity-preflight-" + uuid.uuid4().hex[:12] + ".json")
            private_json(safe_record, record)
            print(json.dumps({key: value for key, value in record.items()
                              if key != "traceback"}, ensure_ascii=False), flush=True)
            if record.get("cleanup", {}).get("mounts_released") and record.get("cleanup", {}).get("stopped"):
                runtime_copy = preflight_dir / "runtime.log"
                if not record["passed"] and runtime_copy.is_file():
                    diagnostic_log = safe_record.with_name(safe_record.stem + "-runtime.log")
                    shutil.copy2(runtime_copy, diagnostic_log)
                    diagnostic_log.chmod(0o600)
                shutil.rmtree(preflight_dir)
                runtime_log = ROOT / ".runtime" / (preflight_dir.name + ".runtime.log")
                runtime_log.unlink(missing_ok=True)
            else:
                record["temporary_sandbox_retained"] = str(preflight_dir)
                private_json(safe_record, record)
        return 0 if record["passed"] else 1
    if args.data_dir.exists() and not args.resume:
        raise RuntimeError("data directory already exists; pass --resume to explicitly continue or choose a fresh directory")
    if args.resume and not (args.data_dir / "backlog-import.json").is_file():
        raise RuntimeError("--resume requires an existing backlog-import.json marker")
    args.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.data_dir, 0o700)
    runner = BacklogCheck(args.data_dir.resolve())
    result = {"passed": False, "provider": args.provider, "model": args.model,
              "frozen_pending_rows": len(frozen_rows), "frozen_existing_memories": REQUIRED_MEMORIES,
              "frozen_through_id": manifest["through_id"], "resumed": args.resume,
              "started_at": time.time()}
    try:
        access = runner.start(args.provider, args.model)
        result["model_access"] = {key: value for key, value in access.items()
                                  if key not in {"selected_env", "private_credential_copy"}}
        _rows, _batches, summary = runner.run_command(args.provider, args.model, acceptance)
        result.update(summary)
        result["passed"] = True
    except BaseException as error:
        result.update(error_type=type(error).__name__, error=str(error))
        import traceback
        result["traceback"] = traceback.format_exc()
        try:
            result["progress"] = runner.capture_progress(phase="failed", detail=type(error).__name__)
        except Exception as snapshot_error:
            result["progress_error"] = type(snapshot_error).__name__
        try:
            if runner.impl.process is not None:
                result["diagnostic_model_evidence"] = True
                runner._save_model_evidence(result)
        except Exception as evidence_error:
            result["evidence_error"] = type(evidence_error).__name__
    finally:
        result["finished_at"] = time.time()
        try:
            result["cleanup"] = runner.stop_and_cleanup()
            if not result["cleanup"]["stopped"] or not result["cleanup"]["mounts_released"] \
                    or not result["cleanup"]["temporary_credentials_removed"] \
                    or (result["cleanup"]["export_required"] and not result["cleanup"]["export_completed"]):
                result["passed"] = False
        except BaseException as cleanup_error:
            result["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            result["passed"] = False
        if "model_access" in result:
            result["model_access"]["credential_copy_removed_after_run"] = (
                result.get("cleanup", {}).get("temporary_credentials_removed", False))
        private_json(runner.result_path, result)
        private_json(acceptance / ("backlog-attempt-" + uuid.uuid4().hex[:12] + ".json"), result)
        print(json.dumps({key: value for key, value in result.items()
                          if key not in {"traceback", "model_access"}}, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
