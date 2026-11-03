"""End-to-end contracts for group memory over the real Journal and store."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "astrbot-plugins"))

from v2_memory.backend import GroupMemoryBackend, MemoryUnavailable, _include_claim_source_ids
from v2_memory.batching import BatchCapacityError
from v2_memory.store import RevisionConflict
from v2_tasks.runner import DailyMemoryRunner, GroupBinding
from v2_tasks.state import TaskStore


def _journal_module():
    path = REPO / "astrbot-plugins/v2_journal/journal.py"
    spec = importlib.util.spec_from_file_location("v2_memory_test_journal", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


JOURNAL = _journal_module()


class FakeVolume:
    @staticmethod
    def verified_group_workspace(path):
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


class FakeState:
    def get_model_route(self, *_identity):
        return ("test-provider", "test-model")


class FakeDsh:
    def __init__(self, responses=()):
        self.settings = {"provider": "test-provider", "model": "test-model"}
        self.responses = deque(responses)
        self.calls = []

    def memory_capacity(self, **kwargs):
        return {"provider": kwargs["provider"], "model": kwargs["model"],
                "contextWindow": 1_000_000, "defaultMaxTokens": 256_000,
                "capacitySource": "native_adapter"}

    def consolidate_memory(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if callable(response):
            response = response(**kwargs)
        if isinstance(response, dict) and isinstance(response.get("text"), str):
            value = json.loads(response["text"])
            if isinstance(value, dict) and isinstance(value.get("changes"), list):
                prompt = json.loads(kwargs["prompt"])
                messages = {row["journal_id"]: row for row in prompt.get("messages", [])}
                message_evidence = {}
                for row in prompt.get("messages", []):
                    message_evidence[row["journal_id"]] = row.get("evidence", [])
                memories = {evidence["journal_id"]: evidence
                            for memory in prompt.get("existing_memories", [])
                            for evidence in memory.get("evidence", [])}
                for item in value["changes"]:
                    if "claims" in item:
                        continue
                    source_ids = item.get("source_ids", [])
                    source_id = next((sid for sid in source_ids if sid in messages), None)
                    quote = (next((evidence["quote"] for evidence in message_evidence.get(source_id, [])), "")
                             if source_id is not None else "")
                    if source_id is None:
                        source = next((memories[sid] for sid in source_ids if sid in memories), None)
                        if source:
                            source_id, quote = source["journal_id"], source["quote"]
                    item["claims"] = ([{"text": item["content"], "sources": [
                        {"journal_id": source_id, "quote": quote}]}] if source_id else [])
                response = {**response, "text": json.dumps(value, ensure_ascii=False)}
        return response


def response(changes, usage=None):
    model_changes = json.loads(json.dumps(changes))
    for item in model_changes:
        item.pop("claims", None)
    return {"text": json.dumps({"changes": model_changes}, ensure_ascii=False),
            "usage": usage or {"inputTokens": 9, "outputTokens": 3}}


def change(source_ids, *, content="群内记忆", memory_id=None):
    result = {"content": content, "members": ["100", "200"],
              "source_ids": source_ids,
              "claims": [{"text": content[:1000], "sources": [
                  {"journal_id": source_id, "quote": f"source {source_id}",
                   "actor_id": "100", "direction": "inbound"}
                  for source_id in source_ids[:5]]}],
              "weight": 3, "status": "active"}
    if memory_id:
        result["id"] = memory_id
    return result


def append(journal, conversation, message_id, actor, text, *, direction="inbound", created_at=1):
    message = JOURNAL.Message(
        platform_id=conversation.platform_id,
        bot_id=conversation.bot_id,
        conversation_key=conversation.key,
        conversation_type="group",
        direction=direction,
        actor_id=actor,
        actor_name={"100": "A", "200": "B", "900": "Bot"}.get(actor),
        source="user" if direction == "inbound" else "unknown",
        message_id=str(message_id),
        reply_to=None,
        content={"text": text, "segments": [{"type": "text", "text": text}]},
        created_at=created_at,
        observed_at_ns=time.time_ns(),
    )
    return journal.append(message)[0]


def make_backend(tmp_path, *, responses=(), group_id="12345"):
    conversation = JOURNAL.Conversation("napcat", "900001", "group:" + group_id)
    journal = JOURNAL.JournalStore(tmp_path / "journal.sqlite3")
    workspace = tmp_path / ("workspace-" + group_id)
    workspace.mkdir(mode=0o700)
    (workspace / "persona.md").write_text("保持本群人格。", encoding="utf-8")
    service = SimpleNamespace(
        _disabled_identities=set(),
        state=FakeState(),
        dsh=FakeDsh(responses),
    )
    router = SimpleNamespace(
        _closing=False,
        allowed_conversations=frozenset({conversation.key}),
        services={"group:testhash": service},
        _group_runtimes={"group:testhash": {
            "platform_id": conversation.platform_id,
            "bot_id": conversation.bot_id,
            "group_id": group_id,
            "workspace": workspace,
            "volume_module": FakeVolume,
        }},
        console_store=None,
    )
    backend = GroupMemoryBackend(
        conversation, journal, service, workspace, FakeVolume, REPO, router,
        SimpleNamespace(warning=lambda *a, **k: None),
    )
    return backend, journal, conversation, workspace, service


def set_memory_capacity(service, *, context=1_000_000, output=256_000):
    service.dsh.memory_capacity = lambda **kwargs: {
        "provider": kwargs["provider"], "model": kwargs["model"],
        "contextWindow": context, "defaultMaxTokens": output,
        "capacitySource": "native_adapter",
    }


async def store_snapshot(backend):
    return await backend._io("snapshot")


@pytest.mark.asyncio
async def test_paused_agent_initializes_and_interrupted_run_is_not_replayed(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    old = append(journal, conversation, 1, "100", "启用时即使Agent暂停也登记高水位")
    service._disabled_identities.add(backend.identity)
    await backend.initialize()
    assert (await backend.snapshot())["cursor"] == old.journal_id
    assert not service.dsh.calls
    pending = append(journal, conversation, 2, "100", "进程中断时尚未整理的范围")
    await backend._io("record_run", "running", old.journal_id, pending.journal_id, 1, None, None)
    await backend.close()
    replacement = GroupMemoryBackend(conversation, journal, service, backend.workspace,
                                     FakeVolume, REPO, backend.router, backend.logger)
    await replacement.initialize()
    assert replacement.last_run["status"] == "interrupted"
    state = await replacement.incremental_state()
    assert state["failed_after"] == old.journal_id
    assert state["failed_through"] == pending.journal_id
    assert state["error_code"] == "PROCESS_INTERRUPTED"
    assert not service.dsh.calls
    await replacement.close()


@pytest.mark.asyncio
async def test_source_pages_preserve_all_evidence_and_search_projection_is_bounded(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, index, "100", f"来源{index}") for index in range(1, 46)]
    expected = [row.journal_id for row in rows]
    saved = await backend._io("commit", 0, 0, expected[-1], [change(expected)], {})
    memory_id = saved["memories"][0]["id"]
    retrieved = await backend.search("群内记忆")
    assert retrieved[0]["source_count"] == 45
    assert len(retrieved[0]["source_ids"]) == 40
    found, offset = set(), 0
    while True:
        page = await backend.sources(memory_id, 20, offset)
        assert len(page["entries"]) <= 20
        found.update(entry["journal_id"] for entry in page["entries"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert found == set(expected)
    with pytest.raises(ValueError):
        await backend.sources(memory_id, 20, -1)
    await backend.close()


@pytest.mark.asyncio
async def test_initial_cursor_excludes_old_history_and_success_commits_sources_once(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    append(journal, conversation, 1, "100", "启用前 A 的旧消息")
    old_bot = append(journal, conversation, 2, "900", "启用前 Bot 的旧消息", direction="outbound")
    await backend.initialize()
    assert (await store_snapshot(backend))["cursor"] == old_bot.journal_id

    row_a = append(journal, conversation, 3, "100", "A 说自己存下了工资")
    row_b = append(journal, conversation, 4, "200", "B 反驳，说 A 用来充值游戏")
    row_bot = append(journal, conversation, 5, "900", "我记得这段经历", direction="outbound")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "A 说工资存下了；B 反驳称 A 用于充值游戏", "members": ["100", "200"],
        "source_ids": [row_a.journal_id, row_b.journal_id, row_bot.journal_id],
        "claims": [
            {"text": "A称自己存下工资", "sources": [{"journal_id": row_a.journal_id,
                                                   "quote": "A 说自己存下了工资"}]},
            {"text": "B反驳称A用于充值游戏", "sources": [{"journal_id": row_b.journal_id,
                                                       "quote": "B 反驳，说 A 用来充值游戏"}]},
        ], "weight": 3, "status": "active"}]}, ensure_ascii=False),
        "usage": {"inputTokens": 9, "outputTokens": 3}})

    assert await backend.consolidate(conversation, after_journal_id=old_bot.journal_id,
                                     through_journal_id=row_bot.journal_id) is True
    assert len(service.dsh.calls) == 1
    prompt = json.loads(service.dsh.calls[0]["prompt"])
    messages = prompt["messages"]
    assert [row["journal_id"] for row in messages] == [row_a.journal_id, row_b.journal_id,
                                                        row_bot.journal_id]
    assert [(row["actor_id"], row["direction"]) for row in messages] == [
        ("100", "inbound"), ("200", "inbound"), ("900", "outbound")]
    assert "A 说自己存下了工资" in "".join(item["quote"] for item in messages[0]["evidence"])
    assert "B 反驳" in "".join(item["quote"] for item in messages[1]["evidence"])

    saved = await store_snapshot(backend)
    assert saved["cursor"] == row_bot.journal_id
    assert len(saved["memories"]) == 1
    assert saved["memories"][0]["source_ids"] == [row_a.journal_id, row_b.journal_id,
                                                  row_bot.journal_id]
    assert [(claim["text"], claim["sources"][0]["actor_id"])
            for claim in saved["memories"][0]["claims"]] == [
                ("A称自己存下工资", "100"), ("B反驳称A用于充值游戏", "200")]
    assert saved["memories"][0]["claims"][1]["sources"][0]["quote"] == \
        "B 反驳，说 A 用来充值游戏"
    assert backend.last_run["status"] == "completed"
    assert backend.last_run["usage"] == {"input_tokens": 9, "output_tokens": 3}
    source_result = await backend.sources(saved["memories"][0]["id"])
    assert [item["journal_id"] for item in source_result["entries"]] == [
        row_a.journal_id, row_b.journal_id, row_bot.journal_id]
    assert await backend.consolidate(conversation, after_journal_id=old_bot.journal_id,
                                     through_journal_id=row_bot.journal_id) is True
    assert len(service.dsh.calls) == 1
    await backend.close()


@pytest.mark.asyncio
async def test_evidence_id_protocol_restores_exact_quote_and_host_identity(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    body = "A说这件事，B补充了不同细节。" * 80
    row = append(journal, conversation, 1, "200", body)

    def selected_evidence(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        message = prompt["messages"][0]
        assert "content" not in message and "source_ids" not in message
        assert all(len(item["quote"]) <= 320 for item in message["evidence"])
        evidence = message["evidence"][0]
        return {"text": json.dumps({"changes": [{
            "content": "B补充了不同细节", "members": ["100", "200"],
            "claims": [{"text": "B补充了不同细节", "sources": [evidence["evidence_id"]]}],
            "weight": 3, "status": "active"}]}, ensure_ascii=False)}

    service.dsh.responses.append(selected_evidence)
    try:
        assert await backend.consolidate(conversation, after_journal_id=0,
                                         through_journal_id=row.journal_id)
        memory = (await store_snapshot(backend))["memories"][0]
        assert memory["source_ids"] == [row.journal_id]
        source = memory["claims"][0]["sources"][0]
        assert source == {"journal_id": row.journal_id, "quote": body[:320],
                          "actor_id": "200", "direction": "inbound"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_six_evidence_refs_bind_and_commit_without_truncation(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, index, "100", f"第{index}条共同证据")
            for index in range(1, 7)]

    def cite_six(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        evidence_ids = [message["evidence"][0]["evidence_id"] for message in prompt["messages"]]
        assert len(evidence_ids) == 6 and all(type(value) is int for value in evidence_ids)
        return {"text": json.dumps({"changes": [{
            "id": None, "content": "六条消息共同支持的理解", "members": ["100"],
            "claims": [{"text": "六条消息共同支持", "sources": evidence_ids}],
            "weight": 3, "status": "active"}]}, ensure_ascii=False)}

    service.dsh.responses.append(cite_six)
    try:
        assert await backend.consolidate(conversation, after_journal_id=0,
                                         through_journal_id=rows[-1].journal_id)
        memory = (await store_snapshot(backend))["memories"][0]
        sources = memory["claims"][0]["sources"]
        assert len(sources) == 6
        assert [source["journal_id"] for source in sources] == [row.journal_id for row in rows]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_claim_evidence_budget_is_checked_before_journal_io(tmp_path):
    backend, _journal, conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    calls = []
    original = backend.journal.by_journal_id

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    backend.journal.by_journal_id = counted
    evidence_index = {}
    claims = []
    for claim_index in range(20):
        refs = []
        for source_index in range(30):
            ref = claim_index * 30 + source_index + 1
            evidence_index[ref] = {"journal_id": ref, "quote": "证" * 320,
                                   "kind": "text", "actor_id": "100", "direction": "inbound"}
            refs.append(ref)
        claims.append({"text": f"claim {claim_index}", "sources": refs})
    try:
        with pytest.raises(ValueError, match="128 KiB") as caught:
            await backend._bind_evidence_claims(claims, conversation, evidence_index)
        assert caught.value.error_code == "MODEL_OUTPUT_CLAIM_EVIDENCE_TOO_LARGE"
        assert caught.value.error_path == "$.changes[0].claims"
        assert caught.value.diagnostic["claim_evidence_bytes"] > 131_072
        assert calls == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_repeated_evidence_refs_are_checked_and_journal_row_is_read_once(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "同一条精确来源")
    calls = []
    original = backend.journal.by_journal_id

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    backend.journal.by_journal_id = counted
    evidence_index = {number: {"journal_id": row.journal_id, "quote": "同一条精确来源",
                               "kind": "text", "actor_id": "100", "direction": "inbound"}
                     for number in range(1, 101)}
    try:
        result = await backend._bind_evidence_claims(
            [{"text": "同一认识", "sources": list(evidence_index)}],
            conversation, evidence_index)
        assert len(result[0]["sources"]) == 1
        assert result[0]["sources"][0]["journal_id"] == row.journal_id
        assert calls == [row.journal_id]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_legacy_claim_binding_accepts_six_sources_and_caches_each_row(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, index, "100", f"legacy evidence {index}")
            for index in range(1, 7)]
    calls = []
    original = backend.journal.by_journal_id

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    backend.journal.by_journal_id = counted
    source_context = {row.journal_id: {"actor_id": row.message.actor_id,
                                       "direction": row.message.direction}
                     for row in rows}
    claims = [{"text": "六条历史共同支持", "sources": [
        {"journal_id": row.journal_id, "quote": row.message.content["text"]}
        for row in rows]}]
    try:
        result = await backend._bind_claims(claims, conversation,
                                            {row.journal_id for row in rows}, source_context)
        assert len(result[0]["sources"]) == 6
        assert calls == [row.journal_id for row in rows]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_blank_search_query_returns_empty_without_reaching_sql(tmp_path):
    backend, _journal, _conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    original = backend._io
    calls = []

    async def track(action, *args, **kwargs):
        calls.append(action)
        return await original(action, *args, **kwargs)

    backend._io = track
    try:
        assert await backend.search(" \t\n ") == []
        assert calls == ["snapshot"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_unknown_evidence_id_is_rejected_and_private_diagnostic_is_actionable(tmp_path):
    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "可引用的真实消息")
    response_text = json.dumps({"changes": [{
        "content": "不能提交的理解", "members": ["100"],
        "claims": [{"text": "不能提交", "sources": ["ev-forged"]}],
        "weight": 2, "status": "active"}]}, ensure_ascii=False)
    service.dsh.responses.append({"text": response_text})
    try:
        with pytest.raises(ValueError, match="unknown evidence ID"):
            await backend.consolidate(conversation, after_journal_id=0,
                                      through_journal_id=row.journal_id)
        assert (await store_snapshot(backend))["cursor"] == 0
        with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
            diagnostic = json.loads(db.execute(
                "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
        assert diagnostic["category"] == "EVIDENCE_ID_VALIDATION_FAILED"
        assert backend.last_run["error_code"] == "MEMORY_UNKNOWN_EVIDENCE_ID"
        assert diagnostic["error_path"] == "$.changes[0].claims[0].sources[0]"
        assert diagnostic["response_sha256"] == hashlib.sha256(response_text.encode()).hexdigest()
        assert diagnostic["prompt_sha256"]
        assert diagnostic["evidence_reference_count"] == 1
        assert len(diagnostic["evidence_reference_sha256"]) == 64
        assert "ev-forged" in diagnostic["response_preview"]
        assert diagnostic["route"] == "test-provider/test-model"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_rejected_evidence_id_after_preview_limit_is_preserved_in_diagnostic(tmp_path):
    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "真实来源")
    forged = "ev-forged-after-preview-" + "z" * 120
    response_text = json.dumps({"changes": [{
        "content": "x" * 3000, "id": None, "members": ["100"],
        "claims": [{"text": "错误引用", "sources": [forged]}],
        "weight": 2, "status": "active"}]}, ensure_ascii=False)
    service.dsh.responses.append({"text": response_text})
    try:
        with pytest.raises(ValueError, match="unknown evidence ID"):
            await backend.consolidate(conversation, after_journal_id=0,
                                      through_journal_id=row.journal_id)
        with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
            diagnostic = json.loads(db.execute(
                "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
        assert diagnostic["response_preview_truncated"] is True
        assert forged not in diagnostic["response_preview"]
        assert diagnostic["offending_evidence_id"] == forged
        assert len(json.dumps(diagnostic, ensure_ascii=False).encode()) <= 16_000
    finally:
        await backend.close()


@pytest.mark.parametrize("invalid_source", [{}, []])
@pytest.mark.asyncio
async def test_non_string_evidence_reference_has_structured_validation_error(
        tmp_path, invalid_source):
    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "真实来源")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "无法提交", "id": None, "members": ["100"],
        "claims": [{"text": "陈述", "sources": [invalid_source]}],
        "weight": 2, "status": "active"}]}, ensure_ascii=False)})
    try:
        with pytest.raises(ValueError, match="unknown evidence ID"):
            await backend.consolidate(conversation, after_journal_id=0,
                                      through_journal_id=row.journal_id)
        assert backend.last_run["error_code"] == "MEMORY_UNKNOWN_EVIDENCE_ID"
        with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
            diagnostic = json.loads(db.execute(
                "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
        assert diagnostic["error_path"] == "$.changes[0].claims[0].sources[0]"
        assert diagnostic["offending_evidence_type"] in {"dict", "list"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_existing_prior_quote_crossing_chunk_boundary_can_be_reselected_and_committed(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    body = "甲" * 319 + "喜欢苹果" + "乙" * 321
    source_row = append(journal, conversation, 1, "100", body)
    await backend._io("commit", 0, 0, source_row.journal_id, [{
        "id": None, "content": "A喜欢苹果", "members": ["100"],
        "source_ids": [source_row.journal_id], "claims": [{"text": "A喜欢苹果", "sources": [{
            "journal_id": source_row.journal_id, "quote": "喜欢苹果",
            "actor_id": "100", "direction": "inbound"}]}],
        "weight": 3, "status": "active"}], {})
    memory_id = (await store_snapshot(backend))["memories"][0]["id"]
    new_row = append(journal, conversation, 2, "100", "今天又提到苹果。")

    def select_prior_quote(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        memory = next(item for item in prompt["existing_memories"] if item["id"] == memory_id)
        evidence = next(item for item in memory["evidence"] if item["quote"] == "喜欢苹果")
        assert evidence["actor_id"] == "100" and evidence["direction"] == "inbound"
        assert memory["claims"][0]["sources"] == [evidence["evidence_id"]]
        return {"text": json.dumps({"changes": [{
            "id": memory_id, "content": "A持续喜欢苹果", "members": ["100"],
            "claims": [{"text": "A持续喜欢苹果", "sources": [evidence["evidence_id"]]}],
            "weight": 3, "status": "active"}]}, ensure_ascii=False)}

    service.dsh.responses.append(select_prior_quote)
    try:
        assert await backend.consolidate(conversation, after_journal_id=source_row.journal_id,
                                         through_journal_id=new_row.journal_id)
        memory = (await store_snapshot(backend))["memories"][0]
        assert memory["claims"][0]["sources"][0]["quote"] == "喜欢苹果"
        assert memory["claims"][0]["sources"][0]["journal_id"] == source_row.journal_id
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_revision_of_memory_with_over_200_sources_preserves_history(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, index, "100", f"历史消息{index}")
            for index in range(1, 202)]
    source_ids = [row.journal_id for row in rows]
    await backend._io("commit", 0, 0, source_ids[-2], [{
        "id": None, "content": "长期记忆", "members": ["100"],
        "source_ids": source_ids[:-1], "claims": [{"text": "第一条历史", "sources": [{
            "journal_id": rows[0].journal_id, "quote": "历史消息1",
            "actor_id": "100", "direction": "inbound"}]}],
        "weight": 3, "status": "active"}], {})
    prior = (await store_snapshot(backend))["memories"][0]
    memory_id = prior["id"]
    await backend._io("commit", 1, source_ids[-2], source_ids[-1], [{
        "id": memory_id, "content": prior["content"], "members": prior["members"],
        "source_ids": [source_ids[-1]], "claims": prior["claims"],
        "weight": 3, "status": "active"}], {})
    new_row = append(journal, conversation, 202, "100", "补充一条当前经历")

    def select_new_evidence(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        message = next(item for item in prompt["messages"] if item["journal_id"] == new_row.journal_id)
        return {"text": json.dumps({"changes": [{
            "id": memory_id, "content": "长期记忆并包含当前经历", "members": ["100"],
            "claims": [{"text": "当前新增经历", "sources": [
                message["evidence"][0]["evidence_id"]]}],
            "weight": 3, "status": "active"}]}, ensure_ascii=False)}

    service.dsh.responses.append(select_new_evidence)
    try:
        assert await backend.consolidate(conversation, after_journal_id=source_ids[-1],
                                         through_journal_id=new_row.journal_id)
        saved = (await store_snapshot(backend))["memories"][0]
        assert len(saved["source_ids"]) == 202
        assert set(source_ids).issubset(saved["source_ids"])
        assert new_row.journal_id in saved["source_ids"]
        assert saved["claims"][0]["sources"][0]["journal_id"] == new_row.journal_id
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_failed_model_run_persists_only_safe_error_code(tmp_path):
    class ProviderFailure(RuntimeError):
        error_code = "RATE_LIMITED"
        status = 503

    backend, journal, conversation, _workspace, _service = make_backend(
        tmp_path, responses=[lambda **_: (_ for _ in ()).throw(
            ProviderFailure("SECRET api-key=must-not-be-persisted"))])
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "trigger")
    with pytest.raises(ProviderFailure):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    assert backend.last_run["status"] == "failed"
    assert backend.last_run["error_code"] == "RATE_LIMITED"
    assert "SECRET" not in repr(backend.last_run)
    assert "api-key" not in repr(backend.last_run)
    state = await backend.incremental_state()
    assert (state["failed_after"], state["failed_through"], state["error_code"]) == (
        0, 1, "RATE_LIMITED")
    await backend.close()


@pytest.mark.asyncio
async def test_multiple_quotes_from_one_real_message_keep_host_bound_speaker(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "200", "B说：A攒了工资；但我认为他充值游戏了。")
    sources = [{"journal_id": row.journal_id, "quote": quote}
               for quote in ("A攒了工资", "我认为他充值游戏了", "A攒了工资")]
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "B提及A攒工资的说法，也提出充值游戏的不同理解", "members": ["100", "200"],
        "source_ids": [row.journal_id], "weight": 3, "status": "active",
        "claims": [{"text": "B同时转述并反驳A的说法", "sources": sources}]}]}, ensure_ascii=False)})
    try:
        await backend.consolidate(conversation, after_journal_id=0, through_journal_id=row.journal_id)
        saved = await store_snapshot(backend)
        evidence = saved["memories"][0]["claims"][0]["sources"]
        assert len(evidence) == 2 and {ref["actor_id"] for ref in evidence} == {"200"}
        assert saved["cursor"] == row.journal_id
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_claim_sources_are_added_when_model_omits_them_from_source_ids(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    listed = append(journal, conversation, 1, "100", "A提到一件事")
    cited = append(journal, conversation, 2, "200", "B补充了关键细节")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "A提到事情，B补充关键细节", "members": ["100", "200"],
        "source_ids": [listed.journal_id], "weight": 3, "status": "active",
        "claims": [{"text": "B补充关键细节", "sources": [{
            "journal_id": cited.journal_id, "quote": "B补充了关键细节"}]}],
    }]}, ensure_ascii=False)})
    try:
        assert await backend.consolidate(conversation, after_journal_id=0,
                                         through_journal_id=cited.journal_id)
        memory = (await store_snapshot(backend))["memories"][0]
        assert memory["source_ids"] == [listed.journal_id, cited.journal_id]
        assert memory["claims"][0]["sources"][0]["journal_id"] == cited.journal_id
    finally:
        await backend.close()


def test_claim_source_union_preserves_claims_beyond_legacy_input_limit():
    claims = [{"text": "claim", "sources": [{"journal_id": 201, "quote": "quote"}]}]
    assert _include_claim_source_ids(list(range(1, 201)), claims) == list(range(1, 202))
    with pytest.raises(ValueError, match="too many source IDs"):
        _include_claim_source_ids(list(range(1, 202)), [])


@pytest.mark.asyncio
async def test_claim_quote_must_be_exact_source_text_and_speaker_is_host_bound(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    warnings = []
    backend.logger = SimpleNamespace(warning=lambda *args: warnings.append(args))
    await backend.initialize()
    row = append(journal, conversation, 1, "200", "B 反驳，说 A 用来充值游戏")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "A拿工资充值游戏", "members": ["100"], "source_ids": [row.journal_id],
        "claims": [{"text": "A拿工资充值游戏", "sources": [{
            "journal_id": row.journal_id, "quote": "A用工资充值游戏"}]}],
        "weight": 3, "status": "active"}]}, ensure_ascii=False)})
    with pytest.raises(ValueError, match="quote does not match"):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    saved = await store_snapshot(backend)
    assert saved["cursor"] == 0 and saved["memories"] == []
    state = await backend.incremental_state()
    assert state["failed_through"] == row.journal_id
    with sqlite3.connect(_workspace / ".v2-memory/state.sqlite3") as db:
        diagnostic = json.loads(db.execute(
            "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    assert diagnostic["category"] == "SOURCE_QUOTE_MISMATCH"
    assert diagnostic["validation_error"] == "model source quote does not match Journal"
    assert diagnostic["journal_id"] == row.journal_id
    assert diagnostic["source_kind"] == "batch_journal"
    assert (diagnostic["change_index"], diagnostic["claim_index"],
            diagnostic["source_index"]) == (0, 0, 0)
    assert diagnostic["route"] == "test-provider/test-model"
    guide = (REPO / "dsh/plugins/agent-profile/memory-evidence-guide.md").read_bytes()
    assert diagnostic["guide_sha256"] == hashlib.sha256(guide).hexdigest()
    assert diagnostic["quote"] == "A用工资充值游戏"
    assert diagnostic["model_visible"]["batch_journal"]["text_preview"] == "B 反驳，说 A 用来充值游戏"
    assert diagnostic["journal_at_validation"]["text_preview"] == "B 反驳，说 A 用来充值游戏"
    assert diagnostic["quote_matches_journal_text"] is False
    assert diagnostic["quote_matches_journal_file_name"] is False
    assert diagnostic["batch_text_changed_since_prompt"] is False
    assert diagnostic["batch_file_names_changed_since_prompt"] is False
    assert "A用工资充值游戏" not in repr(warnings)
    assert "B 反驳，说 A 用来充值游戏" not in repr(warnings)
    assert saved["cursor"] == 0
    await backend.close()


@pytest.mark.asyncio
async def test_diagnostic_write_failure_preserves_validation_error_and_cursor(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    warnings = []
    backend.logger = SimpleNamespace(warning=lambda *args: warnings.append(args))
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "私有原文不能写入公共日志")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "需要拒绝的记忆", "members": ["100"], "source_ids": [row.journal_id],
        "claims": [{"text": "不应被持久化的陈述", "sources": [{
            "journal_id": row.journal_id, "quote": "伪造引句"}]}],
        "weight": 3, "status": "active"}]}, ensure_ascii=False)})
    sync = backend._sync

    def fail_diagnostic(action, *args, **kwargs):
        if action == "record_validation_diagnostic":
            raise OSError("diagnostic volume unavailable")
        return sync(action, *args, **kwargs)

    backend._sync = fail_diagnostic
    with pytest.raises(ValueError, match="quote does not match") as caught:
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    assert type(caught.value) is ValueError
    assert "伪造引句" not in repr(warnings)
    assert "私有原文不能写入公共日志" not in repr(warnings)
    snapshot = await store_snapshot(backend)
    state = await backend.incremental_state()
    assert snapshot["cursor"] == 0 and snapshot["memories"] == []
    assert state["failed_after"] == 0 and state["failed_through"] == row.journal_id
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_write_fails", [False, True])
async def test_cancel_while_diagnostic_is_blocked_persists_failure_before_propagating(
        tmp_path, diagnostic_write_fails):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "触发来源校验失败")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "将被拒绝的记忆", "members": ["100"], "source_ids": [row.journal_id],
        "claims": [{"text": "不应提交", "sources": [{
            "journal_id": row.journal_id, "quote": "不存在的引句"}]}],
        "weight": 3, "status": "active"}]}, ensure_ascii=False)})
    diagnostic_started, release_diagnostic = threading.Event(), threading.Event()
    sync = backend._sync

    def block_diagnostic(action, *args, **kwargs):
        if action == "record_validation_diagnostic":
            diagnostic_started.set()
            if not release_diagnostic.wait(timeout=5):
                raise TimeoutError("test diagnostic release timed out")
            if diagnostic_write_fails:
                raise OSError("diagnostic volume unavailable")
        return sync(action, *args, **kwargs)

    backend._sync = block_diagnostic
    task = asyncio.create_task(backend.consolidate(
        conversation, after_journal_id=0, through_journal_id=row.journal_id))
    assert await asyncio.to_thread(diagnostic_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    release_diagnostic.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = await store_snapshot(backend)
    state = await backend.incremental_state()
    progress = backend.progress_snapshot()
    assert saved["cursor"] == 0 and saved["memories"] == []
    assert state["failed_after"] == 0 and state["failed_through"] == row.journal_id
    assert state["error_code"] == "MEMORY_SOURCE_QUOTE_MISMATCH"
    assert backend.last_run["status"] == "failed"
    assert backend.last_run["error_code"] == "MEMORY_SOURCE_QUOTE_MISMATCH"
    assert progress["active"] is False and progress["phase"] == "failed"
    await backend.close()


@pytest.mark.asyncio
async def test_late_journal_update_is_compared_with_exact_model_visible_snapshot(tmp_path):
    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    receipt = JOURNAL.Message(
        platform_id=conversation.platform_id, bot_id=conversation.bot_id,
        conversation_key=conversation.key, conversation_type="group", direction="outbound",
        actor_id="900", actor_name=None, source="receipt", message_id="late-feedback-1",
        reply_to=None, content={"text": "迟到反馈前的临时回执正文", "segments": [
            {"type": "text", "text": "迟到反馈前的临时回执正文"}]},
        created_at=1, observed_at_ns=time.time_ns())
    row = journal.append(receipt)[0]

    def late_feedback(**_kwargs):
        canonical = JOURNAL.Message(
            platform_id=conversation.platform_id, bot_id=conversation.bot_id,
            conversation_key=conversation.key, conversation_type="group", direction="outbound",
            actor_id="900", actor_name=None, source="unknown", message_id="late-feedback-1",
            reply_to=None, content={"text": "迟到的权威反馈内容", "segments": [
                {"type": "text", "text": "迟到的权威反馈内容"}]},
            created_at=2, observed_at_ns=time.time_ns())
        updated, inserted = journal.append(canonical)
        assert inserted is False and updated.journal_id == row.journal_id
        return {"text": json.dumps({"changes": [{
            "content": "根据临时回执产生的记忆", "members": ["900"],
            "source_ids": [row.journal_id], "claims": [{
                "text": "根据临时回执产生的陈述", "sources": [{
                    "journal_id": row.journal_id, "quote": "迟到反馈前的临时回执正文"}]}],
            "weight": 3, "status": "active"}]}, ensure_ascii=False)}

    service.dsh.responses.append(late_feedback)
    with pytest.raises(ValueError, match="quote does not match"):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
        diagnostic = json.loads(db.execute(
            "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    assert diagnostic["source_kind"] == "batch_journal"
    assert diagnostic["model_visible"]["batch_journal"]["text_preview"] == "迟到反馈前的临时回执正文"
    assert diagnostic["journal_at_validation"]["text_preview"] == "迟到的权威反馈内容"
    assert diagnostic["batch_text_changed_since_prompt"] is True
    saved = await store_snapshot(backend)
    assert saved["cursor"] == 0 and saved["memories"] == []
    await backend.close()


@pytest.mark.asyncio
async def test_quote_from_reply_metadata_is_rejected_but_content_is_diagnosable(tmp_path):
    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    message = JOURNAL.message_from_onebot(conversation.platform_id, {
        "post_type": "message", "message_type": "group", "self_id": 900001,
        "user_id": 200, "group_id": 12345,
        "message": [{"type": "reply", "data": {"id": "reply-target-77"}}],
    }, observed_at_ns=time.time_ns())
    row = journal.append(message)[0]
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "根据回复目标产生的记忆", "members": ["200"],
        "source_ids": [row.journal_id], "claims": [{
            "text": "根据回复目标产生的陈述", "sources": [{
                "journal_id": row.journal_id, "quote": "reply-target-77"}]}],
        "weight": 3, "status": "active"}]}, ensure_ascii=False)})

    with pytest.raises(ValueError, match="quote does not match"):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
        diagnostic = json.loads(db.execute(
            "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    visible = diagnostic["model_visible"]["batch_journal"]
    checked = diagnostic["journal_at_validation"]
    assert "reply-target-77" not in visible["content_preview"]
    assert "reply-target-77" in checked["content_preview"]
    assert visible["content_sha256"] != checked["content_sha256"]
    assert diagnostic["batch_content_changed_since_prompt"] is False
    assert diagnostic["quote_matches_journal_text"] is False
    assert diagnostic["quote_matches_journal_file_name"] is False
    assert (await store_snapshot(backend))["cursor"] == 0
    await backend.close()


def test_content_diagnostic_excludes_binary_and_bounds_preview():
    from v2_memory.backend import _content_diagnostic

    first_content = {
        "text": "ordinary content",
        "segments": [{"type": "image", "file_id": "image-7", "base64_payload": "BINARY_ONE"}],
    }
    second_content = {
        "text": "ordinary content",
        "segments": [{"type": "image", "file_id": "image-7", "base64_payload": "BINARY_TWO"}],
    }
    result = _content_diagnostic(first_content)
    assert "image-7" in result["content_preview"]
    assert "BINARY_ONE" not in result["content_preview"]
    assert len(result["content_preview"]) <= 768
    assert result["content_sha256"] != _content_diagnostic(second_content)["content_sha256"]

    oversized_a = _content_diagnostic({"text": "ordinary", "metadata": "A" * 5000})
    oversized_b = _content_diagnostic({"text": "ordinary", "metadata": "B" * 5000})
    assert oversized_a["content_preview"] == oversized_b["content_preview"]
    assert oversized_a["content_sha256"] != oversized_b["content_sha256"]


@pytest.mark.asyncio
async def test_metadata_quote_after_long_text_has_private_field_path_diagnostic(tmp_path):
    from v2_memory.backend import _content_quote_paths

    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    message = JOURNAL.Message(
        platform_id=conversation.platform_id, bot_id=conversation.bot_id,
        conversation_key=conversation.key, conversation_type="group", direction="inbound",
        actor_id="200", actor_name=None, source="user", message_id="metadata-after-long-text",
        reply_to="reply-target-after-preview",
        content={"text": "很长的普通正文" * 200, "segments": [
            {"type": "reply", "message_id": "reply-target-after-preview"}]},
        created_at=1, observed_at_ns=time.time_ns())
    row = journal.append(message)[0]
    quote = "reply-target-after-preview"
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "引用了回复目标", "members": ["200"], "source_ids": [row.journal_id],
        "claims": [{"text": "引用回复目标的陈述", "sources": [
            {"journal_id": row.journal_id, "quote": quote}]}],
        "weight": 3, "status": "active"}]}, ensure_ascii=False)})

    assert _content_quote_paths(message.content, quote) == ["segments[0].message_id"]
    with pytest.raises(ValueError, match="quote does not match"):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
        diagnostic = json.loads(db.execute(
            "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    assert quote not in diagnostic["model_visible"]["batch_journal"]["content_preview"]
    assert diagnostic["quote_matches_searchable_model_fields"] is False
    assert diagnostic["model_content_search_scope"] == \
        "non-binary string fields up to 4096 characters, 12 levels, 8 paths"
    assert diagnostic["model_visible_content_match_paths"] == []
    assert (await store_snapshot(backend))["cursor"] == 0
    await backend.close()


def test_model_visible_source_context_marks_prior_memory_references():
    from v2_memory.backend import _model_visible_source_context

    context = _model_visible_source_context({
        "messages": [{"journal_id": 12, "content": {"text": "当前批次消息", "segments": []}}],
        "existing_memories": [{"claims": [{"sources": [
            {"journal_id": 7, "quote": "旧记忆保留的来源引句"},
            {"journal_id": 12, "quote": "同一ID的既有记忆引句"},
        ]}]}],
    })
    assert context[12]["kinds"] == ["batch_journal", "prior_memory_reference"]
    assert context[12]["batch_journal"]["text_preview"] == "当前批次消息"
    assert context[7]["kinds"] == ["prior_memory_reference"]
    assert context[7]["prior_memory_references"][0]["quote_preview"] == "旧记忆保留的来源引句"


def test_diagnostic_source_hashes_all_file_names_not_only_previews():
    from v2_memory.backend import _diagnostic_source

    before = _diagnostic_source("", ["a", "b", "c", "fourth-old"])
    after = _diagnostic_source("", ["a", "b", "c", "fourth-new"])
    assert before["file_names_count"] == after["file_names_count"] == 4
    assert before["file_names"] == after["file_names"]
    assert before["file_names_sha256"] != after["file_names_sha256"]


@pytest.mark.asyncio
async def test_claim_quote_can_match_journal_file_segment_name(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    message = JOURNAL.message_from_group_upload(conversation.platform_id, {
        "post_type": "notice", "notice_type": "group_upload", "self_id": 900001,
        "group_id": 12345, "user_id": 200, "time": 1,
        "file": {"id": "opaque-id", "busid": 102,
                 "name": "planning-report.pdf", "file_size": 1024}},
        observed_at_ns=time.time_ns(),
    )
    row = journal.append(message)[0]
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "B上传了规划报告", "members": ["200"],
        "source_ids": [row.journal_id], "weight": 3, "status": "active",
        "claims": [{"text": "B上传了规划报告", "sources": [{
            "journal_id": row.journal_id, "quote": "planning-report.pdf"}]}],
    }]}, ensure_ascii=False)})
    try:
        assert await backend.consolidate(conversation, after_journal_id=0,
                                         through_journal_id=row.journal_id)
        saved = await store_snapshot(backend)
        evidence = saved["memories"][0]["claims"][0]["sources"][0]
        assert evidence["quote"] == "planning-report.pdf"
        assert evidence["actor_id"] == "200"
        assert saved["cursor"] == row.journal_id
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["opaque-id", "https://files.invalid/x", "1024"])
async def test_claim_quote_cannot_match_arbitrary_file_metadata(tmp_path, quote):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    message = JOURNAL.message_from_group_upload(conversation.platform_id, {
        "post_type": "notice", "notice_type": "group_upload", "self_id": 900001,
        "group_id": 12345, "user_id": 200, "time": 1,
        "file": {"id": "opaque-id", "busid": 102,
                 "name": "planning-report.pdf", "file_size": 1024,
                 "url": "https://files.invalid/x"}},
        observed_at_ns=time.time_ns(),
    )
    row = journal.append(message)[0]
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "content": "B上传了文件", "members": ["200"],
        "source_ids": [row.journal_id], "weight": 3, "status": "active",
        "claims": [{"text": "B上传了文件", "sources": [{
            "journal_id": row.journal_id, "quote": quote}]}],
    }]}, ensure_ascii=False)})
    try:
        with pytest.raises(ValueError, match="quote does not match"):
            await backend.consolidate(conversation, after_journal_id=0,
                                      through_journal_id=row.journal_id)
        saved = await store_snapshot(backend)
        assert saved["cursor"] == 0 and saved["memories"] == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_unclassified_failure_uses_fixed_safe_error_code(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(
        tmp_path, responses=[lambda **_: (_ for _ in ()).throw(
            RuntimeError("SECRET api-key=must-not-be-persisted"))])
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "trigger")
    with pytest.raises(RuntimeError):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    assert backend.last_run["error_code"] == "MEMORY_CONSOLIDATION_FAILED"
    assert "SECRET" not in repr(backend.last_run)
    await backend.close()


@pytest.mark.asyncio
async def test_model_source_ids_must_be_real_and_bound_to_this_group(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    foreign = JOURNAL.Conversation("napcat", "900001", "group:99999")
    foreign_row = append(journal, foreign, 1, "200", "另一个群的秘密")
    await backend.initialize()
    own = append(journal, conversation, 2, "100", "当前群经历")
    service.dsh.responses.append(response([change([foreign_row.journal_id])]))

    with pytest.raises(ValueError, match="unknown source"):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=own.journal_id)
    assert (await store_snapshot(backend))["cursor"] == 0
    assert (await store_snapshot(backend))["memories"] == []

    # Even a tampered stored reference is checked again before raw Journal reads.
    await backend._io("commit", 0, 0, 0, [change([foreign_row.journal_id])], {})
    saved = await store_snapshot(backend)
    with pytest.raises(MemoryUnavailable):
        await backend.sources(saved["memories"][0]["id"])
    await backend.close()


@pytest.mark.asyncio
async def test_prepare_reset_uses_group_guard_and_failure_does_not_advance_cursor(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    before = append(journal, conversation, 1, "100", "启用前消息")
    await backend.initialize()
    current = append(journal, conversation, 2, "100", "需要整理的消息")
    service.dsh.responses.append(response([change([999999])]))

    async with backend.guard(conversation):
        pending = asyncio.create_task(backend.prepare_reset(
            conversation=conversation, through_journal_id=current.journal_id, session_id="sid"
        ))
        await asyncio.sleep(0.02)
        assert not pending.done()
    with pytest.raises(ValueError):
        await pending
    assert (await store_snapshot(backend))["cursor"] == before.journal_id
    assert (await store_snapshot(backend))["memories"] == []
    service.dsh.responses.append(response([change([current.journal_id])]))
    assert await backend.prepare_reset(conversation=conversation,
                                       through_journal_id=current.journal_id,
                                       session_id="sid") is True
    assert (await store_snapshot(backend))["cursor"] == current.journal_id
    await backend.close()


@pytest.mark.asyncio
async def test_two_batches_keep_first_atomic_cursor_when_second_fails(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    own_rows = [append(journal, conversation, i, "100", f"当前群消息 {i}")
                for i in range(1, 102)]
    foreign = JOURNAL.Conversation("napcat", "900001", "group:99999")
    foreign_row = append(journal, foreign, 102, "200", "其他群消息")
    service.dsh.responses.extend([
        response([change([row.journal_id for row in own_rows[:100]], content="第一批理解")]),
        response([change([foreign_row.journal_id], content="不应提交第二批")]),
    ])

    with pytest.raises(ValueError, match="unknown source"):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=own_rows[-1].journal_id)
    saved = await store_snapshot(backend)
    assert saved["cursor"] == own_rows[99].journal_id
    assert len(saved["memories"]) == 1
    assert saved["memories"][0]["content"] == "第一批理解"
    assert len(service.dsh.calls) == 2
    await backend.close()


@pytest.mark.asyncio
async def test_native_short_window_splits_long_journal_rows_into_complete_contiguous_batches(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    set_memory_capacity(service, context=60_000, output=2_000)
    await backend.initialize()
    texts = [f"消息{i:03d}🙂" * 400 for i in range(121)]
    rows = [append(journal, conversation, i + 1, "100", text)
            for i, text in enumerate(texts)]
    service.dsh.responses.extend(response([]) for _ in rows)

    assert await backend.consolidate(conversation, after_journal_id=0,
                                     through_journal_id=rows[-1].journal_id) is True
    consumed = []
    for call in service.dsh.calls:
        batch = json.loads(call["prompt"])["messages"]
        ids = [item["journal_id"] for item in batch]
        assert ids == list(range(ids[0], ids[-1] + 1))
        consumed.extend(ids)
    expected_ids = [row.journal_id for row in rows]
    assert consumed == expected_ids
    assert ["".join(evidence["quote"] for evidence in item["evidence"])
            for call in service.dsh.calls
            for item in json.loads(call["prompt"])["messages"]] == texts
    assert 1 < len(service.dsh.calls) < len(rows)
    assert (await store_snapshot(backend))["cursor"] == rows[-1].journal_id
    await backend.close()


@pytest.mark.asyncio
async def test_actual_payload_over_old_256k_limit_is_accepted_with_native_capacity(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    set_memory_capacity(service, context=1_000_000, output=10_000)
    await backend.initialize()
    texts = [f"row-{index:02d}-" + "x" * 4_000 for index in range(80)]
    rows = [append(journal, conversation, i + 1, "100", text)
            for i, text in enumerate(texts)]
    service.dsh.responses.append(response([]))

    assert await backend.consolidate(conversation, after_journal_id=0,
                                     through_journal_id=rows[-1].journal_id) is True
    assert len(service.dsh.calls) == 1
    assert backend.last_plan["input_bytes"] > 256_000
    selected = json.loads(service.dsh.calls[0]["prompt"])["messages"]
    assert [item["journal_id"] for item in selected] == [row.journal_id for row in rows]
    assert ["".join(evidence["quote"] for evidence in item["evidence"])
            for item in selected] == texts
    assert (await store_snapshot(backend))["cursor"] == rows[-1].journal_id
    await backend.close()


@pytest.mark.asyncio
async def test_eighty_large_existing_memories_are_compacted_without_blocking_journal(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    set_memory_capacity(service, context=70_000, output=10_000)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "SPECIAL new event")
    old_memories = []
    for index in range(80):
        prefix = ("SPECIAL " if index < 20 else "ordinary ") + f"memory-{index:02d} "
        item = change([row.journal_id], content=prefix + "x" * (4_000 - len(prefix)))
        item["weight"] = 1 if index < 20 else 5
        old_memories.append(item)
    await backend._io("commit", 0, 0, 0, old_memories, {"actor": "memory_agent"})
    service.dsh.responses.append(response([]))

    assert await backend.consolidate(conversation, after_journal_id=0,
                                     through_journal_id=row.journal_id) is True
    assert len(service.dsh.calls) == 1
    assert backend.last_plan["candidate_memories"] == 80
    assert 0 < backend.last_plan["selected_memories"] < 80
    assert backend.last_plan["input_bytes"] <= backend.last_plan["input_allowance"]
    assert json.loads(service.dsh.calls[0]["prompt"])["messages"][0]["journal_id"] == row.journal_id
    assert (await store_snapshot(backend))["cursor"] == row.journal_id
    await backend.close()


@pytest.mark.asyncio
async def test_first_oversized_journal_row_fails_without_model_call_or_cursor_progress(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    set_memory_capacity(service, context=10_000, output=1_000)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "🙂" * 20_000)

    with pytest.raises(BatchCapacityError) as caught:
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    assert caught.value.error_code == "MEMORY_MESSAGE_TOO_LARGE"
    assert not service.dsh.calls
    assert (await store_snapshot(backend))["cursor"] == 0
    assert backend.last_run["error_code"] == "MEMORY_MESSAGE_TOO_LARGE"
    await backend.close()


@pytest.mark.asyncio
async def test_missing_native_capacity_fails_without_calling_model(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    service.dsh.memory_capacity = None
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "内容")

    with pytest.raises(BatchCapacityError) as caught:
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=row.journal_id)
    assert caught.value.error_code == "MEMORY_CAPACITY_UNKNOWN"
    assert not service.dsh.calls
    assert (await store_snapshot(backend))["cursor"] == 0
    assert backend.last_run["error_code"] == "MEMORY_CAPACITY_UNKNOWN"
    await backend.close()


@pytest.mark.asyncio
async def test_second_batch_model_output_limit_keeps_first_batch_and_does_not_retry(tmp_path):
    class OutputLimit(RuntimeError):
        error_code = "MODEL_OUTPUT_LIMIT"

    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, i + 1, "100", f"内容{i}")
            for i in range(101)]

    def fail_output(**_kwargs):
        raise OutputLimit("output token limit")

    service.dsh.responses.extend([response([]), fail_output])
    with pytest.raises(OutputLimit):
        await backend.consolidate(conversation, after_journal_id=0,
                                  through_journal_id=rows[-1].journal_id)
    saved = await store_snapshot(backend)
    assert saved["cursor"] == rows[99].journal_id
    assert len(service.dsh.calls) == 2
    assert backend.last_run["error_code"] == "MODEL_OUTPUT_LIMIT"
    await backend.close()


@pytest.mark.parametrize("mutation", ["edit", "disable"])
@pytest.mark.asyncio
async def test_manual_edit_or_disable_during_model_call_wins_cas(tmp_path, mutation):
    started, release = threading.Event(), threading.Event()

    def blocked_model(**_kwargs):
        started.set()
        assert release.wait(5)
        return response([change([row.journal_id], content="模型旧快照")])

    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "新的消息")
    initial = await backend._io("commit", 0, 0, 0,
                               [change([row.journal_id], content="初始理解")], {})
    memory_id = initial["memories"][0]["id"]
    service.dsh.responses.append(blocked_model)
    run = asyncio.create_task(backend.consolidate(conversation, after_journal_id=0,
                                                   through_journal_id=row.journal_id))
    assert await asyncio.to_thread(started.wait, 2)
    if mutation == "edit":
        manual = change([row.journal_id], content="Owner 手动修改", memory_id=memory_id)
        manual.pop("claims")
        await backend._io("update", 1, [manual], {"actor": "dashboard_owner"})
    else:
        await backend._io("set_enabled", False, 1)
    release.set()
    with pytest.raises(RevisionConflict):
        await run

    saved = await store_snapshot(backend)
    assert saved["cursor"] == 0
    if mutation == "edit":
        assert saved["memories"][0]["content"] == "Owner 手动修改"
    else:
        assert saved["enabled"] is False
        assert saved["memories"][0]["id"] == memory_id
    await backend.close()


@pytest.mark.asyncio
async def test_cancel_sets_worker_event_and_cannot_commit_late_result(tmp_path):
    started, observed_cancel = threading.Event(), threading.Event()

    def cancellable_model(cancel_event, **_kwargs):
        started.set()
        while not cancel_event.wait(0.01):
            pass
        observed_cancel.set()
        return response([change([row.journal_id], content="迟到的模型结果")])

    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "需要整理")
    service.dsh.responses.append(cancellable_model)
    run = asyncio.create_task(backend.consolidate(conversation, after_journal_id=0,
                                                   through_journal_id=row.journal_id))
    assert await asyncio.to_thread(started.wait, 2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert observed_cancel.is_set()
    assert (await store_snapshot(backend))["cursor"] == 0
    assert (await store_snapshot(backend))["memories"] == []
    await asyncio.sleep(0.03)
    assert (await store_snapshot(backend))["cursor"] == 0
    await backend.close()


@pytest.mark.asyncio
async def test_removed_workspace_cannot_be_read_or_recreated_on_host(tmp_path):
    backend, _journal, _conversation, workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    shutil.rmtree(workspace)
    with pytest.raises((FileNotFoundError, MemoryUnavailable, OSError)):
        await backend.snapshot()
    assert not (workspace / ".v2-memory/state.sqlite3").exists()
    await backend.close()


@pytest.mark.asyncio
async def test_manage_edit_worker_is_drained_by_close_and_closed_backend_rejects_reads(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path)
    row = append(journal, conversation, 1, "100", "要编辑的来源")
    await backend.initialize()
    created = await backend._io("commit", 0, row.journal_id, row.journal_id,
                               [change([row.journal_id], content="旧内容")], {})
    memory_id = created["memories"][0]["id"]

    entered, release = threading.Event(), threading.Event()
    original_sync = backend._sync

    def blocked_sync(action, *args, **kwargs):
        if action == "update":
            entered.set()
            assert release.wait(5)
        return original_sync(action, *args, **kwargs)

    backend._sync = blocked_sync
    edit = asyncio.create_task(backend.manage("edit", {
        "id": memory_id, "expected_revision": 1, "content": "Owner 新内容",
        "members": ["100", "200"], "weight": 4, "status": "active",
    }))
    assert await asyncio.to_thread(entered.wait, 2)
    closing = asyncio.create_task(backend.close())
    await asyncio.sleep(0.03)
    assert not closing.done()
    release.set()
    await closing
    assert await edit == {"ok": True}
    with pytest.raises(MemoryUnavailable, match="closed"):
        await backend.snapshot()
    assert original_sync("get", memory_id)["content"] == "Owner 新内容"


@pytest.mark.asyncio
async def test_search_and_source_lookup_recheck_disable_and_clear(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path)
    row = append(journal, conversation, 1, "100", "可检索的来源")
    await backend.initialize()
    created = await backend._io("commit", 0, row.journal_id, row.journal_id,
                               [change([row.journal_id], content="可检索记忆")], {})
    memory_id = created["memories"][0]["id"]

    assert len(await backend.search("可检索")) == 1
    assert len((await backend.sources(memory_id))["entries"]) == 1
    await backend._io("set_enabled", False, created["revision"])
    with pytest.raises(MemoryUnavailable, match="disabled"):
        await backend.search("可检索")
    with pytest.raises(MemoryUnavailable, match="disabled"):
        await backend.sources(memory_id)

    disabled = await backend._io("snapshot")
    enabled = await backend._io("set_enabled", True, disabled["revision"])
    await backend._io("clear", enabled["revision"])
    assert await backend.search("可检索") == []
    with pytest.raises(ValueError, match="not found"):
        await backend.sources(memory_id)
    await backend.close()


@pytest.mark.parametrize("replacement", ["service", "workspace", "volume", "identity"])
@pytest.mark.asyncio
async def test_admission_rejects_changed_exact_runtime_binding(tmp_path, replacement):
    backend, _journal, _conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    runtime = backend.router._group_runtimes["group:testhash"]
    if replacement == "service":
        backend.router.services["group:testhash"] = SimpleNamespace(_disabled_identities=set())
    elif replacement == "workspace":
        runtime["workspace"] = tmp_path / "other-workspace"
    elif replacement == "volume":
        runtime["volume_module"] = object()
    else:
        runtime["group_id"] = "99999"

    with pytest.raises(MemoryUnavailable):
        await backend.search("anything")
    await backend.close()


@pytest.mark.asyncio
async def test_real_daily_runner_counts_members_but_archives_bot_and_shares_backend_guard(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    old = append(journal, conversation, 1, "100", "启用时的高水位")
    await backend.initialize()
    identity = (conversation.platform_id, conversation.bot_id, conversation.key)
    tasks = TaskStore(tmp_path / "task-state.sqlite3")
    tasks.enroll_initial([identity])
    prompts = []

    def archive_batch(**kwargs):
        payload = json.loads(kwargs["prompt"])
        prompts.append(payload)
        source_ids = [message["journal_id"] for message in payload["messages"]]
        existing = payload["existing_memories"]
        known_id = existing[0]["id"] if existing else None
        return response([change(source_ids, content="群聊连续经历", memory_id=known_id)])

    service.dsh.responses.extend([archive_batch, archive_batch])

    async def resolve(candidate):
        if candidate != identity:
            return None
        return GroupBinding(conversation, journal, backend, lambda: True)

    runner = DailyMemoryRunner(tasks, resolve)
    member_rows = [append(journal, conversation, 100 + i, str(100 + i), f"成员消息 {i}")
                   for i in range(98)]
    bot_sent = [append(journal, conversation, 300 + i, conversation.bot_id,
                       f"Bot已发送内容 {i}", direction="outbound") for i in range(2)]
    bot_self_inbound = append(journal, conversation, 400, conversation.bot_id,
                              "Bot身份的入站事件也不计数")

    await runner.run_day("2026-10-26")
    first_attempt = tasks.snapshot()["attempts"][0]
    assert first_attempt["status"] == "below_threshold"
    assert first_attempt["inbound_count"] == 98
    assert first_attempt["after_id"] == old.journal_id
    assert first_attempt["through_id"] == bot_self_inbound.journal_id
    assert (await backend.snapshot())["cursor"] == old.journal_id
    assert not service.dsh.calls

    # A same-day retry is suppressed even after more messages arrive.
    appended = append(journal, conversation, 401, "200", "第99条群友消息")
    await runner.run_day("2026-10-26")
    assert not service.dsh.calls
    assert (await backend.snapshot())["cursor"] == old.journal_id

    await runner.run_day("2026-10-27")
    assert len(service.dsh.calls) == 2
    assert [len(payload["messages"]) for payload in prompts] == [100, 2]
    all_prompt_rows = [message for payload in prompts for message in payload["messages"]]
    assert {row["journal_id"] for row in all_prompt_rows} == {
        *(row.journal_id for row in member_rows),
        *(row.journal_id for row in bot_sent),
        bot_self_inbound.journal_id,
        appended.journal_id,
    }
    assert any(row["actor_id"] == conversation.bot_id and row["direction"] == "outbound"
               for row in all_prompt_rows)
    assert any(row["actor_id"] == conversation.bot_id and row["direction"] == "inbound"
               for row in all_prompt_rows)

    saved = await backend.snapshot()
    assert saved["cursor"] == appended.journal_id
    assert saved["memories"][0]["source_ids"] == sorted(row["journal_id"] for row in all_prompt_rows)
    assert {row.journal_id for row in bot_sent}.issubset(saved["memories"][0]["source_ids"])
    sources = await backend.sources(saved["memories"][0]["id"], limit=20)
    assert sources["total_sources"] == 102
    assert any(entry["journal_id"] == bot_self_inbound.journal_id
               for entry in sources["entries"])

    attempts = tasks.snapshot()["attempts"]
    assert attempts[0]["local_day"] == "2026-10-27"
    assert attempts[0]["status"] == "completed"
    assert attempts[0]["inbound_count"] == 99
    assert attempts[0]["after_id"] == old.journal_id
    assert attempts[0]["through_id"] == appended.journal_id
    await runner.run_day("2026-10-27")
    assert len(service.dsh.calls) == 2
    await backend.close()
    tasks.close()


@pytest.mark.asyncio
async def test_capacity_lookup_cancellation_drains_worker_without_calling_model(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "cancel metadata lookup")
    original = service.dsh.memory_capacity
    started, stopped = threading.Event(), threading.Event()

    def lookup(**kwargs):
        started.set()
        assert kwargs['cancel_event'].wait(3)
        stopped.set()
        return original(**kwargs)

    service.dsh.memory_capacity = lookup
    task = asyncio.create_task(backend.prepare_reset(
        conversation=conversation, through_journal_id=row.journal_id))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert not service.dsh.calls
    assert (await backend.snapshot())["cursor"] == 0
    assert backend.last_run["status"] == "cancelled"
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_source", [0, -1, 999999, True, 1.0, "1"])
async def test_request_numeric_references_reject_non_integer_or_invisible_number(tmp_path, invalid_source):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "本群原文")
    service.dsh.responses.append({"text": json.dumps({"changes": [{
        "id": None, "content": "拒绝错误编号", "members": ["100"],
        "claims": [{"text": "认识", "sources": [invalid_source]}],
        "weight": 3, "status": "active"}]}, ensure_ascii=False)})
    try:
        with pytest.raises(ValueError, match="unknown evidence ID"):
            await backend.consolidate(conversation, after_journal_id=0, through_journal_id=row.journal_id)
        assert (await store_snapshot(backend))["cursor"] == 0
        assert (await store_snapshot(backend))["memories"] == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_file_name_citation_uses_request_number_and_retains_exact_host_binding(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    row = journal.append(JOURNAL.Message(
        platform_id=conversation.platform_id, bot_id=conversation.bot_id,
        conversation_key=conversation.key, conversation_type="group", direction="inbound",
        actor_id="100", actor_name="A", source="user", message_id="file-number",
        reply_to=None, content={"text": "文档在此", "segments": [
            {"type": "file", "name": "plan.pdf", "file": "opaque-file"}]},
        created_at="2026-01-01T00:00:00Z", observed_at_ns=time.time_ns()))[0]
    def response(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        ref = next(e for e in prompt["messages"][0]["evidence"] if e["kind"]=="file_name")
        assert type(ref["evidence_id"]) is int
        assert "ev-" not in kwargs["prompt"]
        return {"text":json.dumps({"changes":[{
            "id":None,"content":"A分享了文档","members":["100"],
            "claims":[{"text":"A分享plan.pdf","sources":[ref["evidence_id"]]}],
            "weight":3,"status":"active"}]},ensure_ascii=False)}
    service.dsh.responses.append(response)
    try:
        assert await backend.consolidate(conversation, after_journal_id=0, through_journal_id=row.journal_id)
        source = (await store_snapshot(backend))["memories"][0]["claims"][0]["sources"][0]
        assert source == {"journal_id":row.journal_id,"quote":"plan.pdf","actor_id":"100","direction":"inbound"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_concurrent_groups_share_numeric_reference_one_without_cross_binding(tmp_path):
    first = tmp_path / "first"; second = tmp_path / "second"
    first.mkdir(mode=0o700); second.mkdir(mode=0o700)
    a, journal, ca, _wa, sa = make_backend(first, group_id="12345")
    b, _unused, cb, _wb, sb = make_backend(second, group_id="67890")
    b.journal = journal
    await a.initialize(); await b.initialize()
    ra = append(journal, ca, 1, "100", "甲群精确原文")
    rb = append(journal, cb, 2, "200", "乙群不同原文")
    def response(**kwargs):
        prompt = json.loads(kwargs["prompt"]); row = prompt["messages"][0]
        assert row["evidence"][0]["evidence_id"] == 1
        return {"text":json.dumps({"changes":[{
            "id":None,"content":"当前群理解","members":[row["actor_id"]],
            "claims":[{"text":"当前来源","sources":[1]}],"weight":3,"status":"active"}]},ensure_ascii=False)}
    sa.dsh.responses.append(response); sb.dsh.responses.append(response)
    try:
        assert all(await asyncio.gather(
            a.consolidate(ca,after_journal_id=0,through_journal_id=ra.journal_id),
            b.consolidate(cb,after_journal_id=0,through_journal_id=rb.journal_id)))
        am=(await store_snapshot(a))["memories"][0]["claims"][0]["sources"][0]
        bm=(await store_snapshot(b))["memories"][0]["claims"][0]["sources"][0]
        assert (am["journal_id"],am["quote"],am["actor_id"]) == (ra.journal_id,"甲群精确原文","100")
        assert (bm["journal_id"],bm["quote"],bm["actor_id"]) == (rb.journal_id,"乙群不同原文","200")
    finally:
        await a.close(); await b.close()


@pytest.mark.asyncio
async def test_revising_201_visible_claim_sources_preserves_every_source(tmp_path):
    backend, journal, conversation, _workspace, service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, i, "100", f"长期共同经历{i}") for i in range(1,202)]
    ids = [row.journal_id for row in rows]
    prior = await backend._io("commit", 0, 0, ids[-1], [{
        "id": None, "content": "长期共同经历", "members": ["100"], "source_ids": ids,
        "claims": [{"text": "共同经历的连续记录", "sources": [{
            "journal_id": row.journal_id, "quote": row.message.content["text"],
            "actor_id": "100", "direction": "inbound"} for row in rows]}],
        "weight": 3, "status": "active"}], {})
    memory_id = prior["memories"][0]["id"]
    new = append(journal, conversation, 202, "100", "长期共同经历的新补充")

    def retain_all(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        old = next(item for item in prompt["existing_memories"] if item["id"] == memory_id)
        assert len(old["claims"][0]["sources"]) == 201
        refs = [*old["claims"][0]["sources"], prompt["messages"][0]["evidence"][0]["evidence_id"]]
        return {"text": json.dumps({"changes": [{"id": memory_id,
            "content": "长期共同经历与新补充", "members": ["100"],
            "claims": [{"text": "完整共同经历", "sources": refs}],
            "weight": 3, "status": "active"}]}, ensure_ascii=False)}

    service.dsh.responses.append(retain_all)
    try:
        assert await backend.consolidate(conversation, after_journal_id=ids[-1],
                                         through_journal_id=new.journal_id)
        saved = (await store_snapshot(backend))["memories"][0]
        assert saved["source_ids"] == [*ids,new.journal_id]
        assert len(saved["claims"][0]["sources"]) == 202
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_owner_edit_preserves_202_sources_and_marks_claims_for_review(tmp_path):
    backend, _journal, _conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    sources = [{"journal_id": i, "quote": "来源", "actor_id": "100", "direction": "inbound"}
               for i in range(1,203)]
    saved = await backend._io("commit", 0, 0, 202, [{"id": None, "content": "原理解",
        "members": ["100"], "source_ids": list(range(1,203)),
        "claims": [{"text": "原认识", "sources": sources}], "weight": 3, "status": "active"}], {})
    before = saved["memories"][0]
    try:
        await backend.manage("edit", {"id": before["id"], "expected_revision": 1,
            "content": "Owner修订理解", "members": ["100"], "weight": 4, "status": "archived"})
        result = await store_snapshot(backend)
        after = result["memories"][0]
        assert result["cursor"] == 202 and result["revision"] == 2
        assert after["content"] == "Owner修订理解" and after["status"] == "archived"
        assert after["source_ids"] == before["source_ids"]
        assert after["claims"] == before["claims"]
        assert after["claims_review_required"] and after["evidence_status"] == "review_required"
    finally:
        await backend.close()


def latest_diagnostic(workspace):
    with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
        row = db.execute(
            "SELECT diagnostic_json FROM validation_diagnostics ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0]) if row else None


@pytest.mark.asyncio
async def test_prebatch_runtime_error_is_persisted_without_exception_text(tmp_path):
    backend, _journal, conversation, workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    error = RuntimeError("SECRET provider payload")

    async def fail_readable():
        raise error

    backend._readable = fail_readable
    try:
        with pytest.raises(RuntimeError) as caught:
            await backend.consolidate(conversation, after_journal_id=7, through_journal_id=19)
        assert caught.value is error
        diagnostic = latest_diagnostic(workspace)
        assert diagnostic["operation"] == "snapshot"
        assert diagnostic["after_id"] == diagnostic["committed_cursor"] == 7
        assert diagnostic["through_id"] == diagnostic["target_through_id"] == 19
        assert diagnostic["exception_type"] == "builtins.RuntimeError"
        assert diagnostic["backend_sha256"]
        serialized = json.dumps(diagnostic)
        assert "SECRET" not in serialized and "provider payload" not in serialized
        assert "/home/" not in serialized and "locals" not in serialized
        assert backend.last_run["error_code"] == "MEMORY_CONSOLIDATION_FAILED"
        state = await backend.incremental_state()
        assert state["failed_through"] == 19
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_external_value_errors_and_json_error_never_persist_raw_message(tmp_path):
    for failing_operation in ("route", "capacity", "provider", "provider_json"):
        (tmp_path / failing_operation).mkdir(mode=0o700)
        backend, journal, conversation, workspace, service = make_backend(
            tmp_path / failing_operation)
        await backend.initialize()
        row = append(journal, conversation, 1, "100", "消息")
        secret_message = "SECRET api-key=abc123 https://private.example/path"
        error = (json.JSONDecodeError(secret_message, "raw " + secret_message, 4)
                 if failing_operation == "provider_json" else ValueError(secret_message))
        error.error_code = "UPSTREAM_FAILURE"
        if failing_operation == "route":
            service.state.get_model_route = lambda *_args: (_ for _ in ()).throw(error)
        else:
            if failing_operation == "capacity":
                service.dsh.memory_capacity = lambda **_kwargs: (_ for _ in ()).throw(error)
            else:
                service.dsh.responses.append(lambda **_kwargs: (_ for _ in ()).throw(error))
        try:
            with pytest.raises(type(error)) as caught:
                await backend.consolidate(conversation, after_journal_id=0,
                                          through_journal_id=row.journal_id)
            diagnostic = latest_diagnostic(workspace)
            assert caught.value is error
            assert diagnostic["operation"] == (
                "model" if failing_operation.startswith("provider") else failing_operation)
            serialized = json.dumps(diagnostic)
            assert "SECRET" not in serialized and "api-key" not in serialized
            assert "private.example" not in serialized and "raw " not in serialized
            assert "/home/abasun/" not in serialized and "/tmp/" not in serialized
            assert "validation_error" not in diagnostic
            assert backend.last_run["error_code"] == "UPSTREAM_FAILURE"
            assert (await backend.snapshot())["cursor"] == 0
            assert (await backend.incremental_state())["failed_through"] == row.journal_id
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_external_value_error_safe_fallback_log_when_diagnostic_write_fails(tmp_path):
    backend, _journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    error = ValueError("SECRET api-key=abc123 https://private.example/path")
    error.error_code = "UPSTREAM_FAILURE"
    service.dsh.responses.append(lambda **_kwargs: (_ for _ in ()).throw(error))
    logs = []
    backend.logger.warning = lambda *args, **kwargs: logs.append((args, kwargs))
    original_sync = backend._sync

    def fail_diagnostic(action, *args, **kwargs):
        if action == "record_validation_diagnostic":
            raise OSError(28, "SECRET diagnostic volume")
        return original_sync(action, *args, **kwargs)

    backend._sync = fail_diagnostic
    row = append(_journal, conversation, 1, "100", "消息")
    try:
        with pytest.raises(ValueError) as caught:
            await backend.consolidate(conversation, after_journal_id=0,
                                      through_journal_id=row.journal_id)
        assert caught.value is error
        log_text = json.dumps(logs)
        assert "SECRET" not in log_text and "api-key" not in log_text
        assert "private.example" not in log_text and "ValueError" in log_text
        assert "/home/abasun/" not in log_text and "/tmp/" not in log_text
        assert backend.last_run["error_code"] == "UPSTREAM_FAILURE"
        assert (await backend.snapshot())["cursor"] == 0
        assert (await backend.incremental_state())["failed_through"] == row.journal_id
        with sqlite3.connect(workspace / ".v2-memory/state.sqlite3") as db:
            assert db.execute("SELECT name FROM sqlite_master WHERE name = 'validation_diagnostics'").fetchone() is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_second_batch_prepare_failure_reports_last_committed_cursor(tmp_path):
    backend, journal, conversation, workspace, service = make_backend(tmp_path)
    await backend.initialize()
    rows = [append(journal, conversation, i + 1, "100", f"消息{i}") for i in range(101)]
    service.dsh.responses.append(response([]))
    original_route = backend._route
    calls = 0

    def route_then_fail():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("SECRET next batch route")
        return original_route()

    backend._route = route_then_fail
    try:
        with pytest.raises(RuntimeError):
            await backend.consolidate(conversation, after_journal_id=0,
                                      through_journal_id=rows[-1].journal_id)
        diagnostic = latest_diagnostic(workspace)
        assert diagnostic["operation"] == "route"
        assert diagnostic["after_id"] == rows[99].journal_id
        assert diagnostic["committed_cursor"] == rows[99].journal_id
        assert diagnostic["through_id"] == rows[-1].journal_id
        assert diagnostic["batch_index"] == 2
        assert "SECRET" not in json.dumps(diagnostic)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_oserror_and_sqlite_metadata_are_bounded_and_cause_cycles_stop(tmp_path):
    backend, _journal, conversation, workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    error = OSError(5, "SECRET file")
    cause = RuntimeError("SECRET cause")
    error.__cause__ = cause
    cause.__cause__ = error
    error.sqlite_errorcode = 5
    error.sqlite_errorname = "SQLITE_BUSY"

    async def fail_readable():
        raise error

    backend._readable = fail_readable
    try:
        with pytest.raises(OSError):
            await backend.consolidate(conversation, after_journal_id=0, through_journal_id=1)
        diagnostic = latest_diagnostic(workspace)
        assert diagnostic["errno"] == 5
        assert diagnostic["sqlite_errorcode"] == 5
        assert diagnostic["sqlite_errorname"] == "SQLITE_BUSY"
        assert len(diagnostic["exception_chain"]) <= 4
        assert "SECRET" not in json.dumps(diagnostic)
        assert len(diagnostic["frames"]) <= 10
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_diagnostic_store_failure_logs_safe_summary_and_preserves_original_failure(tmp_path):
    backend, _journal, conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    error = RuntimeError("SECRET original")
    logs = []
    backend.logger.warning = lambda *args, **kwargs: logs.append((args, kwargs))
    original_sync = backend._sync

    def fail_diagnostic(action, *args, **kwargs):
        if action == "record_validation_diagnostic":
            raise OSError(28, "SECRET diagnostic volume")
        return original_sync(action, *args, **kwargs)

    backend._sync = fail_diagnostic

    async def fail_readable():
        raise error

    backend._readable = fail_readable
    try:
        with pytest.raises(RuntimeError) as caught:
            await backend.consolidate(conversation, after_journal_id=0, through_journal_id=9)
        assert caught.value is error
        log_text = json.dumps(logs)
        assert "SECRET" not in log_text and "original" not in log_text
        assert "operation" in log_text and "builtins.RuntimeError" in log_text
        assert (await backend.incremental_state())["failed_through"] == 9
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_legacy_diagnostic_identifies_first_bad_source_after_preflight(tmp_path):
    backend, journal, conversation, _workspace, _service = make_backend(tmp_path)
    await backend.initialize()
    row = append(journal, conversation, 1, "100", "实际原文")
    try:
        with pytest.raises(ValueError, match="does not match Journal") as caught:
            await backend._bind_claims([{"text": "待核对", "sources": [
                {"journal_id": row.journal_id, "quote": "错误引句"},
                {"journal_id": row.journal_id, "quote": "实际原文"}]}],
                conversation, {row.journal_id}, change_index=2)
        assert caught.value.error_path == "$.changes[2].claims[0].sources[0]"
        assert caught.value.diagnostic["source_index"] == 0
    finally:
        await backend.close()
