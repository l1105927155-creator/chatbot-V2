"""Console-backed group workspace write authorization."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "dsh/plugins/agent-profile/group-write-policy.mjs"
NODE = shutil.which("node")


def _node_policy(state_file: Path, source: dict[str, str], owners: list[dict[str, str]]) -> bool:
    script = (
        f"import {{canWriteGroupWorkspace}} from {json.dumps(POLICY.as_uri())};"
        "const source=JSON.parse(process.env.TEST_SOURCE);"
        "const owners=process.env.TEST_OWNERS;"
        "process.stdout.write(String(canWriteGroupWorkspace(source,{"
        "stateFile:process.env.V2_CONSOLE_STATE_FILE,ownersRaw:owners})));"
    )
    result = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", script],
        text=True, capture_output=True, check=True,
        env={**os.environ, "V2_CONSOLE_STATE_FILE": str(state_file),
             "TEST_SOURCE": json.dumps(source), "TEST_OWNERS": json.dumps(owners)},
    )
    return result.stdout == "true"


def _state(path: Path, editors: list[str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = {
        "revision": 3,
        "defaults": {"model": None, "enabled": True, "editors": []},
        "sessions": {
            json.dumps(["qq", "bot-1", "group:123"], separators=(",", ":")): {
                "identity": {"platform_id": "qq", "bot_id": "bot-1", "conversation_key": "group:123"},
                "settings": {"model": None, "enabled": True, "editors": editors},
            }
        },
        "templates": [],
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    path.chmod(0o600)


def test_group_editors_are_read_from_current_console_snapshot(tmp_path):
    state_file = tmp_path / "private" / "console.json"
    source = {"platform_id": "qq", "bot_id": "bot-1", "conversation_key": "group:123",
              "requester_id": "10001"}
    _state(state_file, ["10001"])
    assert _node_policy(state_file, source, []) is True
    _state(state_file, ["10002"])
    assert _node_policy(state_file, source, []) is False


def test_only_exact_deployed_owner_bypasses_group_editor_list(tmp_path):
    state_file = tmp_path / "private" / "console.json"
    source = {"platform_id": "qq", "bot_id": "bot-1", "conversation_key": "group:123",
              "requester_id": "90000"}
    _state(state_file, [])
    owners = [{"platform_id": "qq", "bot_id": "bot-1", "user_id": "90000"}]
    assert _node_policy(state_file, source, owners) is True
    owners[0]["bot_id"] = "other-bot"
    assert _node_policy(state_file, source, owners) is False


def test_background_child_rechecks_latest_policy_for_inherited_requester(tmp_path):
    state_file = tmp_path / "private" / "console.json"
    source = {"platform_id": "qq", "bot_id": "bot-1", "conversation_key": "group:123",
              "requester_id": "10001", "nonce": "turn-nonce"}
    _state(state_file, ["10001"])
    script = (
        f"import {{canWriteGroupWorkspace}} from {json.dumps(POLICY.as_uri())};"
        f"import {{bindParentTurn,createResearchAuthority,bindResearchChild,researchDelegation}} "
        f"from {json.dumps((ROOT / 'dsh/plugins/agent-profile/background-authority.mjs').as_uri())};"
        "const source=JSON.parse(process.env.TEST_SOURCE);"
        "const parent={}, child={}, turn={}; const controller=new AbortController();"
        "bindParentTurn(parent,turn,source);"
        "const authority=createResearchAuthority(parent,turn,source,'task-1','group-sandbox',controller.signal);"
        "bindResearchChild(child,authority);"
        "const delegated=researchDelegation(child);"
        "const decision=()=>canWriteGroupWorkspace(delegated.source,{"
        "stateFile:process.env.V2_CONSOLE_STATE_FILE,ownersRaw:'[]'});"
        "process.stdout.write(String(decision()));"
    )
    env = {**os.environ, "V2_CONSOLE_STATE_FILE": str(state_file),
           "TEST_SOURCE": json.dumps(source)}
    first = subprocess.run([NODE or "node", "--input-type=module", "-e", script],
                           text=True, capture_output=True, check=True, env=env)
    assert first.stdout == "true"
    _state(state_file, [])
    second = subprocess.run([NODE or "node", "--input-type=module", "-e", script],
                            text=True, capture_output=True, check=True, env=env)
    assert second.stdout == "false"


def test_configured_missing_or_invalid_console_state_denies_member_writes(tmp_path):
    state_file = tmp_path / "private" / "missing.json"
    source = {"platform_id": "qq", "bot_id": "bot-1", "conversation_key": "group:123",
              "requester_id": "10001"}
    state_file.parent.mkdir(mode=0o700)
    assert _node_policy(state_file, source, []) is False
    state_file.write_text('{broken', encoding="utf-8")
    state_file.chmod(0o600)
    assert _node_policy(state_file, source, []) is False


def test_unset_console_path_preserves_legacy_contract(tmp_path):
    source = {"platform_id": "qq", "bot_id": "bot-1", "conversation_key": "group:123",
              "requester_id": "10001"}
    script = (
        f"import {{canWriteGroupWorkspace}} from {json.dumps(POLICY.as_uri())};"
        "process.stdout.write(String(canWriteGroupWorkspace(JSON.parse(process.env.TEST_SOURCE),"
        "{stateFile:undefined})));"
    )
    result = subprocess.run([NODE or "node", "--input-type=module", "-e", script], text=True,
                            capture_output=True, check=True,
                            env={**os.environ, "TEST_SOURCE": json.dumps(source),
                                 "V2_CONSOLE_STATE_FILE": ""})
    assert result.stdout == "true"
