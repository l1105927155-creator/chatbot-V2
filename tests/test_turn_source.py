"""Security and lifecycle contracts for private per-turn source files."""
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router/turn_source.py'
spec = importlib.util.spec_from_file_location('v2_turn_source_contract', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

FACTS = {
    'platform_id': 'qq', 'bot_id': '2869323076',
    'conversation_key': 'private:1105927155', 'requester_id': '1105927155',
    'inbound_message_id': 'message-1',
}


def test_publish_atomic_private_payload_and_nonce_matched_cleanup(tmp_path):
    publisher = module.TurnSourcePublisher(tmp_path)
    nonce = publisher.publish('session-id', FACTS, 'canonical journal delta')
    target = publisher.directory / (module.TurnSourcePublisher.filename('session-id'))
    assert publisher.root.stat().st_mode & 0o777 == 0o700
    assert publisher.directory.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    payload = json.loads(target.read_text())
    assert payload == {
        'epoch': publisher.epoch, 'session_id': 'session-id', 'nonce': nonce,
        'prompt_sha256': hashlib.sha256(b'canonical journal delta').hexdigest(), **FACTS,
    }
    assert not list(publisher.directory.glob('*.tmp'))
    assert publisher.cleanup('session-id', 'wrong-nonce') is False
    assert target.exists()
    assert publisher.cleanup('session-id', nonce) is True
    assert not target.exists()
    publisher.close()
    assert not publisher.directory.exists()


def test_cleanup_refuses_replaced_nonce_and_close_keeps_unowned_file(tmp_path):
    publisher = module.TurnSourcePublisher(tmp_path)
    nonce = publisher.publish('session-id', FACTS, 'canonical journal delta')
    target = publisher.directory / module.TurnSourcePublisher.filename('session-id')
    payload = json.loads(target.read_text())
    payload['nonce'] = 'replacement'
    target.write_text(json.dumps(payload))
    assert publisher.cleanup('session-id', nonce) is False
    with pytest.raises(ExceptionGroup, match='turn source cleanup failed'):
        publisher.close()
    # A mismatched file is not deleted as though it belonged to this publisher.
    assert target.exists()


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'blank', 'newline', 'non_string'])
def test_source_requires_exact_nonempty_string_facts(tmp_path, mutation):
    publisher = module.TurnSourcePublisher(tmp_path)
    source = dict(FACTS)
    if mutation == 'extra': source['role'] = 'owner'
    elif mutation == 'missing': source.pop('requester_id')
    elif mutation == 'blank': source['requester_id'] = ' '
    elif mutation == 'newline': source['requester_id'] = 'a\nb'
    elif mutation == 'non_string': source['requester_id'] = 123
    with pytest.raises(ValueError): publisher.publish('session-id', source, 'delta')
    assert list(publisher.directory.iterdir()) == []
    publisher.close()


def test_publish_rejects_overlapping_same_session_and_failed_cleanup_is_reported(tmp_path, monkeypatch):
    publisher = module.TurnSourcePublisher(tmp_path)
    nonce = publisher.publish('session-id', FACTS, 'canonical journal delta')
    with pytest.raises(RuntimeError, match='already published'):
        publisher.publish('session-id', FACTS, 'another delta')
    target = publisher.directory / module.TurnSourcePublisher.filename('session-id')
    original_unlink = Path.unlink
    def failed_unlink(path, *args, **kwargs):
        if path == target: raise PermissionError('cleanup blocked')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', failed_unlink)
    with pytest.raises(ExceptionGroup, match='turn source cleanup failed'):
        publisher.close()
    assert target.exists() and publisher._active['session-id'] == nonce


def test_runtime_epoch_is_random_and_stale_source_is_not_reused(tmp_path):
    old = module.TurnSourcePublisher(tmp_path)
    old_nonce = old.publish('same-session', FACTS, 'old delta')
    old_dir, old_epoch = old.directory, old.epoch
    current = module.TurnSourcePublisher(tmp_path)
    assert current.epoch != old_epoch and current.directory != old_dir
    assert not (current.directory / module.TurnSourcePublisher.filename('same-session')).exists()
    old_payload = json.loads((old_dir / module.TurnSourcePublisher.filename('same-session')).read_text())
    assert old_payload['nonce'] == old_nonce
    current.close()
    # Simulate crash leftovers: a new publisher does not reuse the previous epoch.
    assert old_dir.exists()
