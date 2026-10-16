"""Health stays operational: it must not create Sessions or prompt ACP."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT = ROOT / ".runtime/astrbot"
sys.path.insert(0, str(ASTRBOT))

_previous_astrbot_root = os.environ.get("ASTRBOT_ROOT")
with tempfile.TemporaryDirectory(prefix="agent-health-astrbot-") as _isolated_astrbot_root:
    os.environ["ASTRBOT_ROOT"] = _isolated_astrbot_root
    try:
        from data.plugins.v2_dsh_router.main import V2DshRouter  # noqa: E402
        from data.plugins.v2_dsh_router.dsh_client import AcpRuntime, DshClient  # noqa: E402
    finally:
        if _previous_astrbot_root is None:
            os.environ.pop("ASTRBOT_ROOT", None)
        else:
            os.environ["ASTRBOT_ROOT"] = _previous_astrbot_root


class FakeClient:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.health_calls = 0
        self.prompt_calls = 0
        self.session_creations = 0
        self.settings = {"workspace": "/existing/bounded/workspace"}

    def health(self) -> dict:
        self.health_calls += 1
        return {"ready": self.ready, "quarantined_sessions": 0}

    def prompt(self, *_args, **_kwargs):
        self.prompt_calls += 1
        raise AssertionError("health must not prompt")

    def create_session(self):
        self.session_creations += 1
        raise AssertionError("health must not create a Session")


class FakeService:
    _queued = 0

    def __init__(self, ready: bool = True) -> None:
        self.state = SimpleNamespace(health_counts=lambda: {"pending": 0} if ready else
                                     (_ for _ in ()).throw(RuntimeError("state unavailable")))


class FakePlatform:
    def __init__(self, platform_id="qq", bot_id="42", api=True, events=True):
        self._meta = SimpleNamespace(name="aiocqhttp", id=platform_id)
        self._client = SimpleNamespace(
            _wsr_api_clients={bot_id: object()} if api else {},
            _wsr_event_clients={bot_id} if events else set(),
        )

    def meta(self):
        return self._meta

    def get_client(self):
        return self._client


class FakeJournal:
    def __init__(self, ready=True):
        self.ready = ready

    def check_health(self):
        return self.ready


def make_router(*, connected=True, api=True, events=True, closing=False,
                owner_host_ready=True, groups_ready=True, acp_ready=True, journal_ready=True,
                state_ready=True):
    router = V2DshRouter.__new__(V2DshRouter)
    router._initialized = True
    router._closing = closing
    router._active = set()
    router.allowed_conversations = frozenset({"group:123", "private:7"})
    router.owners = [SimpleNamespace(platform_id="qq", bot_id="42", user_id="7")]
    router.clients = {"chat": FakeClient(acp_ready), "owner": FakeClient(acp_ready and owner_host_ready),
                      "group:test": FakeClient(acp_ready)}
    router.services = {"chat": FakeService(state_ready), "owner": FakeService(state_ready),
                       "group:test": FakeService(state_ready)}
    router.mcp = SimpleNamespace(journal=FakeJournal(journal_ready))
    router.audit = SimpleNamespace(write_errors=0)
    verify = ((lambda _path: os.open(os.devnull, os.O_RDONLY)) if groups_ready else
              (lambda _path: (_ for _ in ()).throw(RuntimeError("group volume unavailable"))))
    router._group_runtimes = {"group:test": {"workspace": Path("/existing/group"),
        "volume_module": SimpleNamespace(verified_group_workspace=verify),
        "platform_id": "qq", "bot_id": "42", "group_id": "123"}}
    platforms = [FakePlatform(api=api, events=events)] if connected else []
    router.context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: platforms))
    return router


def test_core_ready_while_onebot_is_disconnected_without_prompt_or_session() -> None:
    router = make_router(connected=False)
    result = asyncio.run(router.health())
    assert result["core_ready"] is True
    assert result["ready"] is False
    assert result["onebot_connected"] is False
    assert result["acp_ready"] and result["journal_ready"] and result["owner_host_ready"] and result["group_sandboxes_ready"]
    assert sum(client.health_calls for client in router.clients.values()) >= 3
    assert all(client.prompt_calls == client.session_creations == 0 for client in router.clients.values())


def test_ready_requires_matching_onebot_api_and_event_clients() -> None:
    assert asyncio.run(make_router(api=True, events=True).health())["ready"] is True
    assert asyncio.run(make_router(api=False, events=True).health())["ready"] is False
    assert asyncio.run(make_router(api=True, events=False).health())["ready"] is False


@pytest.mark.parametrize("broken,flag", [
    ({"closing": True}, "closing"),
    ({"owner_host_ready": False}, "owner_host_ready"),
    ({"groups_ready": False}, "group_sandboxes_ready"),
    ({"acp_ready": False}, "acp_ready"),
    ({"journal_ready": False}, "journal_ready"),
    ({"state_ready": False}, "state_ready"),
])
def test_core_fails_closed_for_runtime_dependencies(broken, flag) -> None:
    result = asyncio.run(make_router(**broken).health())
    assert result[flag] is (broken.get("closing") is True if flag == "closing" else False)
    assert result["core_ready"] is False


class FakeProcess:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class FakeReader:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


@pytest.mark.parametrize("process,reader,expected", [
    (FakeProcess(), FakeReader(), True),
    (None, FakeReader(), False),
    (FakeProcess(1), FakeReader(), False),
    (FakeProcess(), None, False),
    (FakeProcess(), FakeReader(False), False),
])
def test_pinned_acp_transport_health_checks_process_and_reader(process, reader, expected) -> None:
    # The pinned HarnessClient exposes these private fields; the adapter uses
    # both because a live child without its response reader cannot serve ACP.
    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime.client = SimpleNamespace(_proc=process, _reader_thread=reader)
    assert runtime.transport_alive() is expected


def test_dsh_client_health_reports_closed_or_dead_transport() -> None:
    client = DshClient.__new__(DshClient)
    client._lock = threading.RLock()
    client._closed = False
    client._runtime_stopped = False
    client._quarantined_sessions = {}
    client.runtime = SimpleNamespace(transport_alive=lambda: True)
    assert client.health() == {"ready": True, "quarantined_sessions": 0}
    client.runtime.transport_alive = lambda: False
    assert client.health()["ready"] is False
    client._closed = True
    client.runtime.transport_alive = lambda: True
    assert client.health()["ready"] is False
