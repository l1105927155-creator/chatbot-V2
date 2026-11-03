"""Host transport contracts for private, session-free memory model calls."""
import importlib.util
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'astrbot-plugins/v2_dsh_router/dsh_client.py'
sys.path.insert(0, str(SOURCE.parent))
spec = importlib.util.spec_from_file_location('v2_dsh_client_memory_contract', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _server(path, response, *, wait_for_disconnect=False):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    received = []
    disconnected = threading.Event()

    def serve():
        connection, _ = listener.accept()
        with connection:
            data = bytearray()
            while b'\n' not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
            received.append(json.loads(bytes(data).split(b'\n', 1)[0]))
            if wait_for_disconnect:
                while connection.recv(4096):
                    pass
                disconnected.set()
                return
            connection.sendall(json.dumps(response).encode() + b'\n')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread, received, disconnected


def test_acp_runtime_one_shot_request_validates_route_and_usage(tmp_path):
    path = tmp_path / 'memory.sock'
    expected = {'ok': True, 'provider': 'subscribed', 'model': 'selected',
                'text': 'new memory', 'usage': {'inputTokens': 10, 'outputTokens': 4}}
    listener, thread, received, _ = _server(path, expected)
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'x' * 64
    try:
        result = runtime.consolidate_memory(provider='subscribed', model='selected',
                                            system='persona', prompt='bounded source',
                                            expected_capacity={'contextWindow': 128000, 'defaultMaxTokens': 20000})
    finally:
        listener.close()
        thread.join(2)
    assert result == {'provider': 'subscribed', 'model': 'selected',
                      'text': 'new memory', 'usage': {'inputTokens': 10, 'outputTokens': 4}}
    assert received[0]['expectedCapacity'] == {'contextWindow': 128000, 'defaultMaxTokens': 20000}
    assert received[0]['action'] == 'consolidate'
    assert received[0]['system'] == 'persona'
    assert received[0]['prompt'] == 'bounded source'
    assert received[0]['token'] == 'x' * 64


def test_acp_runtime_accepts_large_utf8_memory_response(tmp_path):
    path = tmp_path / 'memory.sock'
    text = '群聊记忆' * 20_000
    expected = {'ok': True, 'provider': 'p', 'model': 'm', 'text': text, 'usage': None}
    listener, thread, _received, _ = _server(path, expected)
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'l' * 64
    try:
        result = runtime.consolidate_memory(provider='p', model='m', system='角色', prompt='消息')
    finally:
        listener.close()
        thread.join(2)
    assert result['text'] == text


def test_native_capacity_metadata_is_exact_and_missing_is_not_invented(tmp_path):
    path = tmp_path / 'memory.sock'
    listener, thread, received, _ = _server(path, {
        'ok': True, 'provider': 'subscribed', 'model': 'selected',
        'contextWindow': 128_000, 'defaultMaxTokens': 20_000,
        'capacitySource': 'native_adapter'})
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'c' * 64
    try:
        result = runtime.memory_capacity(provider='subscribed', model='selected')
    finally:
        listener.close()
        thread.join(2)
    assert result == {'provider': 'subscribed', 'model': 'selected',
                      'contextWindow': 128_000, 'defaultMaxTokens': 20_000,
                      'capacitySource': 'native_adapter'}
    assert received[0] == {'action': 'capacity', 'provider': 'subscribed', 'model': 'selected',
                           'token': 'c' * 64}

    path = tmp_path / 'unknown.sock'
    listener, thread, _received, _ = _server(path, {
        'ok': True, 'provider': 'p', 'model': 'm', 'capacitySource': 'native_adapter'})
    runtime._memory_control_path = str(path)
    try:
        result = runtime.memory_capacity(provider='p', model='m')
    finally:
        listener.close()
        thread.join(2)
    assert result == {'provider': 'p', 'model': 'm', 'contextWindow': None,
                      'defaultMaxTokens': None, 'capacitySource': 'native_adapter'}


def test_memory_capacity_unknown_error_code_survives_python_transport(tmp_path):
    path = tmp_path / 'capacity-unknown.sock'
    listener, thread, _received, _ = _server(path, {
        'ok': False, 'code': 'MEMORY_CAPACITY_UNKNOWN'})
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'u' * 64
    try:
        with pytest.raises(module.MemoryModelError) as caught:
            runtime.consolidate_memory(provider='p', model='m', system='persona', prompt='source')
    finally:
        listener.close()
        thread.join(2)
    assert caught.value.error_code == 'MEMORY_CAPACITY_UNKNOWN'


def test_acp_runtime_cancel_disconnects_one_shot_socket(tmp_path):
    path = tmp_path / 'memory.sock'
    listener, thread, received, disconnected = _server(path, None, wait_for_disconnect=True)
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'y' * 64
    cancelled = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        call = pool.submit(runtime.consolidate_memory, provider='p', model='m',
                           system='persona', prompt='source', cancel_event=cancelled)
        deadline = time.monotonic() + 2
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received
        cancelled.set()
        with pytest.raises(module.DshTurnCancelled):
            call.result(2)
    listener.close()
    thread.join(2)
    assert disconnected.wait(1)


def _progress_server(path, frames, delay=.025):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve():
        connection, _ = listener.accept()
        with connection:
            request = b''
            while b'\n' not in request:
                request += connection.recv(4096)
            for frame in frames:
                try:
                    wire = json.dumps(frame).encode() + b'\n'
                    connection.sendall(wire[:7])
                    connection.sendall(wire[7:])
                except OSError:
                    break
                time.sleep(delay)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread


def test_progress_frames_extend_idle_without_total_deadline_and_hide_text(tmp_path):
    path = tmp_path / 'progress.sock'
    frames = [dict(type='progress', phase='reasoning', elapsed_ms=i*25,
                   event_count=i, text_bytes=0, reasoning_bytes=i*3, idle_ms=0,
                   heartbeat=i % 2 == 0) for i in range(8)]
    frames.append(dict(ok=True, provider='p', model='m', text='done', usage=None))
    listener, thread = _progress_server(path, frames)
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path, runtime._memory_control_token = str(path), 'x'*64
    runtime._memory_transport_idle_seconds = .08
    seen = []
    try:
        result = runtime.consolidate_memory(provider='p', model='m', system='system',
                                             prompt='source', on_progress=seen.append)
    finally:
        listener.close(); thread.join(2)
    assert result['text'] == 'done' and len(seen) == 8
    assert seen[-1]['reasoning_bytes'] == 21
    assert all('text' not in item and 'type' not in item for item in seen)


def test_transport_silence_is_classified_and_callback_errors_close_socket(tmp_path):
    path = tmp_path / 'silent.sock'
    listener, thread, _, disconnected = _server(path, None, wait_for_disconnect=True)
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path, runtime._memory_control_token = str(path), 'x'*64
    runtime._memory_transport_idle_seconds = .03
    try:
        with pytest.raises(module.MemoryModelError) as error:
            runtime.consolidate_memory(provider='p', model='m', system='system', prompt='source',
                                       on_progress=lambda item: None)
        assert error.value.error_code == 'MEMORY_TRANSPORT_TIMEOUT'
    finally:
        listener.close(); thread.join(2)
    assert disconnected.is_set()

    path = tmp_path / 'callback.sock'
    listener, thread = _progress_server(path, [dict(type='progress', phase='waiting_model',
        elapsed_ms=0, event_count=0, text_bytes=0, reasoning_bytes=0, idle_ms=0, heartbeat=False)])
    runtime._memory_control_path = str(path)

    def fail_callback(_event):
        raise RuntimeError('private callback error')

    try:
        with pytest.raises(module.MemoryModelError) as error:
            runtime.consolidate_memory(provider='p', model='m', system='system', prompt='source',
                                       on_progress=fail_callback)
        assert error.value.error_code == 'MODEL_INVALID_RESPONSE'
        assert 'private' not in str(error.value)
    finally:
        listener.close(); thread.join(2)


@pytest.mark.parametrize(("response", "code", "status"), [
    ({'ok': False, 'code': 'MODEL_OUTPUT_LIMIT'}, 'MODEL_OUTPUT_LIMIT', None),
    ({'ok': False, 'code': 'RATE_LIMITED', 'status': 503,
      'message': 'SECRET api-key=must-not-leak'}, 'RATE_LIMITED', 503),
    ({'ok': False, 'code': 'bad code', 'status': 999,
      'message': 'SECRET api-key=must-not-leak'}, 'MODEL_INVALID_RESPONSE', None),
])
def test_model_failures_keep_only_bounded_safe_metadata(tmp_path, response, code, status):
    path = tmp_path / 'memory.sock'
    listener, thread, _received, _ = _server(path, response)
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'z' * 64
    try:
        with pytest.raises(module.MemoryModelError) as raised:
            runtime.consolidate_memory(provider='p', model='m', system='persona', prompt='source')
    finally:
        listener.close()
        thread.join(2)
    assert raised.value.error_code == code
    assert raised.value.status == status
    assert str(raised.value) == 'memory model call failed'
    assert 'SECRET' not in repr(raised.value.__dict__)


def test_memory_rpc_rejects_oversized_response_with_stable_code(tmp_path):
    path = tmp_path / 'memory.sock'
    listener, thread, _received, _ = _server(path, {
        'ok': True, 'provider': 'p', 'model': 'm', 'text': 'x' * (4 * 1024 * 1024 + 32),
        'usage': None})
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 's' * 64
    try:
        with pytest.raises(module.MemoryModelError) as raised:
            runtime.consolidate_memory(provider='p', model='m', system='persona', prompt='source')
    finally:
        listener.close()
        thread.join(2)
    assert raised.value.error_code == 'MEMORY_RESPONSE_TOO_LARGE'


def test_dsh_client_delegates_without_session_reservation_and_fails_closed(tmp_path):
    for name in ('dsh', 'patch.yml', 'instructions.md'):
        (tmp_path / name).touch()

    class Runtime:
        def __init__(self, **_):
            self.calls = []
        def consolidate_memory(self, **kwargs):
            self.calls.append(kwargs)
            return {'provider': kwargs['provider'], 'model': kwargs['model'],
                    'text': 'memory', 'usage': None}
        def memory_capacity(self, **kwargs):
            self.calls.append(kwargs)
            return {'provider': kwargs['provider'], 'model': kwargs['model'],
                    'contextWindow': None, 'defaultMaxTokens': None,
                    'capacitySource': 'native_adapter'}
        def close(self):
            pass

    client = module.DshClient(dsh_bin=tmp_path / 'dsh', dsh_home=tmp_path / 'home',
                              workspace=tmp_path / 'work', patch=tmp_path / 'patch.yml',
                              instructions=tmp_path / 'instructions.md', provider='p', model='m',
                              harness_factory=Runtime)
    event = threading.Event()
    try:
        result = client.consolidate_memory(provider='p', model='selected', system='persona',
                                           prompt='source', cancel_event=event)
        assert result['model'] == 'selected'
        assert client.runtime.calls == [{'provider': 'p', 'model': 'selected', 'system': 'persona',
                                         'prompt': 'source', 'cancel_event': event, 'expected_capacity': None}]
        assert client._turn_tokens == {}
        assert client._session_locks == {}
        capacity = client.memory_capacity(provider='p', model='selected', cancel_event=event)
        assert capacity['capacitySource'] == 'native_adapter'
        assert client.runtime.calls[-1] == {'provider': 'p', 'model': 'selected',
                                            'cancel_event': event}
    finally:
        client.close()

    class UnsupportedRuntime:
        def close(self):
            pass

    client = module.DshClient(dsh_bin=tmp_path / 'dsh', dsh_home=tmp_path / 'home2',
                              workspace=tmp_path / 'work2', patch=tmp_path / 'patch.yml',
                              instructions=tmp_path / 'instructions.md', provider='p', model='m',
                              harness_factory=lambda **_: UnsupportedRuntime())
    try:
        with pytest.raises(RuntimeError, match='unsupported'):
            client.consolidate_memory(provider='p', model='m', system='persona', prompt='source')
        with pytest.raises(RuntimeError, match='unsupported'):
            client.memory_capacity(provider='p', model='m')
    finally:
        client.close()


@pytest.mark.parametrize("source", ["native_subscription_catalog", "untrusted_capacity", None, [], {}])
def test_capacity_transport_preserves_authenticated_catalog_and_rejects_unknown_source(tmp_path, source):
    path = tmp_path / 'capacity.sock'
    listener, thread, _received, _ = _server(path, {
        'ok': True, 'provider': 'openai-codex', 'model': 'gpt-6.1-terra',
        'contextWindow': 500_000, 'defaultMaxTokens': 128_000, 'capacitySource': source})
    runtime = object.__new__(module.AcpRuntime)
    runtime._memory_control_path = str(path)
    runtime._memory_control_token = 'c' * 64
    try:
        if source != 'native_subscription_catalog':
            with pytest.raises(module.MemoryModelError) as error:
                runtime.memory_capacity(provider='openai-codex', model='gpt-6.1-terra')
            assert error.value.error_code == 'MODEL_INVALID_RESPONSE'
        else:
            result = runtime.memory_capacity(provider='openai-codex', model='gpt-6.1-terra')
            assert result['capacitySource'] == source
            assert (result['contextWindow'], result['defaultMaxTokens']) == (500_000,128_000)
    finally:
        listener.close()
        thread.join(2)


def test_background_terminal_remains_on_runtime_with_native_control_receipt(tmp_path):
    runtime = object.__new__(module.AcpRuntime)
    # This lookup catches a class-boundary regression before any socket operation.
    lookup = runtime.background_terminal
    path = tmp_path / 'terminal.sock'
    expected = {'event': 'settled', 'parent_session_id': 'sid', 'task_id': 'task'}
    listener, thread, received, _ = _server(path, {'ok': True, 'event': expected})
    runtime._console_control_path = str(path)
    runtime._console_control_token = 'test-local-token'
    try:
        assert lookup('sid', 'task') == expected
        thread.join(2)
        assert not thread.is_alive()
        assert received == [{'action': 'background_terminal', 'session_id': 'sid',
                             'task_id': 'task', 'token': 'test-local-token'}]
    finally:
        listener.close()
        thread.join(2)
