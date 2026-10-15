"""Check simulated forward payloads with the pinned AstrBot payload parser."""
import os
from pathlib import Path
import subprocess
import textwrap


def test_forward_reply_payload_understood_by_pinned_astrbot(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent('''
        import asyncio, os, sys
        from pathlib import Path
        sys.path.insert(0, str(Path(os.environ['V2_REPO_ROOT']) / '.runtime/astrbot'))
        from sandbox.server import SandboxApp
        from astrbot.core.utils.quoted_message.chain_parser import OneBotPayloadParser

        async def main():
            app = SandboxApp(Path(os.environ['ASTRBOT_ROOT']) / 'onebot', 'ws://127.0.0.1:6220/ws')
            try:
                # AstrBot's Node.to_dict format on the actual outbound API.
                response, event = await app.handle_action('send_group_forward_msg', {
                    'group_id': 91000,
                    'messages': [{'type': 'node', 'data': {
                        'user_id': '90002', 'nickname': 'Bob',
                        'content': [{'type': 'text', 'data': {'text': 'preserved forward content'}}],
                    }}],
                })
                assert event['message'][0]['type'] == 'forward'
                forward_id = event['message'][0]['data']['id']
                payload, _ = await app.handle_action('get_forward_msg', {'id': forward_id})
                parsed = OneBotPayloadParser().parse_get_forward_payload(payload)
                assert 'Bob' in parsed['text'] and 'preserved forward content' in parsed['text'], parsed
                # A persisted payload remains available beyond the latest UI event window.
                app.store.close()
                from sandbox.model import SandboxStore
                app.store = SandboxStore(Path(os.environ['ASTRBOT_ROOT']) / 'onebot')
                payload2, _ = await app.handle_action('get_forward_msg', {'id': forward_id})
                assert OneBotPayloadParser().parse_get_forward_payload(payload2) == parsed
            finally:
                app.store.close()
        asyncio.run(main())
    ''')
    env = {**os.environ, "ASTRBOT_ROOT": str(tmp_path), "V2_REPO_ROOT": str(root)}
    completed = subprocess.run([str(root / '.runtime/astrbot/.venv/bin/python'), '-c', script],
                               cwd=root, env=env, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
