"""Synthetic native probes can identify independent scheduled Sessions."""
import hashlib
import json
from types import SimpleNamespace

from sandbox.server import SandboxApp


def test_scheduled_source_mapping_is_exact_and_admitted(tmp_path):
    app = object.__new__(SandboxApp)
    app.evidence = SimpleNamespace(root=tmp_path, state_sources=lambda: [])
    app.control = SimpleNamespace(scenario=lambda: {'admitted': ['group:91000']})
    sid = 'scheduled-one'
    name = hashlib.sha256(sid.encode()).hexdigest() + '.json'
    path = tmp_path / 'repo/.runtime/dsh-home-group-example/v2-turn-sources/epoch' / name
    path.parent.mkdir(parents=True)
    source = {'session_id': sid, 'kind': 'scheduled', 'conversation_key': 'group:91000'}
    path.write_text(json.dumps(source))
    assert app._session_conversation(sid) == 'group:91000'
    assert app._session_conversation('other') is None
    path.write_text(json.dumps(dict(source, conversation_key='group:91999')))
    assert app._session_conversation(sid) is None
    path.write_text(json.dumps(dict(source, session_id='forged')))
    assert app._session_conversation(sid) is None
