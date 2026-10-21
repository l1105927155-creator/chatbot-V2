"""Attachment reception coordinates with editors without freezing cancellation."""
import asyncio
import fcntl
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("console_async_locks", ROOT / "astrbot-plugins/v2_dsh_router/console_settings.py")
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


def test_waiting_attachment_lock_can_cancel_and_later_acquire(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "lock"
    holder = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    entered = []

    async def receive():
        async with settings.async_workspace_lock(path):
            entered.append(True)

    async def scenario():
        waiting = asyncio.create_task(receive())
        await asyncio.sleep(0.04)
        assert not entered and not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        os.close(holder)
        await asyncio.wait_for(receive(), 1)
        assert entered == [True]

    asyncio.run(scenario())
