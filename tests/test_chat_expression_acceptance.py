"""Focused contracts for the live conversational-expression evaluator."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-chat-expression.py"
spec = importlib.util.spec_from_file_location("chat_expression_acceptance", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(evaluation)


def test_scenarios_cover_ten_turns_and_all_required_dialogue_shapes():
    guides, scenarios = evaluation.load_inputs()
    assert set(guides) >= {"principles", "examples"}
    ids = {scenario["id"] for scenario in scenarios["scenarios"]}
    assert ids == {"familiar-game-chat", "product-opinion-discussion",
                   "tool-results-and-file-work", "reset-history-explanation"}
    assert sum(len(row["turns"]) for row in scenarios["scenarios"]) == 10
    reset = next(row for row in scenarios["scenarios"] if row["id"] == "reset-history-explanation")
    assert [turn["user"] for turn in reset["turns"]] == ["刚才聊到哪了？", "你根据什么知道重置了？你能确认记录被删除了吗？"]
    assert all("setup" not in row for row in scenarios["scenarios"]), \
        "setup notes are evaluator metadata and must not be injected as model context"


def test_transfer_fixture_keeps_all_four_intents_in_one_session():
    _, scenarios = evaluation.load_inputs(scenarios_path=ROOT / "tests/fixtures/chat-expression-transfer.json")
    assert len(scenarios["scenarios"]) == 1
    scenario = scenarios["scenarios"][0]
    assert scenario["conversation"] == "private:90009"
    assert len(scenario["turns"]) == 4
    assert scenario["turns"][2]["tool"] == "file"
    assert "class-options.txt" in scenario["workspace_files"]


def test_native_header_requires_exact_candidate_tool_and_deployed_model(tmp_path):
    guides, _ = evaluation.load_inputs()
    candidate = guides["examples"]
    log = tmp_path / "repo/.runtime/dsh-home-owner/sessions/workdir/sid/session.v4.jsonl"
    log.parent.mkdir(parents=True)
    header = {"config": {"provider": "deepseek-official", "model": "deepseek-flash"},
              "tools": [{"name": "mcp__astrbot__qq_send_origin",
                         "description": candidate["send_tool_description"]}]}
    def write():
        log.write_text(json.dumps({"type": "request/header", "data": {"header": header}}) + "\n")
    write()
    assert evaluation.native_header_check(tmp_path, "sid", candidate)["send_description_matches"]
    header["tools"][0]["description"] = "stale description"
    write()
    with pytest.raises(AssertionError, match="differs"):
        evaluation.native_header_check(tmp_path, "sid", candidate)
    header["tools"][0]["description"] = candidate["send_tool_description"]
    header["config"]["model"] = "different-model"
    write()
    with pytest.raises(AssertionError, match="differs"):
        evaluation.native_header_check(tmp_path, "sid", candidate)


def test_receipt_join_requires_one_to_one_transport_and_canonical_evidence():
    outbound = [{"message_id": "11"}, {"message_id": "12"}]
    receipts = [{"message_id": "11"}, {"message_id": "12"}]
    journal = [{"message_id": "11", "direction": "outbound", "conversation_key": "group:91000"},
               {"message_id": "12", "direction": "outbound", "conversation_key": "group:91000"},
               {"message_id": "13", "direction": "outbound", "conversation_key": "private:90009"}]
    joined = evaluation.verify_receipt_join(outbound, receipts, journal, "group:91000")
    assert joined["same_ids_in_transport_and_receipts"]
    assert joined["all_outbound_in_journal"]
    assert joined["unique_receipt_ids"]
    assert joined["outbound_count"] == 2

    missing = evaluation.verify_receipt_join(outbound, receipts[:1], journal, "group:91000")
    assert not missing["same_ids_in_transport_and_receipts"]
    missing_journal = evaluation.verify_receipt_join(outbound, receipts,
        [journal[0]], "group:91000")
    assert not missing_journal["all_outbound_in_journal"]


def test_prompt_loading_records_exact_native_prompt_and_variant_indicators():
    guides, _ = evaluation.load_inputs()
    candidate = guides["examples"]
    messages = [{"role": "system", "content": candidate["decision_guide"] + "\n" + candidate["profile_reply"]},
                {"role": "user", "content": "测试输入"}]
    raw = json.dumps(messages, ensure_ascii=False)
    event = {"event": "model/start", "input": raw, "input_bytes": len(raw.encode()),
             "input_sha256": evaluation.digest(raw), "tools": ["mcp__astrbot__qq_send_origin"]}
    result = evaluation.prompt_loading(candidate, [event])
    assert result["all_guide_lines_observed"]
    assert result["all_profile_reply_lines_observed"]
    assert result["tool_availability_observed"]
    assert result["input_sha256"] == [evaluation.digest(raw)]
    assert result["input_bytes"] == [len(raw.encode())]


def test_workspace_artifact_checks_exact_file_bytes_and_required_content():
    before = {"purchase-summary.txt": {"sha256": "old"}}
    content = "推荐标准版，等待折扣；豪华版没有足够的追加内容支持差价。"
    artifact = {"text": content, "bytes": len(content.encode()), "sha256": evaluation.digest(content)}
    passed = evaluation.verify_file_artifact(before, {"purchase-summary.txt": artifact},
                                             "purchase-summary.txt", ["标准版", "豪华版"])
    assert passed["exists_and_nonempty"] and passed["changed_during_turn"]
    assert passed["all_required_terms_present"]
    failed = evaluation.verify_file_artifact({}, {}, "purchase-summary.txt", ["标准版"])
    assert not failed["exists_and_nonempty"]


def test_tool_description_replacement_preserves_a_valid_staged_module(tmp_path):
    module = tmp_path / "mcp_server.py"
    module.write_text(
        "async def qq_send_origin(text):\n"
        f"    {chr(34) * 3}old description{chr(34) * 3}\n"
        "    return text\n", encoding="utf-8")
    candidate = "First complete message.\nSend preceding messages through this tool."
    evaluation._replace_tool_description(module, candidate)
    tree = compile(module.read_text(encoding="utf-8"), str(module), "exec")
    namespace = {}
    exec(tree, namespace)
    assert namespace["qq_send_origin"].__doc__ == candidate


def test_candidate_staging_is_private_and_does_not_change_shared_sources(tmp_path):
    guides, _ = evaluation.load_inputs()
    candidate = guides["principles"]
    shared_guide = ROOT / "dsh/plugins/agent-profile/decision-guide.md"
    shared_hash = evaluation.digest(shared_guide.read_bytes())
    staged_repo = tmp_path / "repo"
    (staged_repo / "dsh").mkdir(parents=True)
    (staged_repo / "dsh/plugins").symlink_to(ROOT / "dsh/plugins", target_is_directory=True)
    shutil.copytree(ROOT / "dsh/profile", staged_repo / "dsh/profile")
    (staged_repo / "dsh/node_modules").symlink_to(
        ROOT / ".runtime/dsh-runtime/node_modules", target_is_directory=True)
    (staged_repo / "astrbot-plugins/v2_dsh_router").mkdir(parents=True)
    for name in ("mcp_server.py", "service.py"):
        shutil.copy2(ROOT / "astrbot-plugins/v2_dsh_router" / name,
                     staged_repo / "astrbot-plugins/v2_dsh_router" / name)
    (staged_repo / "sandbox").mkdir()
    shutil.copy2(ROOT / "sandbox/probe.mjs", staged_repo / "sandbox/probe.mjs")
    shared_probe_hash = evaluation.digest((ROOT / "sandbox/probe.mjs").read_bytes())
    details = {"staged_repo": str(staged_repo)}

    staged = evaluation.stage_candidate(details, candidate)

    assert not (staged_repo / "dsh/plugins").is_symlink()
    assert (staged_repo / "dsh/plugins/agent-profile/decision-guide.md").read_text(encoding="utf-8") == \
        candidate["decision_guide"].rstrip() + "\n"
    assert shared_guide.is_file() and evaluation.digest(shared_guide.read_bytes()) == shared_hash
    for profile in ("agent-owner.md", "agent-group.md", "agent-group-sandbox.md"):
        body = (staged_repo / "dsh/profile" / profile).read_text(encoding="utf-8")
        assert candidate["profile_reply"] in body
    compile((staged_repo / "astrbot-plugins/v2_dsh_router/mcp_server.py").read_text(encoding="utf-8"),
            "staged-mcp", "exec")
    assert staged["decision_guide"] == evaluation.digest(candidate["decision_guide"].rstrip() + "\n")
    assert staged["owner_volume_module_present"]
    assert staged["bounded_system_projection"]
    assert "input_projection: 'system-only'" in (staged_repo / "sandbox/probe.mjs").read_text()
    assert evaluation.digest((ROOT / "sandbox/probe.mjs").read_bytes()) == shared_probe_hash
    assert staged["node_modules_target"] == str((ROOT / ".runtime/dsh-runtime/node_modules").resolve())


def test_blinded_packet_stays_unrated_and_skips_missing_variant_pairs():
    _, scenarios = evaluation.load_inputs()
    scenario = scenarios["scenarios"][0]
    rows = []
    for variant in ("principles", "examples"):
        for index, turn in enumerate(scenario["turns"], start=1):
            rows.append({"variant": variant, "repeat": 0, "scenario": scenario["id"],
                         "turn_index": index, "outbound_messages": [],
                         "human_checks": turn["human_checks"], "tool_call_observed": False})
    packet, answer_key = evaluation.build_blinded_pairs(rows, {0: ["principles", "examples"]}, scenarios)
    assert packet["human_evaluation"] == "unrated"
    assert len(packet["pairs"]) == 1
    assert all(all(value is None for value in turn["ratings"].values())
               and turn["preference"] is None for turn in packet["pairs"][0]["turns"])
    assert set(answer_key[f"{scenario['id']}:r0"].values()) == {"principles", "examples"}

    incomplete, _ = evaluation.build_blinded_pairs(rows[:-1], {0: ["principles", "examples"]}, scenarios)
    assert not incomplete["pairs"]
    assert incomplete["incomplete_pairs"][0]["missing_variants"]
