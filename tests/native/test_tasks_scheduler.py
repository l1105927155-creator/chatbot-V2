"""Real APScheduler callback, real Journal/state, controlled memory backend."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_scheduler_retires_legacy_daily_cron_and_keeps_workspace_reconciler(tmp_path):
    worker = r'''
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS

repo = Path(os.environ['TASK_REPO'])
root = Path(os.environ['TASK_PROBE'])
sys.path.insert(0, str(repo/'astrbot-plugins'))
import data, data.plugins
data.plugins.__path__ = [str(repo/'astrbot-plugins'), *data.plugins.__path__]
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from data.plugins.v2_journal.journal import JournalStore
from v2_tasks.main import V2Tasks, JOB_ID
from v2_tasks.workspace_manager import RECONCILE_JOB_ID
from v2_tasks.workspace_store import RunStore

async def probe():
    journal = JournalStore(root/'journal.sqlite3')
    router=NS(_initialized=True,_closing=False,allowed_conversations=set(),
              console_store=None,services={},mcp=NS(journal=journal),
              _group_runtimes={},owners=())
    scheduler=AsyncIOScheduler(timezone='UTC')
    scheduler.start()
    async def foreign(): raise AssertionError('foreign job must not be invoked')
    scheduler.add_job(foreign,'date',run_date=datetime.now(timezone.utc)+timedelta(days=2),id='foreign.job')
    scheduler.add_job(foreign,'cron',hour=22,id=JOB_ID)
    context=NS(cron_manager=NS(scheduler=scheduler),
               get_registered_star=lambda _: NS(star_cls=router))
    def plugin():
        instance=object.__new__(V2Tasks)
        instance.context=context
        instance.logger=logging.getLogger('tasks-native-probe')
        instance.store=None
        from v2_tasks.runner import DailyMemoryRunner
        instance.runner=DailyMemoryRunner(None,instance._resolve_binding)
        instance.run_store=RunStore(root/'tasks'/'workspace-runs.sqlite3')
        instance._started=False; instance._stopping=False
        instance._callback_tasks=set(); instance._scheduler=None
        instance.manager=None
        return instance
    first=plugin()
    try:
        assert await first.start()
        assert scheduler.get_job(JOB_ID) is None
        assert scheduler.get_job(RECONCILE_JOB_ID) is not None
        assert not any(job.id.startswith('v2_tasks.workspace.') for job in scheduler.get_jobs())
        await first.terminate()
        second=plugin()
        try:
            assert await second.start()
            assert scheduler.get_job(JOB_ID) is None
            assert scheduler.get_job(RECONCILE_JOB_ID) is not None
            assert scheduler.get_job('foreign.job') is not None
        finally:
            await second.terminate()
    finally:
        await first.terminate()
        scheduler.shutdown(wait=False)
print('native scheduler removes retired daily cron and retains workspace reconciliation: passed')
asyncio.run(probe())
'''
    result = subprocess.run(
        [str(ROOT / ".runtime/astrbot/.venv/bin/python"), "-c", worker],
        check=False,
        cwd=ROOT / ".runtime/astrbot",
        env={**os.environ, "ASTRBOT_ROOT": str(tmp_path / "astrbot-data"),
             "TASK_REPO": str(ROOT), "TASK_PROBE": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
