"""Native QQ image projection into ACP, using loopback-only resources."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "v2_qq_vision_contract", ROOT / "astrbot-plugins/v2_dsh_router/vision.py"
)
vision_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = vision_module
spec.loader.exec_module(vision_module)
QQVision = vision_module.QQVision


@dataclass
class Message:
    content: dict
    direction: str = "inbound"
    platform_id: str | None = None
    bot_id: str | None = None
    conversation_key: str | None = None


@dataclass
class Row:
    journal_id: int
    message: Message


def image_bytes(fmt="PNG", size=(3, 2)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(buffer, format=fmt)
    return buffer.getvalue()


def row(journal_id, *segments, direction="inbound", identity=None):
    identity = identity or (None, None, None)
    return Row(journal_id, Message({"segments": list(segments)}, direction, *identity))


def image_segment(name="opaque.bin", **extra):
    return {"type": "image", "file": "qq-resource", "file_name": name, **extra}


async def serve(routes):
    app = web.Application()

    async def handler(request):
        item = routes[request.path]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item[0] == "delay":
            await asyncio.sleep(item[1])
            item = item[2]
        if isinstance(item, tuple) and item[0] == "status":
            return web.Response(status=item[1])
        return web.Response(body=item, content_type="application/octet-stream")

    app.router.add_get("/{name}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_multiple_images_magic_detection_and_bytes_preserved_despite_metadata(tmp_path):
    png, jpeg, gif, webp = (image_bytes(fmt) for fmt in ("PNG", "JPEG", "GIF", "WEBP"))
    runner, base = await serve({f"/{i}": body for i, body in enumerate((png, jpeg, gif, webp))})
    resolved = []

    async def resolve(segment):
        resolved.append(segment)
        index = segment["resource_index"]
        return {"url": f"{base}/{index}", "file_name": "fake.txt", "mime_type": "text/plain"}

    rows = [row(11, image_segment("not-an-image.png", resource_index=0),
                {"type": "text", "text": "between"}, image_segment("wrong.jpg", resource_index=1)),
            row(12, image_segment("foo.gif", resource_index=2), image_segment("bar.webp", resource_index=3),
                direction="outbound")]
    try:
        result = await QQVision(resolve).prepare(rows, {11, 12})
        assert [x["mimeType"] for x in result["images"]] == ["image/png", "image/jpeg", "image/gif", "image/webp"]
        assert [base64.b64decode(x["data"]) for x in result["images"]] == [png, jpeg, gif, webp]
        assert [(x["journal_id"], x["segment_index"]) for x in result["references"]] == [(11, 0), (11, 2), (12, 0), (12, 1)]
        assert result["references"][-1]["direction"] == "outbound"
        assert all("url" not in ref and "path" not in ref for ref in result["references"])
        assert not result["errors"] and len(resolved) == 4
        assert list(tmp_path.iterdir()) == []
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_only_visible_canonical_image_rows_are_resolved():
    calls = []

    async def resolve(segment):
        calls.append(segment)
        return {"file": "/not-opened"}

    rows = [row(1, image_segment()), row(2, {"type": "text", "text": "image"}), row(3, image_segment())]
    result = await QQVision(resolve).prepare(rows, {2, 3})
    assert calls == [rows[2].message.content["segments"][0]]
    assert [error["reason"] for error in result["errors"]] == ["resource_unavailable"]
    assert result["references"] == []


@pytest.mark.asyncio
async def test_invalid_expired_unknown_and_oversized_images_are_safe_errors():
    good = image_bytes()
    corrupt = b"\x89PNG\r\n\x1a\n" + b"not-a-valid-png"
    runner, base = await serve({"/expired": ("status", 410), "/unknown": b"not image",
                               "/corrupt": corrupt, "/good": good, "/big": good})
    by_id = {1: "expired", 2: "unknown", 3: "corrupt", 4: "good", 5: "big"}

    async def resolve(segment):
        return {"url": f"{base}/{by_id[segment['resource_index']]}",
                "file_name": "secret-signed-name.png", "url_hint": "https://signed.example/private?token=SECRET"}

    rows = [row(i, image_segment(resource_index=i)) for i in range(1, 6)]
    result = await QQVision(resolve, max_image_bytes=len(good) + 2,
                            max_batch_bytes=len(good) + len(corrupt) + len(b'not image') + 1).prepare(rows, {1, 2, 3, 4, 5})
    try:
        assert [err["reason"] for err in result["errors"]] == [
            "resource_unavailable", "unsupported_image_format", "invalid_image_data", "batch_size_limit"
        ]
        assert len(result["images"]) == 1
        assert base64.b64decode(result["images"][0]["data"]) == good
        serialized = repr(result)
        assert "SECRET" not in serialized and "signed.example" not in serialized
        assert "/private" not in serialized
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_individual_failure_does_not_block_later_images():
    good = image_bytes("JPEG")
    runner, base = await serve({"/fail": ("status", 403), "/good": good})

    async def resolve(segment):
        return {"url": f"{base}/{segment['resource_index']}"}

    result = await QQVision(resolve).prepare(
        [row(1, image_segment(resource_index="fail")), row(2, image_segment(resource_index="good"))], {1, 2})
    try:
        assert [err["journal_id"] for err in result["errors"]] == [1]
        assert len(result["images"]) == 1 and result["images"][0]["mimeType"] == "image/jpeg"
        assert result["references"][0]["journal_id"] == 2
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_latest_twenty_selected_and_older_images_explicitly_omitted():
    good = image_bytes()
    runner, base = await serve({f"/{i}": good for i in range(25)})
    calls = []

    async def resolve(segment):
        calls.append(segment["resource_index"])
        return {"url": f"{base}/{segment['resource_index']}"}

    rows = [row(i, image_segment(resource_index=i)) for i in range(25)]
    try:
        result = await QQVision(resolve).prepare(rows, set(range(25)))
        assert calls == list(range(5, 25))
        assert [ref["journal_id"] for ref in result["omitted"]] == list(range(5))
        assert len(result["images"]) == len(result["references"]) == 20
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_batch_deadline_bounds_slow_resource_and_reports_unprocessed_images():
    good = image_bytes()
    runner, base = await serve({"/slow": ("delay", 1.0, good), "/later": good})

    async def resolve(segment):
        return {"url": f"{base}/{segment['resource_index']}"}

    started = time.monotonic()
    try:
        result = await QQVision(resolve, timeout_seconds=0.05).prepare(
            [row(1, image_segment(resource_index="slow")), row(2, image_segment(resource_index="later"))], {1, 2})
        assert time.monotonic() - started < 0.3
        assert [error["reason"] for error in result["errors"]] == ["batch_timeout", "batch_timeout"]
        assert not result["images"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_local_resolved_file_is_read_ephemerally_and_never_persisted(tmp_path):
    source = tmp_path / "resolved-cache.png"
    expected = image_bytes("PNG")
    source.write_bytes(expected)
    before = {path.name for path in tmp_path.iterdir()}

    async def resolve(_segment):
        return {"file": str(source), "file_name": "cache.png"}

    result = await QQVision(resolve).prepare([row(1, image_segment())], {1})
    assert base64.b64decode(result["images"][0]["data"]) == expected
    assert {path.name for path in tmp_path.iterdir()} == before
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
async def test_single_image_limit_and_pixel_limits_reject_without_aborting_other_images():
    valid = image_bytes()
    huge = image_bytes(size=(8193, 1))
    runner, base = await serve({"/huge": huge, "/valid": valid, "/large": valid + bytes(200_000)})

    async def resolve(segment):
        return {"url": f"{base}/{segment['resource_index']}"}

    rows = [row(1, image_segment(resource_index="large")), row(2, image_segment(resource_index="huge")),
            row(3, image_segment(resource_index="valid"))]
    try:
        result = await QQVision(resolve, max_image_bytes=100_000).prepare(rows, {1, 2, 3})
        assert [error["reason"] for error in result["errors"]] == ["image_size_limit", "invalid_image_data"]
        assert len(result["images"]) == 1 and result["references"][0]["journal_id"] == 3
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_private_cache_reuses_valid_image_but_isolates_conversations():
    QQVision._cache.clear()
    QQVision._cache_bytes = 0
    data = image_bytes()
    runner, base = await serve({"/resource": data})
    calls = []

    async def resolve(segment):
        calls.append(segment["resource_index"])
        return {"url": f"{base}/{segment['resource_index']}"}

    original = row(900, image_segment(resource_index="resource"),
                   identity=("qq", "bot", "group:one"))
    same_conversation = row(900, image_segment(resource_index="expired"),
                            identity=("qq", "bot", "group:one"))
    other_conversation = row(900, image_segment(resource_index="expired"),
                             identity=("qq", "bot", "group:two"))
    try:
        first = await QQVision(resolve).prepare([original], {900})
        cached = await QQVision(lambda _segment: (_ for _ in ()).throw(AssertionError("cache miss"))) \
            .prepare([same_conversation], {900})
        uncached = await QQVision(resolve).prepare([other_conversation], {900})
        assert first["images"] and cached["images"]
        assert uncached["errors"] == [{"journal_id": 900, "segment_index": 0,
                                        "reason": "resource_unavailable"}]
        assert calls == ["resource", "expired"]
    finally:
        await runner.cleanup()
        QQVision._cache.clear()
        QQVision._cache_bytes = 0


def test_private_cache_evicts_oldest_entries_at_its_byte_limit(monkeypatch):
    QQVision._cache.clear()
    QQVision._cache_bytes = 0
    monkeypatch.setattr(vision_module, "MAX_CACHE_BYTES", 4)
    QQVision._cache_put(("qq", "bot", "group:one", 1, 0), b"abc")
    QQVision._cache_put(("qq", "bot", "group:one", 2, 0), b"def")
    assert QQVision._cache_get(("qq", "bot", "group:one", 1, 0)) is None
    assert QQVision._cache_get(("qq", "bot", "group:one", 2, 0)) == b"def"
    assert QQVision._cache_bytes == 3
    QQVision._cache.clear()
    QQVision._cache_bytes = 0


@pytest.mark.asyncio
async def test_platform_empty_url_falls_back_to_trusted_local_image(tmp_path):
    payload = image_bytes()
    path = tmp_path / 'platform-cache.png'
    path.write_bytes(payload)
    async def resolve(segment):
        return {'url': '', 'file': str(path)}
    result = await QQVision(resolve).prepare([row(42, image_segment())], {42})
    assert base64.b64decode(result['images'][0]['data']) == payload
    assert not result['errors']
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_invalid_downloads_also_exhaust_shared_batch_budget(tmp_path):
    invalid = tmp_path / 'invalid.png'
    invalid.write_bytes(b'not an image')
    good = tmp_path / 'good.png'
    good.write_bytes(image_bytes())
    calls = []
    async def resolve(segment):
        calls.append(segment['resource_index'])
        return {'file': str(invalid if segment['resource_index'] < 3 else good)}
    result = await QQVision(resolve, max_batch_bytes=2 * invalid.stat().st_size).prepare(
        [row(i, image_segment(resource_index=i)) for i in (1, 2, 3)], {1, 2, 3})
    assert calls == [1, 2]
    assert [error['reason'] for error in result['errors']] == [
        'unsupported_image_format', 'unsupported_image_format', 'batch_size_limit']
    assert not result['images']


@pytest.mark.asyncio
async def test_cache_hit_still_obeys_smaller_reader_image_limit(tmp_path):
    QQVision._cache.clear()
    QQVision._cache_bytes = 0
    path = tmp_path / 'image.png'; path.write_bytes(image_bytes())
    async def resolve(_): return {'file': str(path)}
    item = row(99191, image_segment(), identity=('qq', 'cache-limit-bot', 'group:7'))
    assert (await QQVision(resolve).prepare([item], {99191}))['images']
    result = await QQVision(resolve, max_image_bytes=1).prepare([item], {99191})
    assert result['images'] == [] and result['errors'][0]['reason'] == 'image_size_limit'
