"""The group ignore list must silence at admission and fail open on bad data."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "astrbot-plugins/v2_dsh_router"
SOURCE = PLUGIN / "blacklist_gate.py"
QQ = "1105927155"
OTHER = "2205927155"


def _load(name: str, path: Path):
    """Import a plugin module without the package, which needs AstrBot."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate_module = _load("v2_blacklist_gate_contract", SOURCE)


def document(*entries, version=1):
    return {"version": version, "entries": {qq: {"qq": qq, **fields} for qq, fields in entries}}


def workspace(tmp_path, payload=None, *, document_value=None):
    """Create a workspace whose only interesting content is the ignore list."""
    root = tmp_path / "workspace"
    directory = root / "apps/blacklist/data"
    directory.mkdir(parents=True)
    if payload is not None:
        (directory / "members.json").write_bytes(payload)
    elif document_value is not None:
        (directory / "members.json").write_text(json.dumps(document_value), encoding="utf-8")
    return root


def root_opener(root):
    return lambda: os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def gate(root, **options):
    return gate_module.SilenceGate(root, root_opener(root), **options)


def test_an_unchanged_list_is_parsed_once(tmp_path, monkeypatch):
    """Reading the file per message is cheap; re-parsing it is the avoidable part."""
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    parsed = []
    real_parse = gate_module.parse

    def parse(payload):
        parsed.append(len(payload))
        return real_parse(payload)

    monkeypatch.setattr(gate_module, "parse", parse)
    silence_gate = gate(root)
    assert silence_gate.decide(QQ).blocked
    assert silence_gate.decide(OTHER).reason == "not_listed"
    assert silence_gate.decide(QQ).blocked
    assert len(parsed) == 1


def test_listed_member_is_blocked_and_unlisted_member_is_not(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))

    blocked = gate(root).decide(QQ)
    assert blocked.blocked and blocked.scope == "all"
    assert blocked.reason == "listed_all" and blocked.reported

    ordinary = gate(root).decide(OTHER)
    assert not ordinary.blocked and ordinary.reason == "not_listed"
    # Ordinary outcomes are routine: they must not write a diagnostic line.
    assert not ordinary.reported


def test_listed_member_stays_silent_even_when_addressing_the_bot(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    verdict = gate(root).decide(QQ, addressed=True)
    assert verdict.blocked and verdict.reason == "listed_all"


def test_mention_only_member_is_silent_unless_actually_addressed(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "mention_only"})))
    direct = gate(root).decide(QQ)
    assert direct.blocked and direct.scope == "mention_only"
    assert direct.reason == "listed_mention_only"

    addressed = gate(root).decide(QQ, addressed=True)
    assert not addressed.blocked and addressed.reason == "addressed_mention"
    assert not addressed.reported


def test_missing_list_fails_open_and_reports_one_diagnostic(tmp_path):
    root = workspace(tmp_path)
    silence_gate = gate(root)

    first = silence_gate.decide(QQ)
    assert not first.blocked and first.reason == "absent" and first.reported
    assert not silence_gate.decide(QQ).reported
    assert not silence_gate.decide(OTHER).reported


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b'{"version": 1, "entries": {', "corrupt_json"),
        (b"\xff\xfe{}", "corrupt_json"),
        (b"[]", "invalid_document"),
        (b'{"version": 2, "entries": {}}', "unsupported_version"),
        (b'{"version": true, "entries": {}}', "unsupported_version"),
        (b'{"version": 1, "entries": []}', "invalid_entries"),
        (b'{"entries": {}}', "unsupported_version"),
    ],
)
def test_damaged_documents_fail_open(tmp_path, payload, reason):
    root = workspace(tmp_path, payload)
    silence_gate = gate(root)
    assertion = silence_gate.decide(QQ)
    assert not assertion.blocked and assertion.reason == reason
    assert assertion.reported


def test_invalid_scope_fails_open_for_that_member(tmp_path):
    root = workspace(tmp_path, document_value=document(
        (QQ, {"scope": "sometimes"}), (OTHER, {"scope": "all"})))
    silence_gate = gate(root)

    invalid = silence_gate.decide(QQ)
    assert not invalid.blocked and invalid.reason == "invalid_scope" and invalid.reported
    # One bad entry must not disable the rest of the list.
    assert silence_gate.decide(OTHER).blocked


def test_parse_rejects_mismatched_and_non_qq_entries():
    snapshot = gate_module.parse(json.dumps({
        "version": 1,
        "entries": {
            "abc": {"qq": "abc", "scope": "all"},
            "12345": {"qq": "99999", "scope": "all"},
            "23456": {"scope": "all"},
            "34567": {"qq": "34567", "scope": "all"},
        },
    }).encode())

    assert snapshot.entries == {"34567": "all"}
    assert snapshot.invalid == {"abc": "invalid_qq", "12345": "invalid_entry",
                                "23456": "invalid_entry"}
    # A document-wide issue would be filled in only for read-level problems.
    assert snapshot.issue is None


def test_invalid_actor_is_not_blocked_before_reading_anything(tmp_path):
    calls = []
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    silence_gate = gate_module.SilenceGate(root, lambda: calls.append(1) or root_opener(root)())

    for actor in ("", None, "abc", QQ + "x", "1", True, 1105927155):
        decision = silence_gate.decide(actor)
        assert not decision.blocked and decision.reason == "invalid_actor"
    assert calls == []


def test_oversized_list_fails_open(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    decision = gate(root, max_bytes=32).decide(QQ)
    assert not decision.blocked and decision.reason == "oversize"


def test_symlinked_list_fails_open(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    members = root / "apps/blacklist/data/members.json"
    real = root / "apps/blacklist/data/real.json"
    members.rename(real)
    members.symlink_to(real)

    decision = gate(root).decide(QQ)
    assert not decision.blocked and decision.reason == "unsafe_path"


def test_symlinked_directory_fails_open(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    data = root / "apps/blacklist/data"
    elsewhere = tmp_path / "elsewhere"
    data.rename(elsewhere)
    data.symlink_to(elsewhere, target_is_directory=True)

    decision = gate(root).decide(QQ)
    assert not decision.blocked and decision.reason == "unsafe_path"


def test_hardlinked_list_fails_open(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    members = root / "apps/blacklist/data/members.json"
    os.link(members, root / "apps/blacklist/data/second-link.json")

    decision = gate(root).decide(QQ)
    assert not decision.blocked and decision.reason == "unexpected_hardlink"


def test_cross_device_list_fails_open(tmp_path, monkeypatch):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    real_fstat = os.fstat

    def fstat(fd):
        info = real_fstat(fd)
        # Only the data file is moved to another device; the root stays put.
        return os.stat_result(tuple(info)[:2] + (info.st_dev + 1,) + tuple(info)[3:]) \
            if stat.S_ISREG(info.st_mode) else info

    monkeypatch.setattr(gate_module.os, "fstat", fstat)
    decision = gate(root).decide(QQ)
    assert not decision.blocked and decision.reason == "cross_device"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (FileNotFoundError(2, "gone"), "absent"),
        (PermissionError(13, "denied"), "permission_denied"),
        (OSError(5, "io"), "io_error"),
        (RuntimeError("no verifier"), "workspace_unavailable"),
    ],
)
def test_unverifiable_workspace_fails_open(tmp_path, failure, reason):
    def verify():
        raise failure

    decision = gate_module.SilenceGate(tmp_path, verify).decide(QQ)
    assert not decision.blocked and decision.reason == reason


def test_unexpected_reader_failure_fails_open(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))

    def opener(*_args, **_kwargs):
        raise ValueError("adapter bug")

    decision = gate_module.SilenceGate(root, root_opener(root), opener=opener).decide(QQ)
    assert not decision.blocked
    assert decision.reason == "gate_error:ValueError"


def test_cache_follows_in_place_edits_and_atomic_replacements(tmp_path):
    root = workspace(tmp_path, document_value=document((QQ, {"scope": "all"})))
    silence_gate = gate(root)
    assert silence_gate.decide(QQ).blocked

    members = root / "apps/blacklist/data/members.json"
    members.write_text(json.dumps(document((OTHER, {"scope": "all"})), indent=2))
    assert not silence_gate.decide(QQ).blocked
    assert silence_gate.decide(OTHER).blocked

    staged = members.with_suffix(".json.new")
    staged.write_text(json.dumps(document((QQ, {"scope": "mention_only"}))))
    os.replace(staged, members)
    replaced = silence_gate.decide(QQ)
    assert replaced.blocked and replaced.scope == "mention_only"


def test_provided_writer_refresh_is_observed_on_the_next_message(tmp_path):
    """The workspace app's own write path must be visible without a restart."""
    root = workspace(tmp_path, document_value=document())
    silence_gate = gate(root)
    assert not silence_gate.decide(QQ).blocked

    members = root / "apps/blacklist/data/members.json"
    payload = json.dumps(document((QQ, {"scope": "all"})), indent=2)
    staged = members.with_suffix(".json.staged")
    staged.write_text(payload, encoding="utf-8")
    os.replace(staged, members)

    assert silence_gate.decide(QQ).blocked


def test_read_problems_do_not_leak_one_diagnostic_per_message(tmp_path):
    root = workspace(tmp_path, b'{"version": 1, "entries": ')
    silence_gate = gate(root)
    reported = [silence_gate.decide(QQ).reported for _ in range(4)]
    assert reported == [True, False, False, False]


def test_gate_keeps_working_when_the_cache_is_full(tmp_path):
    for index in range(gate_module._CACHE_LIMIT + 2):
        root = workspace(tmp_path / str(index), document_value=document((QQ, {"scope": "all"})))
        assert gate(root).decide(QQ).blocked
