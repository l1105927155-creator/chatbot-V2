"""Workspace task tools use canonical requester identity and scoped live tokens."""
from __future__ import annotations

import httpx
import pytest

from test_mcp_server import append, connect, journal, mcp_module


@pytest.mark.asyncio
async def test_task_tools_bind_origin_actor_and_allow_current_group_sends(tmp_path):
    store = journal.JournalStore(tmp_path / 'journal.sqlite3')
    origin = journal.Conversation('qq-test', '10', 'group:30')
    private = journal.Conversation('qq-test', '10', 'private:20')
    append(store, origin, '1', '每晚写日记')
    calls, sent = [], []

    class Tasks:
        async def list(self, conversation, task_id=None):
            calls.append(('list', conversation, task_id))
            return {'tasks': [], 'revision': 0}

        async def manage(self, conversation, action, task_id, definition, revision, **provenance):
            calls.append(('manage', conversation, action, provenance))
            return {'registered': True, 'task_id': 'task-a'}

    async def send(text, **_):
        sent.append(text)

    server = mcp_module.McpServer(store)
    server.task_manager = Tasks()
    await server.start(0)
    try:
        async with server.turn(origin, send, source_message_id='1') as declarations:
            headers = {h['name'].lower(): h['value'] for h in declarations[0]['headers']}
            stack, client = await connect(server.url, headers)
            try:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert 'actor_id' not in tools['task_manage'].inputSchema['properties']
                assert 'group_id' not in tools['task_manage'].inputSchema['properties']
                result = await client.call_tool('task_manage', {'action': 'create', 'definition': {
                    'name': '日记', 'prompt': 'write diary', 'actor_id': '999'}})
                assert not result.isError
                assert calls[-1][1] == origin
                assert calls[-1][-1] == {'actor_id': '20', 'source_message_id': '1'}
                assert not (await client.call_tool('task_list')).isError
            finally:
                await stack.aclose()
        chat_headers = headers
        async with server.turn(origin, send, execution_kind='scheduled', actor_id='20',
                               source_message_id='1', history_through=1) as declarations:
            headers = {h['name'].lower(): h['value'] for h in declarations[0]['headers']}
            assert headers != chat_headers
            async with httpx.AsyncClient() as http:
                assert (await http.post(server.url, json={}, headers=chat_headers)).status_code == 401
            stack, client = await connect(server.url, headers)
            try:
                assert not (await client.call_tool('task_list')).isError
                before = len(calls)
                assert (await client.call_tool('task_manage', {'action': 'delete', 'task_id': 'task-a'})).isError
                assert not (await client.call_tool('qq_send_origin', {'text': 'requested diary'})).isError
                assert (await client.call_tool('bili_subscribe', {'uid': 123})).isError
                assert sent == ['requested diary'] and len(calls) == before
            finally:
                await stack.aclose()
        async with server.turn(private, send) as declarations:
            headers = {h['name'].lower(): h['value'] for h in declarations[0]['headers']}
            stack, client = await connect(server.url, headers)
            try:
                assert (await client.call_tool('task_list')).isError
            finally:
                await stack.aclose()
        async with server.turn(origin, send) as declarations:
            headers = {h['name'].lower(): h['value'] for h in declarations[0]['headers']}
            stack, client = await connect(server.url, headers)
            try:
                assert (await client.call_tool('task_manage', {'action': 'create'})).isError
            finally:
                await stack.aclose()
    finally:
        await server.close()
