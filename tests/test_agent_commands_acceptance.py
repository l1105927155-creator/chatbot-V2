"""Small deterministic contracts for the full-process command acceptance."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_commands_acceptance", ROOT / "scripts/check-agent-commands.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
sys.path.insert(0, str(ROOT))
from sandbox.control import SandboxControl  # noqa: E402


def test_history_floor_evidence_accepts_only_post_reset_rows():
    result = {"entries": [{"journal_id": 11, "content": {"text": "new session"}}]}
    assert module.assert_history_floor(result, 10) == result["entries"]


def test_history_floor_evidence_rejects_pre_reset_rows_and_old_marker():
    with pytest.raises(AssertionError, match="at or before"):
        module.assert_history_floor({"entries": [{"journal_id": 10}]}, 10)
    with pytest.raises(AssertionError, match="before the session floor"):
        module.assert_history_floor({"entries": [{"journal_id": 11, "text": "OLD_MARKER"}]},
                                    10, "OLD_MARKER")


def test_native_tool_result_parser_requires_actual_structured_history_result():
    pair = {"result": {"message": {"content": [{"type": "text", "text": '{"entries":[]}'}]}}}
    assert module.history_payload(pair) == {"entries": []}
    with pytest.raises(ValueError):
        module.history_payload({"result": {"message": {"content": [
            {"type": "text", "text": "not JSON"}
        ]}}})


def test_scripted_sandbox_allows_only_the_three_native_history_tools():
    class Store:
        def __init__(self):
            self.db = sqlite3.connect(":memory:")

        @staticmethod
        def conversations():
            return [{"type": "private", "id": "20"}]

        @staticmethod
        def get_member(_member_id):
            return None

    control = SandboxControl(Store())
    for tool in ("mcp__astrbot__history_recent", "mcp__astrbot__history_before",
                 "mcp__astrbot__history_search"):
        created = control.add_script({"conversation_key": "private:20", "kind": "tool", "name": tool})
        assert created["name"] == tool
    with pytest.raises(ValueError, match="unsupported scripted tool"):
        control.add_script({"conversation_key": "private:20", "kind": "tool", "name": "arbitrary-tool"})


def test_live_check_shares_command_helpers_but_starts_live_runtime(monkeypatch):
    checker = object.__new__(module.LiveCommandCheck)
    assert hasattr(checker, "_journal_row")
    assert hasattr(checker, "_deterministic")
    assert hasattr(checker, "_command_journal_id")
    started = []
    monkeypatch.setattr(module.live_helpers.LiveOwner, "start", lambda self: started.append(self))
    checker.start()
    assert started == [checker]
