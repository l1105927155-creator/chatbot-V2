"""Journal/session integration around real MCP protocol requests."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT=Path(__file__).resolve().parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module

journal=load('phase4_journal','astrbot-plugins/v2_journal/journal.py')
state=load('phase4_state','astrbot-plugins/v2_dsh_router/state.py')
service=load('phase4_service','astrbot-plugins/v2_dsh_router/service.py')
mcp=load('phase4_mcp','astrbot-plugins/v2_dsh_router/mcp_server.py')

def append(store,mid,text,outbound=False):
    msg=journal.message_from_onebot('qq',{'post_type':'message_sent' if outbound else 'message',
        'message_sent_type':'self' if outbound else None,'message_type':'group',
        'self_id':1,'user_id':1 if outbound else 2,'group_id':3,'message_id':mid,
        'message':[{'type':'text','data':{'text':text}}]})
    return store.append(msg)[0]

async def invoke(declaration,calls):
    headers={h['name']:h['value'] for h in declaration['headers']}
    async with streamablehttp_client(declaration['url'],headers=headers) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            return [await session.call_tool(name,args) for name,args in calls]

@pytest.mark.asyncio
async def test_older_history_capability_send_and_followup_use_one_journal(tmp_path):
    j=journal.JournalStore(tmp_path/'journal.sqlite3');s=state.StateStore(tmp_path/'state.sqlite3')
    conv=journal.Conversation('qq','1','group:3')
    older=append(j,'old','older context: yellow pear')
    saved=s.get_or_create('qq','1','group:3','persisted-session')
    s.advance('qq','1','group:3',saved.session_id,older.journal_id)
    server=mcp.McpServer(j);await server.start(0)
    sent=[];inputs=[];results=[]
    async def send(text):
        sent.append(text);append(j,'sent-'+str(len(sent)),text,True)
    async def group():return SimpleNamespace(group_id='3',group_name='Contract group',member_count=7)
    class Model:
        def run(self,sid,prompt,declarations):
            assert sid=='persisted-session';inputs.append(prompt)
            if len(inputs)==1:
                found,info,submission=asyncio.run(invoke(declarations[0],[
                    ('history_search',{'query':'yellow pear','before_id':2}),
                    ('current_group_info',{}),('qq_send_origin',{'text':'tool-visible message'}),
                ]))
                assert not any(r.isError for r in (found,info,submission))
                results.extend((found.structuredContent,info.structuredContent,submission.structuredContent))
                return 'natural answer: yellow pear; Contract group has 7 members'
            return 'I saw the tool output in the canonical journal'
    svc=service.ChatService(j,s,Model(),server)
    try:
        first=append(j,'in-1','look up earlier fact, current group and send a tool message')
        await svc.handle(conv,'in-1',send,get_group=group)
        assert [json.loads(x)['journal_id'] for x in inputs[0].splitlines()[1:]]==[first.journal_id]
        assert results[0]['entries'][0]['content']['text']=='older context: yellow pear'
        assert results[1]['name']=='Contract group' and results[1]['member_count']==7
        assert results[2]=={'submitted':True,'platform_confirmed':False}
        assert sent[0]=='tool-visible message'
        second=append(j,'in-2','what did you send?')
        await svc.handle(conv,'in-2',send,get_group=group)
        assert [json.loads(x)['content']['text'] for x in inputs[1].splitlines()[1:]]==[
            'tool-visible message','natural answer: yellow pear; Contract group has 7 members','what did you send?']
        progress=s.get('qq','1','group:3')
        assert progress.session_id=='persisted-session' and progress.last_seen_journal_id==second.journal_id
    finally:await server.close()

@pytest.mark.asyncio
async def test_mcp_failure_can_be_explained_without_fabricating_action(tmp_path):
    j=journal.JournalStore(tmp_path/'journal.sqlite3');s=state.StateStore(tmp_path/'state.sqlite3')
    conv=journal.Conversation('qq','1','group:3');inbound=append(j,'in','ask current group')
    server=mcp.McpServer(j);await server.start(0)
    async def fail_group():raise OSError('capability unavailable')
    sent=[]
    async def send(text):sent.append(text)
    class Model:
        def create_session(self,declarations):return 'session'
        def run(self,sid,prompt,declarations):
            result=asyncio.run(invoke(declarations[0],[('current_group_info',{})]))[0]
            assert result.isError
            return 'The group lookup failed; please try later'
    try:
        await service.ChatService(j,s,Model(),server).handle(conv,'in',send,get_group=fail_group)
        assert sent==['The group lookup failed; please try later']
        assert [r.journal_id for r in j.recent(conv)]==[inbound.journal_id]
        progress=s.get('qq','1','group:3')
        assert progress.last_seen_journal_id==inbound.journal_id and progress.pending_upper_cursor is None
        # This cursor records successful consumption of user input, not success
        # of the failed capability. No action row was written by the tool.
    finally:await server.close()
