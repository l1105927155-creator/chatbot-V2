"""Forward tools use the same authenticated turn and lifetime lease as QQ sends."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mcp_server import append, connect, journal, mcp_module


@pytest.mark.asyncio
async def test_forward_tools_bound_to_turn_and_lifetime(tmp_path):
    store = journal.JournalStore(tmp_path / 'journal.sqlite3')
    server = mcp_module.McpServer(store)
    origin = journal.Conversation('qq-test', '10', 'group:30')
    append(store, origin, '11', 'hello')
    calls = []

    async def read(*args):
        calls.append(('read', args))
        return {'nodes': [], 'next_offset': None}

    async def send(nodes):
        calls.append(('send', nodes))

    async def invoke(operation):
        calls.append('lease-enter')
        try:
            return await operation()
        finally:
            calls.append('lease-exit')

    messaging = SimpleNamespace(forwards=SimpleNamespace(read=read, send=send))
    await server.start(0)
    try:
        async with server.turn(origin, lambda _: None, messaging=messaging, invoke=invoke) as declarations:
            headers = {i['name']: i['value'] for i in declarations[0]['headers']}
            stack, client = await connect(server.url, headers)
            try:
                names = {t.name for t in (await client.list_tools()).tools}
                assert {'history_read_forward', 'history_read_forward_image', 'qq_send_forward'} <= names
                result = await client.call_tool('history_read_forward', {'journal_id': 1, 'segment_index': 0})
                assert not result.isError
                assert calls == ['lease-enter', ('read', (1, 0, None, 0, 10)), 'lease-exit']
                calls.clear()
                assert (await client.call_tool('history_read_forward', {'journal_id': True, 'segment_index': 0})).isError
                assert not calls
                result = await client.call_tool('qq_send_forward', {'nodes': [{'text': 'hello'}], 'group_id': 999})
                assert not result.isError
                assert calls == ['lease-enter', ('send', [{'text': 'hello'}]), 'lease-exit']
            finally:
                await stack.aclose()
        assert not server._active_tokens
        calls.clear()
        async with server.turn(origin, lambda _: None, messaging=messaging, invoke=invoke,
                               execution_kind='scheduled', actor_id='20', source_message_id='11') as declarations:
            headers = {i['name']: i['value'] for i in declarations[0]['headers']}
            stack, client = await connect(server.url, headers)
            try:
                for name, args in [('qq_send_forward', {'nodes': [{'text': 'hi'}]}),
                                   ('history_read_forward', {'journal_id': 1, 'segment_index': 0})]:
                    assert (await client.call_tool(name, args)).isError
                assert not calls
            finally:
                await stack.aclose()
    finally:
        await server.close()
