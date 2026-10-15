from pathlib import Path

import pytest

from sandbox.control import SandboxControl, RevisionConflict
from sandbox.model import SandboxStore


def test_admission_is_independent_and_cas_and_owner_is_startup_only(tmp_path):
    store = SandboxStore(tmp_path / "onebot")
    control = SandboxControl(store, owner_id="90009")
    initial = control.scenario()
    assert initial["owner_id"] == "90009" and "private:90009" in initial["admitted"]
    store.add_conversation("group", "91999", "excluded")
    assert "group:91999" not in control.scenario()["admitted"]
    updated = control.update_scenario({"revision": initial["revision"], "admitted": ["group:91999"]})
    assert updated["admitted"] == ["group:91999"]
    with pytest.raises(RevisionConflict):
        control.update_scenario({"revision": initial["revision"], "admitted": []})
    with pytest.raises(ValueError):
        control.update_scenario({"revision": updated["revision"], "admitted": [], "owner_id": "90001"})
    with pytest.raises(ValueError, match="Owner identity"):
        SandboxControl(store, owner_id="90001")
    store.close()


def test_fault_exact_scope_nth_phase_and_one_shot_with_after_preview(tmp_path):
    store = SandboxStore(tmp_path / "onebot")
    control = SandboxControl(store)
    rule = control.arm_fault({"target": "model", "kind": "truncate", "phase": "after", "nth": 2,
                              "conversation_key": "group:91000", "session_id": "sid", "limit_chars": 5})
    request = {"target": "model", "session_id": "sid", "native_turn_id": "turn", "call_id": "call"}
    assert not control.claim({**request, "phase": "after"}, conversation_key="private:90001")["fault"]
    assert control.fault(rule["id"])["seen"] == 0
    assert not control.claim({**request, "phase": "before"}, conversation_key="group:91000")["buffer_for_after"]
    assert not control.claim({**request, "phase": "after"}, conversation_key="group:91000")["fault"]
    assert control.claim({**request, "phase": "before"}, conversation_key="group:91000")["buffer_for_after"]
    assert control.claim({**request, "phase": "after"}, conversation_key="group:91000")["fault"]["id"] == rule["id"]
    assert not control.claim({**request, "phase": "after"}, conversation_key="group:91000")["fault"]
    assert control.fault(rule["id"])["hit"]["call_id"] == "call"
    store.close()


def test_script_binding_is_per_native_turn_and_provider_target(tmp_path):
    store = SandboxStore(tmp_path / "onebot")
    control = SandboxControl(store)
    first = control.add_script({"conversation_key": "group:91000", "kind": "reply", "text": "first"})
    second = control.add_script({"conversation_key": "group:91000", "kind": "reply", "text": "second"})
    request = {"target": "model", "session_id": "sid", "native_turn_id": "turn-1", "phase": "before"}
    assert control.claim(request, conversation_key="group:91000")["script"]["id"] == first["id"]
    assert control.claim(request, conversation_key="group:91000")["script"]["id"] == first["id"]
    assert control.claim({**request, "native_turn_id": "turn-2"}, conversation_key="group:91000")["script"]["id"] == second["id"]
    assert not control.claim({**request, "target": "search"}, conversation_key="group:91000")["script"]
    store.close()


def test_private_runtime_probe_metadata_rotation_and_bounded_inputs(tmp_path):
    store = SandboxStore(tmp_path / "onebot")
    control = SandboxControl(store)
    raw = "x" * 50000 + "RAW_TAIL"
    record = control.record_probe({"event": "model/start", "session_id": "sid", "turn_id": "turn",
                                   "input": raw, "access_token": "secret"})
    assert record["native_turn_id"] == "turn" and record["input_truncated"]
    assert len(record["input"]) == 32768 and record["input"].endswith("RAW_TAIL")
    assert record["input_projection"] == "tail"
    structured = control.record_probe({"event": "model/start", "session_id": "sid",
                                       "input": {"old": "x" * 50000, "latest": "STRUCTURED_TAIL"}})
    assert len(structured["input"]) == 32768 and structured["input"].endswith('"latest": "STRUCTURED_TAIL"}')
    assert structured["input_projection"] == "tail" and structured["input_truncated"]
    assert "access_token" not in record
    path = tmp_path / "runtime-events.jsonl"
    assert path.stat().st_mode & 0o077 == 0
    path.write_bytes(b" " * (8 * 1024 * 1024))
    control.record_probe({"event": "model/end", "session_id": "sid"})
    assert (tmp_path / "runtime-events.1.jsonl").exists() and path.stat().st_size < 2000
    store.close()


def test_native_numeric_turn_fault_scope_matches_real_probe(tmp_path):
    store = SandboxStore(tmp_path / "onebot")
    control = SandboxControl(store)
    rule = control.arm_fault({"target": "model", "kind": "truncate", "phase": "after",
                              "conversation_key": "group:91000", "native_turn_id": 1})
    context = {"target": "model", "phase": "before", "session_id": "sid", "native_turn_id": 1}
    assert not control.claim({**context, "native_turn_id": 2}, conversation_key="group:91000")["buffer_for_after"]
    assert control.claim(context, conversation_key="group:91000")["buffer_for_after"]
    hit = control.claim({**context, "phase": "after"}, conversation_key="group:91000")
    assert hit["fault"]["id"] == rule["id"]
    assert control.fault(rule["id"])["seen"] == 1
    store.close()
