"""Binary attachment round trips and file-domain boundaries for FileTransfer."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = load("v2_file_transfer_journal", "astrbot-plugins/v2_journal/journal.py")
transfer_module = load("v2_file_transfer_contract", "astrbot-plugins/v2_dsh_router/file_transfer.py")
FileTransfer = transfer_module.FileTransfer


def upload(store, conversation, message_id: str, *, file_id="resource-1", file_name="payload.bin",
           file_size=None, kind="file", direction="inbound"):
    group = conversation.key.startswith("group:")
    outbound = direction == "outbound"
    peer = conversation.key.split(":", 1)[1]
    raw = {
        "post_type": "message_sent" if outbound else "message",
        "message_sent_type": "self" if outbound else None,
        "message_type": "group" if group else "private",
        "self_id": int(conversation.bot_id),
        "user_id": int(conversation.bot_id) if outbound else 20,
        "group_id": int(peer) if group else None,
        "target_id": int(peer) if not group and outbound else None,
        "message_id": message_id,
        "message": [{"type": kind, "data": {
            "file_id": file_id, "file_name": file_name,
            **({} if file_size is None else {"file_size": file_size}),
        }}],
    }
    return store.append(journal.message_from_onebot(conversation.platform_id, raw))[0]


def make_transfer(store, conversation, workspace, *, profile="group-sandbox", resolve=None,
                  send=None, **kwargs):
    async def default_resolve(_segment):
        raise AssertionError("test must provide a resource resolver")

    async def default_send(_path, _name):
        raise AssertionError("test must provide a sender")

    return FileTransfer(store, conversation, workspace, profile=profile,
                        resolve=resolve or default_resolve, send=send or default_send, **kwargs)


async def start_http(body: bytes, *, disconnect=False):
    app = web.Application()

    async def handle(_request):
        if not disconnect:
            return web.Response(body=body, headers={"Content-Type": "application/octet-stream"})
        response = web.StreamResponse(status=200)
        await response.prepare(_request)
        await response.write(body[: max(1, len(body) // 2)])
        _request.transport.close()
        return response

    app.router.add_get("/attachment", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/attachment"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,payload", [
    ("archive.zip", b"PK\x03\x04" + bytes(range(256)) * 8),
    ("document.pdf", b"%PDF-1.7\x00\xff\x80binary-pdf-content"),
    ("picture.png", b"\x89PNG\r\n\x1a\n\x00\xff\x80" + bytes(range(64))),
    ("arbitrary.bin", bytes(range(256)) + b"\x00\xff\x80"),
])
async def test_receive_roundtrips_binary_bytes_from_loopback_url(tmp_path, name, payload):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    row = upload(store, conversation, "incoming-1", file_name=name, file_size=len(payload))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner, url = await start_http(payload)

    async def resolve(segment):
        assert segment["type"] == "file"
        return {"url": url, "file_name": name, "file_size": len(payload)}

    transfer = make_transfer(store, conversation, workspace, resolve=resolve)
    try:
        result = await transfer.receive(row.journal_id, 0)
        saved = workspace / result["path"].removeprefix("/workspace/")
        assert saved.read_bytes() == payload
        assert result["name"] == name
        assert result["size"] == len(payload)
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        assert result["path"].startswith("/workspace/attachments/")
        assert not list(workspace.rglob(".partial"))
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_receive_download_disconnect_and_streaming_limit_remove_partial_files(tmp_path):
    payload = b"binary" * 100
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    row = upload(store, conversation, "incoming", file_size=None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner, url = await start_http(payload, disconnect=True)
    transfer = make_transfer(store, conversation, workspace,
        resolve=lambda _segment: asyncio.sleep(0, result={"url": url, "file_name": "broken.bin"}),
        max_bytes=1024)
    try:
        with pytest.raises((aiohttp.ClientError, ConnectionError, OSError)):
            await transfer.receive(row.journal_id, 0)
        assert not list(workspace.rglob(".partial"))
        assert not list((workspace / "attachments").iterdir())
    finally:
        await runner.cleanup()

    runner, url = await start_http(payload)
    limited = make_transfer(store, conversation, workspace,
        resolve=lambda _segment: asyncio.sleep(0, result={"url": url, "file_name": "too-large.bin"}),
        max_bytes=100)
    try:
        with pytest.raises(ValueError, match="size limit"):
            await limited.receive(row.journal_id, 0)
        assert not list(workspace.rglob(".partial"))
        assert not list((workspace / "attachments").iterdir())
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_receive_rejects_cross_conversation_journal_id_and_wrong_direction(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    origin = journal.Conversation("qq", "10", "group:30")
    other = journal.Conversation("qq", "10", "group:31")
    foreign = upload(store, other, "foreign")
    outbound = upload(store, origin, "outgoing", direction="outbound")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    async def resolve(segment):
        calls.append(segment)
        return {"file": str(tmp_path / "not-used"), "file_name": "x.bin"}

    transfer = make_transfer(store, origin, workspace, resolve=resolve)
    with pytest.raises(PermissionError, match="current conversation"):
        await transfer.receive(foreign.journal_id, 0)
    with pytest.raises(ValueError, match="inbound"):
        await transfer.receive(outbound.journal_id, 0)
    assert calls == []


@pytest.mark.asyncio
async def test_receive_rejects_attachment_before_current_session_history_floor(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "private:20")
    old = upload(store, conversation, "old")
    reset = upload(store, conversation, "reset")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    async def resolve(segment):
        calls.append(segment)
        return {"file": str(tmp_path / "not-used"), "file_name": "x.bin"}

    transfer = make_transfer(store, conversation, workspace, resolve=resolve)
    transfer.history_after = reset.journal_id
    with pytest.raises(PermissionError, match="predates"):
        await transfer.receive(old.journal_id, 0)
    assert calls == []


@pytest.mark.asyncio
async def test_receive_group_upload_notice_after_journal_normalization(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    payload = b"notice-only-upload\x00\xff\x80"
    source = tmp_path / "napcat-group-upload.bin"
    source.write_bytes(payload)
    notice = {
        "post_type": "notice", "notice_type": "group_upload", "self_id": 10,
        "group_id": 30, "user_id": 20, "time": 1_792_326_400,
        "file": {"id": "opaque-upload-id", "busid": 102,
                 "name": "notice-upload.bin", "file_size": len(payload)},
    }
    normalized = journal.message_from_group_upload("qq", notice, observed_at_ns=123)
    inserted, created = store.append(normalized)
    assert created
    persisted = store.before(conversation, inserted.journal_id + 1, 1)[0]
    assert persisted.message.content["event_type"] == "group_upload"
    assert persisted.message.content["message_id_is_synthetic"] is True
    assert persisted.message.message_id.startswith("notice:group_upload:")
    assert persisted.message.content["segments"] == [{
        "type": "file", "id": "opaque-upload-id", "busid": 102,
        "name": "notice-upload.bin", "file_size": len(payload),
        "file_id": "opaque-upload-id",
    }]

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    resolved = []

    async def resolve(segment):
        resolved.append(dict(segment))
        return {"file": str(source), "file_name": segment["name"],
                "file_size": segment["file_size"]}

    transfer = make_transfer(store, conversation, workspace, resolve=resolve)
    result = await transfer.receive(persisted.journal_id, 0)
    saved = workspace / result["path"].removeprefix("/workspace/")
    assert saved.read_bytes() == payload
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert resolved == [persisted.message.content["segments"][0]]
    final = store.before(conversation, persisted.journal_id + 1, 1)[0]
    assert final.message.content["event_type"] == "group_upload"
    assert final.message.content["message_id_is_synthetic"] is True


@pytest.mark.asyncio
async def test_receive_local_napcat_binary_file_and_sanitized_name(tmp_path):
    payload = b"\x00\xff\x80\x89PNG"
    source = tmp_path / "napcat-cache"
    source.write_bytes(payload)
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "private:20")
    row = upload(store, conversation, "incoming", file_name="../../evil.png", file_size=len(payload))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    transfer = make_transfer(store, conversation, workspace, profile="safe-chat",
        resolve=lambda _segment: asyncio.sleep(0, result={"file": str(source), "file_name": "../../evil.png",
                                                             "file_size": len(payload)}))
    result = await transfer.receive(row.journal_id, 0)
    assert result["name"] == "evil.png"
    assert result["path"] == f"attachments/{Path(result['path']).parts[-2]}/evil.png"
    assert (workspace / result["path"]).read_bytes() == payload


@pytest.mark.asyncio
async def test_safe_chat_accumulated_quota_and_two_instances_share_conversation_limit(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "private:20")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    first_source = tmp_path / "first.dat"
    second_source = tmp_path / "second.dat"
    first_source.write_bytes(b"A" * 60)
    second_source.write_bytes(b"B" * 50)
    rows = [
        upload(store, conversation, "quota-first", file_id="one", file_size=60),
        upload(store, conversation, "quota-second", file_id="two", file_size=50),
    ]

    async def resolve(segment):
        return {"file": str(first_source if segment["file_id"] == "one" else second_source),
                "file_name": segment["file_id"] + ".bin"}

    transfer = make_transfer(store, conversation, workspace, profile="safe-chat", resolve=resolve)
    transfer.conversation_quota = 100
    transfer.host_reserve_bytes = 0
    await transfer.receive(rows[0].journal_id, 0)
    with pytest.raises(ValueError, match="conversation attachment storage quota"):
        await transfer.receive(rows[1].journal_id, 0)
    assert transfer._stored_bytes() == 60
    assert len([path for path in (workspace / "attachments").glob("*/*") if path.is_file()]) == 1
    assert not list(workspace.rglob(".partial"))

    # Distinct service objects for one conversation still use the same
    # event-loop storage lock; without it both can measure 0 and save 60 bytes.
    concurrent_workspace = tmp_path / "concurrent-workspace"
    concurrent_workspace.mkdir(mode=0o700)
    concurrent_workspace.chmod(0o700)
    payload_path = tmp_path / "concurrent.bin"
    payload_path.write_bytes(b"C" * 60)
    concurrent_rows = [
        upload(store, conversation, "parallel-a", file_id="a", file_size=60),
        upload(store, conversation, "parallel-b", file_id="b", file_size=60),
    ]

    async def resolve_concurrent(_segment):
        return {"file": str(payload_path), "file_name": "concurrent.bin", "file_size": 60}

    transfers = [make_transfer(store, conversation, concurrent_workspace, profile="safe-chat",
                               resolve=resolve_concurrent) for _ in range(2)]
    for item in transfers:
        item.conversation_quota = 100
        item.host_reserve_bytes = 0
    outcomes = await asyncio.gather(*(item.receive(row.journal_id, 0)
                                      for item, row in zip(transfers, concurrent_rows)),
                                    return_exceptions=True)
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1, outcomes
    assert sum(isinstance(value, ValueError) and "conversation attachment storage quota" in str(value)
               for value in outcomes) == 1, outcomes
    assert transfers[0]._stored_bytes() == 60
    assert not list(concurrent_workspace.rglob(".partial"))


@pytest.mark.asyncio
async def test_host_storage_reserve_rejects_safe_chat_and_cleans_partial_but_not_group_volume(tmp_path):
    payload = b"reserve-check" * 8
    runner, url = await start_http(payload)
    try:
        safe_store = journal.JournalStore(tmp_path / "safe.sqlite3")
        safe_conversation = journal.Conversation("qq", "10", "private:20")
        safe_row = upload(safe_store, safe_conversation, "safe-reserve", file_size=None)
        safe_workspace = tmp_path / "safe-workspace"
        safe_workspace.mkdir(mode=0o700)
        safe_workspace.chmod(0o700)
        safe = make_transfer(safe_store, safe_conversation, safe_workspace, profile="safe-chat",
            resolve=lambda _segment: asyncio.sleep(0, result={"url": url, "file_name": "reserve.bin"}))
        safe.host_reserve_bytes = 10**30
        with pytest.raises(ValueError, match="host storage reserve"):
            await safe.receive(safe_row.journal_id, 0)
        assert not list(safe_workspace.rglob(".partial"))
        assert not list((safe_workspace / "attachments").glob("*"))

        group_store = journal.JournalStore(tmp_path / "group.sqlite3")
        group_conversation = journal.Conversation("qq", "10", "group:30")
        group_row = upload(group_store, group_conversation, "group-reserve", file_size=None)
        group_workspace = tmp_path / "group-workspace"
        group_workspace.mkdir(mode=0o700)
        group = make_transfer(group_store, group_conversation, group_workspace,
            resolve=lambda _segment: asyncio.sleep(0, result={"url": url, "file_name": "group.bin"}))
        group.host_reserve_bytes = 10**30
        result = await group.receive(group_row.journal_id, 0)
        assert result["size"] == len(payload)
        assert (group_workspace / result["path"].removeprefix("/workspace/")).read_bytes() == payload
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_receive_and_send_copy_share_one_event_loop_storage_lock(tmp_path, monkeypatch):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    outgoing = workspace / "outgoing.bin"
    incoming = tmp_path / "incoming.bin"
    payload = b"Z" * (transfer_module.CHUNK * 3 + 17)
    outgoing.write_bytes(payload)
    incoming.write_bytes(payload)
    row = upload(store, conversation, "concurrent-transfer", file_size=len(payload))
    order = []
    original_write = FileTransfer._write

    def record_write(self, fd, chunk, state):
        order.append("send" if state.get("host_storage") else "receive")
        return original_write(self, fd, chunk, state)

    monkeypatch.setattr(FileTransfer, "_write", record_write)

    async def resolve(_segment):
        return {"file": str(incoming), "file_name": "incoming.bin", "file_size": len(payload)}

    async def send(_path, _name):
        return None

    receiver = make_transfer(store, conversation, workspace, resolve=resolve)
    sender = make_transfer(store, conversation, workspace, send=send, spool_dir=tmp_path / "spool")
    sender.host_reserve_bytes = 0
    await asyncio.gather(receiver.receive(row.journal_id, 0), sender.send_file("outgoing.bin"))
    assert order.count("send") == 4 and order.count("receive") == 4, order
    transitions = sum(left != right for left, right in zip(order, order[1:]))
    assert transitions == 1, f"copy operations interleaved despite shared storage lock: {order}"


@pytest.mark.asyncio
async def test_resolve_onebot_file_uses_each_media_id_and_rejects_path_or_url_without_api_calls():
    calls = []

    class Bot:
        async def call_action(self, action, **kwargs):
            calls.append((action, kwargs))
            return {"file": "resolved:" + str(kwargs.get("file_id", kwargs.get("file"))),
                    "file_name": str(kwargs.get("file_id", kwargs.get("file")))}

    bot = Bot()
    conversation = journal.Conversation("qq", "10", "group:30")
    first = await transfer_module.resolve_onebot_file(bot, conversation,
        {"type": "file", "file_id": "file-A", "busid": 102})
    second = await transfer_module.resolve_onebot_file(bot, conversation,
        {"type": "file", "file_id": "file-B", "busid": 103})
    image = await transfer_module.resolve_onebot_file(bot, conversation,
        {"type": "image", "file_id": "image-C"})
    record = await transfer_module.resolve_onebot_file(bot, conversation,
        {"type": "record", "file_id": "record-D"})
    video = await transfer_module.resolve_onebot_file(bot, conversation,
        {"type": "video", "file_id": "video-E"})
    assert first["file"] == "resolved:file-A" and second["file"] == "resolved:file-B"
    assert calls == [
        ("get_group_file_url", {"file_id": "file-A", "self_id": 10, "group_id": 30, "busid": 102}),
        ("get_group_file_url", {"file_id": "file-B", "self_id": 10, "group_id": 30, "busid": 103}),
        ("get_image", {"file": "image-C", "self_id": 10}),
        ("get_file", {"file_id": "record-D", "self_id": 10}),
        ("get_file", {"file_id": "video-E", "self_id": 10}),
    ]
    assert [image["file_name"], record["file_name"], video["file_name"]] == [
        "image-C", "record-D", "video-E",
    ]

    before = len(calls)
    for kind, identity in (("file", "../secret"), ("image", "../secret"),
                           ("record", "https://attacker.invalid/file"),
                           ("video", "http://attacker.invalid/file")):
        with pytest.raises((ValueError, PermissionError)):
            await transfer_module.resolve_onebot_file(bot, conversation,
                {"type": kind, "file_id": identity})
    assert len(calls) == before, "invalid path and URL-shaped references never reach OneBot"


@pytest.mark.asyncio
async def test_send_group_files_reject_traversal_symlink_hardlink_and_fifo(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    regular = workspace / "regular.bin"
    regular.write_bytes(b"binary")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (workspace / "linked.bin").symlink_to(outside)
    fifo = workspace / "fifo"
    os.mkfifo(fifo)
    sent = []

    async def send(path, name):
        sent.append((Path(path).read_bytes(), name))

    transfer = make_transfer(store, conversation, workspace, send=send, spool_dir=tmp_path / "spool")
    result = await transfer.send_file("/workspace/regular.bin")
    assert result["sha256"] == hashlib.sha256(b"binary").hexdigest()
    assert sent == [(b"binary", "regular.bin")]
    os.link(regular, workspace / "hardlinked.bin")
    for unsafe in ("../outside.bin", str(outside), "linked.bin", "hardlinked.bin", "fifo"):
        with pytest.raises((OSError, ValueError)):
            await transfer.send_file(unsafe)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_owner_can_send_authorized_host_file_and_safe_chat_only_attachments(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    owner_conversation = journal.Conversation("qq", "10", "private:20")
    owner_workspace = tmp_path / "owner-workspace"
    owner_workspace.mkdir()
    host_file = tmp_path / "authorized-host-file.pdf"
    host_file.write_bytes(b"%PDF\x00\xff")
    observed = []

    async def send(path, name):
        observed.append((Path(path).read_bytes(), name))

    owner = make_transfer(store, owner_conversation, owner_workspace,
        profile="owner-host", send=send, spool_dir=tmp_path / "owner-spool")
    assert owner.max_bytes == 2**63 - 1, "Owner keeps the host file limit instead of a 1 GiB default"
    result = await owner.send_file(str(host_file))
    assert result["sha256"] == hashlib.sha256(host_file.read_bytes()).hexdigest()
    assert observed == [(host_file.read_bytes(), host_file.name)]

    safe = make_transfer(store, owner_conversation, owner_workspace,
        profile="safe-chat", send=send, spool_dir=tmp_path / "safe-spool")
    with pytest.raises(PermissionError, match="received attachments"):
        await safe.send_file("ordinary.txt")
    attachment = owner_workspace / "attachments" / "received.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"received")
    result = await safe.send_file("attachments/received.bin")
    assert result["size"] == len(b"received")
    assert observed[-1] == (b"received", "received.bin")


@pytest.mark.asyncio
async def test_send_fails_once_and_always_cleans_snapshot(tmp_path):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.zip"
    source.write_bytes(b"PK\x03\x04" + b"contents")
    spool = tmp_path / "spool"
    attempts = []

    async def fail(path, name):
        attempts.append((Path(path).read_bytes(), name))
        raise OSError("OneBot rejected file")

    transfer = make_transfer(store, conversation, workspace, send=fail, spool_dir=spool)
    with pytest.raises(OSError, match="rejected"):
        await transfer.send_file("report.zip")
    assert len(attempts) == 1
    assert attempts[0] == (source.read_bytes(), "report.zip")
    assert list(spool.iterdir()) == []


@pytest.mark.asyncio
async def test_send_rejects_source_changed_while_snapshot_is_copied(tmp_path, monkeypatch):
    store = journal.JournalStore(tmp_path / "journal.sqlite3")
    conversation = journal.Conversation("qq", "10", "group:30")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "large.bin"
    source.write_bytes(b"a" * (transfer_module.CHUNK * 2))
    spool = tmp_path / "spool"
    sends = []
    real_read = os.read
    source_inode = source.stat().st_ino
    changed = False

    def mutate_during_source_read(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if not changed and os.fstat(fd).st_ino == source_inode:
            changed = True
            with source.open("ab") as handle:
                handle.write(b"concurrent-change")
                handle.flush()
                os.fsync(handle.fileno())
        return chunk

    monkeypatch.setattr(transfer_module.os, "read", mutate_during_source_read)

    async def send(path, name):
        sends.append((Path(path), name))

    transfer = make_transfer(store, conversation, workspace, send=send, spool_dir=spool)
    with pytest.raises(ValueError, match="file changed"):
        await transfer.send_file("large.bin")
    assert changed
    assert sends == []
    assert list(spool.iterdir()) == []
