"""Real pinned ACP, native subagent/jobs/web, host callback and Journal integration."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'astrbot-plugins/v2_dsh_router'))
sys.path.insert(0, str(ROOT / 'astrbot-plugins/v2_journal'))
from dsh_client import DshClient
from journal import Conversation, JournalStore, message_from_onebot
from mcp_server import McpServer
from service import ChatService
from state import StateStore


@pytest.mark.parametrize('profile', ['safe-chat', 'owner-host', 'group-sandbox'])
def test_native_background_foreground_update_completion_and_stop(tmp_path, monkeypatch, profile):
    home, workspace = tmp_path/'home', tmp_path/'workspace'
    home.mkdir(mode=0o700); workspace.mkdir(mode=0o700)
    group_runtime = None
    if profile == 'group-sandbox':
        sys.path.insert(0, str(ROOT/'dsh/plugins/agent-profile'))
        import group_volume
        volume = group_volume.prepare_group(tmp_path, 'qq', '1', '3')
        workspace = Path(volume['workspace'])
        group_runtime = {'workspaceRoot':str(workspace),'platformId':'qq','botId':'1','groupId':'3'}
    instructions = tmp_path/'instructions.md'
    instructions.write_text('Use the requested tool and report its result.\n')
    log, release = tmp_path/'probe.jsonl', tmp_path/'release'
    parallel_releases = {tag: tmp_path/f'release-{tag}' for tag in 'ABCD'}
    monkeypatch.setenv('V2_BG_PROBE_LOG', str(log))
    monkeypatch.setenv('V2_BG_PROBE_RELEASE', str(release))
    monkeypatch.setenv('V2_EXECUTION_PROFILE', profile)
    for tag, path in parallel_releases.items():
        monkeypatch.setenv(f'V2_BG_PROBE_RELEASE_{tag}', str(path))
    (workspace/'research.txt').write_text('LOCAL_RESEARCH_FIXTURE')
    monkeypatch.setenv('V2_BG_PROBE_FILE', str(workspace/'research.txt'))
    monkeypatch.setenv('V2_QQ_OWNERS', json.dumps([
        {'platform_id':'qq','bot_id':'1','user_id':'2'}]))
    monkeypatch.delenv('V2_SHARED_MODELS_PROFILE', raising=False)
    patch_name = {'owner-host':'v2-agent-owner.patch.yml',
                  'group-sandbox':'v2-agent-group-sandbox.patch.yml',
                  'safe-chat':'v2-agent-group.patch.yml'}[profile]
    profile_dir = tmp_path/'dsh/profile'
    profile_dir.mkdir(parents=True)
    (profile_dir.parent/'skills').symlink_to(ROOT/'dsh/skills', target_is_directory=True)
    patch = profile_dir/patch_name
    patch.write_text((ROOT/'dsh/profile'/patch_name).read_text().replace(
        '../plugins/agent-profile/', str(ROOT/'dsh/plugins/agent-profile')+'/') +
        '\n- id: web\n  config:\n    fetchProvider: background-fixture\n'
        '- insert:\n    - id: background-native-probe\n      name: '+
        str(ROOT/'tests/native/background-research-acp-probe.mjs')+'\n')
    journal = JournalStore(tmp_path/'journal.sqlite3')
    store = StateStore(tmp_path/'state.sqlite3')
    conv = Conversation('qq','1','group:3' if group_runtime else 'private:2')
    sent = []
    sequence = 0
    lose_next_settled_callback = False
    killed_process_for_test = False

    def append(text, *, outbound=False):
        nonlocal sequence
        sequence += 1
        raw = {'post_type':'message_sent' if outbound else 'message',
               'message_sent_type':'self' if outbound else None,
               'message_type':'group' if group_runtime else 'private',
               'group_id':3 if group_runtime else None,
               'self_id':1,'user_id':1 if outbound else 2,
               'target_id':2,'message_id':str(sequence),
               'message':[{'type':'text','data':{'text':text}}]}
        journal.append(message_from_onebot('qq',raw))
        return str(sequence)

    class Event:
        def get_sender_id(self): return '2'
    class Messaging:
        event = Event()
        async def send_origin(self, text, mention_user_ids, reply_to_journal_id):
            metadata = {'mention_user_ids':mention_user_ids,'reply_to_journal_id':reply_to_journal_id}
            sent.append((text, metadata)); append(text, outbound=True)

    async def wait_for(predicate, timeout=15):
        async with asyncio.timeout(timeout):
            while not predicate(): await asyncio.sleep(0.02)

    def probe_rows():
        if not log.exists(): return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def fetch_started(tag):
        return any(row.get('kind') == 'fetch-start' and row.get('child') == tag
                   for row in probe_rows())

    async def scenario():
        nonlocal lose_next_settled_callback, killed_process_for_test
        server = McpServer(journal)
        service = None
        async def callback(event):
            nonlocal lose_next_settled_callback
            if event["event"] == "settled" and lose_next_settled_callback:
                lose_next_settled_callback = False
                return False
            return await service.handle_background_event(event)
        server.configure_background(callback)
        await server.start(0)
        client = await asyncio.to_thread(DshClient,
            dsh_bin=ROOT/'.runtime/dsh-runtime/node_modules/.bin/dsh',
            dsh_home=home, workspace=workspace, patch=patch, instructions=instructions,
            provider='background-native-fixture', model='scripted',
            enable_authorization=True, execution_profile=profile,
            group_runtime=group_runtime,
            background_callback_url=server.background_callback_url,
            background_callback_token=server.background_callback_token)
        service = ChatService(journal,store,client,server,trusted_sources=True,
                              domain='owner' if profile == 'owner-host' else 'chat',
                              execution_profile=profile)
        async def send(text): sent.append((text,{})); append(text,outbound=True)
        async def turn(text):
            mid=append(text)
            return await service.handle(conv,mid,send,messaging=Messaging())
        try:
            started = await asyncio.wait_for(turn('BG_START'),20)
            assert 'running' in started, started
            await wait_for(lambda: log.exists() and 'fetch-start' in log.read_text())
            if profile == 'owner-host':
                rows = probe_rows()
                child_tools = next(row['tools'] for row in rows if row.get('kind') == 'child-tools')
                assert {'bash', 'write', 'edit', 'read', 'read_image', 'glob', 'grep'} <= set(child_tools)
                assert any(row.get('kind') == 'file-verified' for row in rows), rows
            assert await asyncio.wait_for(turn('ordinary conversation'),5) == 'FOREGROUND_RESPONSIVE'
            assert 'running' in await asyncio.wait_for(turn('BG_STATUS'),5)
            updated = await asyncio.wait_for(turn('BG_UPDATE'),5)
            assert 'queued' in updated or 'running' in updated, updated
            assert not any('BACKGROUND_COMPLETE' in value for value,_ in sent)
            release.touch()
            await wait_for(lambda: any('BACKGROUND_COMPLETE' in value for value,_ in sent))
            completed=[(text,meta) for text,meta in sent if 'BACKGROUND_COMPLETE' in text]
            assert len(completed)==1, completed
            assert 'ADDITIONAL_REQUIREMENT_APPLIED' in completed[0][0]
            assert completed[0][1]['mention_user_ids']==(['2'] if group_runtime else [])
            assert not service.active_background(conv)

            # A second detached run is stopped even while its foreground is idle.
            release.unlink()
            await turn('BG_START')
            await wait_for(lambda: log.read_text().count('fetch-start')==2)
            assert service.active_background(conv)
            assert await asyncio.wait_for(service.cancel(conv),10)
            await wait_for(lambda: not service.active_background(conv))
            release.touch()
            assert await asyncio.wait_for(turn('conversation after stop'),10) == 'FOREGROUND_RESPONSIVE'
            assert len([text for text,_ in sent if 'BACKGROUND_COMPLETE' in text])==1
            # Reset cancels the previous generation before releasing a clean session.
            release.unlink()
            await turn('BG_START')
            await wait_for(lambda: log.read_text().count('fetch-start')==3)
            before=store.get('qq','1',conv.key)
            command=append('/new')
            after=await asyncio.wait_for(service.reset_conversation(conv,command),10)
            assert before.session_id != after.session_id
            assert not service.active_background(conv)
            release.touch()
            assert await asyncio.wait_for(turn('conversation after reset'),10) == 'FOREGROUND_RESPONSIVE'
            assert len([text for text,_ in sent if 'BACKGROUND_COMPLETE' in text])==1

            # Two native children share one parent Agent but have independent gates and task IDs.
            for tag in 'ABCD': parallel_releases[tag].unlink(missing_ok=True)
            await turn('BG_PAR_START_A')
            await wait_for(lambda: fetch_started('A'))
            await turn('BG_PAR_START_B')
            await wait_for(lambda: fetch_started('B'))
            assert await asyncio.wait_for(turn('ordinary conversation while A and B block'),5) == 'FOREGROUND_RESPONSIVE'
            assert 'running' in await asyncio.wait_for(turn('BG_PAR_STATUS_A'),5)
            assert 'running' in await asyncio.wait_for(turn('BG_PAR_STATUS_B'),5)
            completed_for = lambda tag: [value for value,_ in sent if f'BACKGROUND_COMPLETE_{tag}' in value]
            parallel_before = len([value for value,_ in sent if 'BACKGROUND_COMPLETE_' in value])
            parallel_releases['A'].touch()
            await wait_for(lambda: len(completed_for('A')) == 1)
            assert not completed_for('B'), 'releasing A must not release B'
            assert service.active_background(conv), 'B remains active after A completes'
            assert 'running' in await asyncio.wait_for(turn('BG_PAR_STATUS_B'),5)
            parallel_releases['B'].touch()
            await wait_for(lambda: len(completed_for('B')) == 1)
            assert len([value for value,_ in sent if 'BACKGROUND_COMPLETE_' in value]) == parallel_before + 2
            await asyncio.sleep(.1)
            assert len(completed_for('A')) == len(completed_for('B')) == 1
            assert not service.active_background(conv)

            # Cancelling C while D blocks must leave D alive and able to complete exactly once.
            await turn('BG_PAR_START_C')
            await wait_for(lambda: fetch_started('C'))
            await turn('BG_PAR_START_D')
            await wait_for(lambda: fetch_started('D'))
            assert 'stopping' in await asyncio.wait_for(turn('BG_PAR_CANCEL_C'),5)
            assert service.active_background(conv), 'cancelling C must not cancel D'
            assert 'running' in await asyncio.wait_for(turn('BG_PAR_STATUS_D'),5)
            parallel_releases['D'].touch()
            await wait_for(lambda: len(completed_for('D')) == 1)
            assert not completed_for('C'), 'cancelled C must not deliver a research result'
            await asyncio.sleep(.1)
            assert len(completed_for('D')) == 1
            assert len([value for value,_ in sent if value.startswith('BG_CHILD_C：后台检索已停止。')]) == 1, 'cancellation notice is delivered once'
            assert not service.active_background(conv)

            # Lose the only terminal callback, then recover its long Chinese
            # result over the existing private DSH control socket.
            release.unlink()
            lose_next_settled_callback = True
            keys_before = set(service._background_tasks)
            fetches_before = log.read_text().count('fetch-start')
            fetch_ends_before = log.read_text().count('fetch-end')
            await turn('BG_START_LONG_RESULT')
            await wait_for(lambda: log.read_text().count('fetch-start') == fetches_before + 1)
            new_key = next(key for key in service._background_tasks if key not in keys_before)
            release.touch()
            await wait_for(lambda: log.read_text().count('fetch-end') == fetch_ends_before + 1)
            async def native_terminal():
                return await asyncio.to_thread(client.background_terminal, new_key[0], new_key[1])
            for _ in range(200):
                terminal = await native_terminal()
                if terminal is not None:
                    break
                await asyncio.sleep(.025)
            assert terminal is not None, 'native DSH retained the lost terminal callback'
            assert len(json.dumps(terminal, ensure_ascii=False).encode('utf-8')) > 16 * 1024
            assert len(terminal['text']) == 8000
            status = await asyncio.wait_for(service.status(conv), 10)
            assert status['background_running'] == 0
            assert not status['background_unverifiable']
            await wait_for(lambda: any('中文结论' in text for text, _ in sent))
            recovered = [text for text, _ in sent if '中文结论' in text]
            assert len(recovered) == 1
            assert len(recovered[0]) >= 8000
            assert not service.active_background(conv)

            if profile == 'safe-chat':
                # Prove real transport death using only this isolated test
                # runtime, after native web_fetch starts and before it ends.
                release.unlink()
                keys_before = set(service._background_tasks)
                fetches_before = log.read_text().count('fetch-start')
                await turn('BG_START')
                await wait_for(lambda: log.read_text().count('fetch-start') == fetches_before + 1)
                dead_process_task = next(key for key in service._background_tasks
                                         if key not in keys_before)
                assert service.active_background(conv)
                sends_before_death = len(sent)
                process = client.runtime.client._proc
                assert process is not None and process.poll() is None
                process.kill()
                await asyncio.to_thread(process.wait, 5)
                assert process.poll() is not None
                killed_process_for_test = True

                interrupted = await asyncio.wait_for(service.status(conv), 10)
                assert interrupted['background_running'] == 0
                assert not interrupted['background_unverifiable']
                assert not service.active_background(conv)
                await wait_for(lambda: any(
                    '后台任务因 DSH 运行时退出而中断，未能确认完成。' in text
                    for text, _ in sent[sends_before_death:]))
                terminal_sends = sent[sends_before_death:]
                assert sum('后台任务因 DSH 运行时退出而中断，未能确认完成。' in text
                           for text, _ in terminal_sends) == 1
                assert not any('BACKGROUND_COMPLETE' in text for text, _ in terminal_sends)
                await service.status(conv)
                await asyncio.sleep(.1)
                assert len(sent[sends_before_death:]) == 1
                assert service._background_tasks[dead_process_task].terminal_event['status'] == 'failed'
        finally:
            release.touch()
            lines = list(getattr(client.runtime.client, '_stderr_lines', []))
            (tmp_path/'stderr.txt').write_text('\n'.join(lines))
            await service.close_background()
            try:
                await asyncio.to_thread(client.close)
            except ExceptionGroup as exc:
                expected_transport_error = any(
                    type(error).__name__ == 'TransportClosedError' for error in exc.exceptions)
                if not killed_process_for_test or not expected_transport_error:
                    raise
            await server.close()
    try:
        asyncio.run(scenario())
    finally:
        if group_runtime:
            group_volume.unmount_group(workspace)
