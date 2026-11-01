import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reply_study_preparation", ROOT / "scripts/prepare-reply-relevance-study.py")
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)

runner_spec = importlib.util.spec_from_file_location("reply_study_runner", ROOT / "scripts/run-reply-relevance-study.py")
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)

analysis_spec = importlib.util.spec_from_file_location("reply_study_analysis", ROOT / "scripts/analyze-reply-relevance-study.py")
analysis = importlib.util.module_from_spec(analysis_spec)
analysis_spec.loader.exec_module(analysis)


def without(cell, *keys):
    return {k: v for k, v in cell.items() if k not in keys}


def test_p0_pairs_change_only_guide_arm_not_task_permission_or_initial_facts():
    cells = study.make_cells()
    assert len(cells) == 27
    for scenario in ("simple", "details", "permission"):
        for repeat in range(3):
            pair = [c for c in cells if c["study"] == "P0" and c["scenario"] == scenario and c["repeat"] == repeat]
            assert {c["arm"] for c in pair} == {"A", "B"}
            assert without(pair[0], "id", "arm") == without(pair[1], "id", "arm")
            assert pair[0]["initial_record"] is None
            assert pair[0]["deny_writes"] == (scenario == "permission")
            assert all(r["direction"] == "outbound" for r in pair[0]["journal"])


def test_p1_changes_only_old_journal_segment_with_same_wake_and_completed_file():
    cells = [c for c in study.make_cells() if c["study"] == "P1"]
    reference = cells[0]
    for cell in cells:
        assert without(cell, "id", "scenario", "journal", "repeat") == without(reference, "id", "scenario", "journal", "repeat")
        assert cell["journal"][0] == reference["journal"][0]
        assert cell["initial_record"] == study.RECORDED
        for row in cell["journal"][1:]:
            assert row["direction"] == "inbound"
            assert all(s["type"] == "text" for s in json.loads(row["content_json"])["segments"])
            assert row["reply_to"] is None
    assert {c["scenario"] for c in cells} == {"none", "unrelated", "related"}
    unrel = next(c for c in cells if c["scenario"] == "unrelated")
    related = next(c for c in cells if c["scenario"] == "related")
    assert unrel["journal"][1:3] == related["journal"][1:3]
    assert unrel["journal"][-1]["actor_id"] == "90002"
    assert related["journal"][-1]["actor_id"] == "90009"


def test_protocol_predeclares_all_repeats_and_blind_key_without_quality_cutoff():
    protocol = study.protocol()
    assert protocol == study.protocol()
    assert len(protocol["order"]) == len(set(protocol["order"])) == 27
    assert set(protocol["order"]) == set(protocol["blind_key"].values())
    assert len(set(protocol["blind_key"])) == 27
    assert all(c.startswith("P0-") for c in protocol["order"][:18])
    assert all(c.startswith("P1-") for c in protocol["order"][18:])
    assert "False completed" in protocol["criteria"]["severe_failure"]
    assert len(protocol["criteria"]["axes"]) == 6
    assert all("seq" not in c and "checkpoint_path" not in c for c in protocol["cells"])


def test_state_inventory_preserves_new_records_without_following_links(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    text = workspace / "new-record.md"
    text.write_text("违规已记入另一份手工记录🙂", encoding="utf-8")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret").write_text("DO_NOT_COPY", encoding="utf-8")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    (workspace / "binary").write_bytes(b"\x00\xff")
    (workspace / "large").write_text("a" * 40)
    result = runner.snapshot_tree(workspace, max_text_bytes=39)
    assert result["new-record.md"]["sha256"] == hashlib.sha256(text.read_bytes()).hexdigest()
    assert result["new-record.md"]["text"] is None  # byte-bound UTF-8, not a character budget
    assert result["link"]["kind"] == "symlink" and "link/secret" not in result
    assert "DO_NOT_COPY" not in json.dumps(result)
    assert result["binary"]["text_omitted"] == "binary"
    assert result["large"]["text_omitted"] == "per_file_limit"
    assert runner.snapshot_tree(workspace)["new-record.md"]["text"] == text.read_text()


def test_frozen_inputs_fail_closed_on_drift_and_cell_ids_are_unique(tmp_path):
    path = tmp_path / "fixture"
    path.write_text("frozen")
    manifest = {"frozen_hashes": {str(path): runner.digest(path)}}
    runner.assert_frozen(manifest)
    path.write_text("changed")
    with pytest.raises(RuntimeError, match="hash changed"):
        runner.assert_frozen(manifest)
    cells = study.make_cells()
    assert runner.choose_cell({"cells": cells}, "P0-simple-A-r0")["repeat"] == 0
    with pytest.raises(ValueError, match="exactly one"):
        runner.choose_cell({"cells": cells + [cells[0]]}, cells[0]["id"])


def turn_stub(tmp_path):
    obj = runner.Cell.__new__(runner.Cell)
    obj.directory = tmp_path
    obj.group_workspace = tmp_path / "workspace"
    obj.group_workspace.mkdir()
    obj.mode = "live"
    obj.manifest_provider, obj.manifest_model = "wb", "deepseek-v4.1-flash"
    identity = dict(conversation_key="group:91000", platform_id="v2-sandbox", bot_id="99000", inbound_message_id="1")
    trace = [dict(identity, event="turn/completed"), dict(identity, event="send/start", attempt_id="a"),
             dict(identity, event="send/returned", attempt_id="a"),
             dict(identity, event="send/receipt", attempt_id="a", message_id="2")]
    obj.evidence = lambda: {"audit": trace}
    obj.wait = lambda fn, *args, **kwargs: fn()
    obj.events = lambda: [dict(conversation={"type": "group", "id": "91000"}, message_id="2", direction="outbound")]
    obj.rows = lambda: [dict(identity, direction="inbound", message_id="1"), dict(identity, direction="outbound", message_id="2")]
    obj.send = lambda *args, **kwargs: {"message_id": "1"}
    obj.snapshot_files = lambda _: {"memory/preferences.md": {"text": "UNCHANGED"}, "AGENTS.md": {"text": "GUIDE"}}
    cell = dict(id="test", record_file="memory/preferences.md", request="记下来", owner="90009", deny_writes=False)
    return obj, cell


def test_delivery_alone_cannot_certify_controlled_input_when_no_snapshot(tmp_path):
    obj, cell = turn_stub(tmp_path)
    result = obj.run_turn(cell)
    assert result["protocol_status"] == "incomplete"
    assert not result["source_verification"]["all_routes_match"]
    assert not result["source_verification"]["guide_verified_in_native_snapshot"]


def test_guide_only_in_assembly_is_not_actual_model_input(tmp_path):
    obj, cell = turn_stub(tmp_path)
    snaps = tmp_path / "request-snapshots"
    snaps.mkdir()
    (snaps / "request.json").write_text(json.dumps({
        "metadata": {"studyCell": "test", "provider": "wb", "model": "deepseek-v4.1-flash"},
        "assembly": {"sections": [{"text": "GUIDE"}]}, "messages": [{"role": "user", "content": []}],
    }))
    result = obj.run_turn(cell)
    assert result["source_verification"]["all_routes_match"]
    assert not result["source_verification"]["guide_verified_in_native_snapshot"]
    assert result["protocol_status"] == "incomplete"


def test_timeout_keeps_files_and_delivery_evidence_without_retry(tmp_path):
    obj, cell = turn_stub(tmp_path)
    def timeout(*args, **kwargs):
        raise TimeoutError("synthetic deadline")
    obj.wait = timeout
    result = obj.run_turn(cell)
    assert result["terminal"]["event"] == "timeout"
    assert result["before_files"] == result["after_files"]
    assert result["bubbles"] and result["send_start_receipt_journal"]
    assert result["protocol_status"] == "incomplete"
    assert (tmp_path / "failure.json").is_file()


def test_preflight_uses_actual_bash_schema_and_existing_owner_is_not_created_twice():
    obj = runner.Cell.__new__(runner.Cell)
    obj.init_journal = lambda _: None
    calls = []
    def request(method, path, data=None):
        calls.append((method, path, data))
        if path == "/api/state":
            return {"members": [{"id": "90009"}]}
        if method == "GET" and path == "/api/scenario":
            return {"revision": 1}
        return {}
    obj.request = request
    obj.prepare_cell({}, {"journal": [], "owner": "90009", "preflight_command": "printf probe >> memory/preferences.md"})
    assert not any(method == "POST" and path == "/api/members" for method, path, _ in calls)
    plan = next(data for method, path, data in calls if path == "/api/scripts")
    assert plan["name"] == "bash" and plan["arguments"]["description"]
    assert plan["arguments"]["command"] == "printf probe >> memory/preferences.md"


def test_native_bash_nonzero_exit_is_visible_even_when_is_error_false(tmp_path):
    events = [
        {"type": "tool/call", "seq": 1, "data": {"name": "bash", "callId": "x"}},
        {"type": "tool/result", "seq": 2, "data": {"message": {
            "toolCallId": "x", "isError": False,
            "content": [{"type": "text", "text": "[stderr]\nRead-only file system\n[exit code: 1]"}]}}},
        {"type": "tool/call", "seq": 3, "data": {"name": "bash", "callId": "y"}},
        {"type": "tool/result", "seq": 4, "data": {"message": {
            "toolCallId": "y", "isError": False,
            "content": [{"type": "text", "text": "PRIVATE_GUIDE\n[exit code: 0]"}]}}},
    ]
    (tmp_path / "session.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    observed = analysis.native_tools(tmp_path, ["session.jsonl"])
    assert observed[0]["exit_code"] == 1 and observed[0]["error_text"]
    assert observed[1]["exit_code"] == 0 and observed[1]["error_text"] is None
    assert "PRIVATE_GUIDE" not in json.dumps(observed)


def test_analysis_checks_actual_journal_instead_of_assuming_seed_reached_model():
    cell = study.make_cells()[0]
    rows = [{"journal_id": r["journal_id"], "direction": r["direction"], "actor_id": r["actor_id"],
             "content": json.loads(r["content_json"]), "reply_to": r["reply_to"]} for r in cell["journal"]]
    rows.append({"journal_id": 1000, "direction": "inbound", "actor_id": cell["owner"],
                 "content": {"text": cell["request"]}, "reply_to": None})
    header = {"conversation": "group:91000", "latest_user_journal_id": 1000}
    def artifact():
        return {"messages": [{"role": "user", "content": [{"type": "text", "text":
                "\n".join(json.dumps(r) for r in [header, *rows])}]}]}
    assert analysis.initial_journal_projection(artifact(), cell)["matches_manifest"]
    rows[0]["content"]["text"] = "wrong old context"
    assert not analysis.initial_journal_projection(artifact(), cell)["matches_manifest"]
    assert not analysis.initial_journal_projection({"messages": []}, cell)["matches_manifest"]


def test_blind_packet_withholds_guide_copies_and_raw_error_paths_but_keeps_bubbles():
    cell = study.make_cells()[0]
    private = {"kind": "file", "text": "FULL_PRIVATE_GUIDE_VARIANT_A", "sha256": "HASH", "bytes": 29}
    result = {"before_files": {cell["record_file"]: private},
              "after_files": {cell["record_file"]: private, "AGENTS.md.bak": private},
              "changed_paths": ["AGENTS.md.bak"], "protocol_status": "complete", "terminal": {"event": "turn/completed"}}
    tools = [{"name": "bash", "is_error": False, "exit_code": 1,
              "error_text": "/private/P0-simple-A-r0/FULL_PRIVATE_GUIDE_VARIANT_A"}]
    packet = analysis.blind_packet("review-001", cell, result, tools, ["实际用户气泡保留"])
    raw = json.dumps(packet, ensure_ascii=False)
    assert "FULL_PRIVATE_GUIDE" not in raw and "P0-simple-A-r0" not in raw and "AGENTS.md.bak" not in raw
    assert packet["bubbles"] == ["实际用户气泡保留"]
    assert packet["tools"][0]["exit_code"] == 1
    assert packet["changed_files"]["file-001"]["after"]["sha256"] == "HASH"
