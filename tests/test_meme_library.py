from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import os
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "v2_meme_library_test_package"
pkg = ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / "astrbot-plugins/v2_dsh_router")]
sys.modules[PACKAGE] = pkg


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load(PACKAGE + ".vision", "astrbot-plugins/v2_dsh_router/vision.py")
meme_module = load(PACKAGE + ".meme_library", "astrbot-plugins/v2_dsh_router/meme_library.py")
MemeLibrary = meme_module.MemeLibrary
MemeLibraryError = meme_module.MemeLibraryError


def png_bytes(color=(7, 40, 90)):
    image = Image.new("RGB", (4, 3), color)
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


@pytest.mark.asyncio
async def test_list_and_send_uses_verified_snapshot_with_native_callback_lifetime(tmp_path):
    root = tmp_path / "meme"
    image_dir = root / "开心"
    image_dir.mkdir(parents=True)
    original = png_bytes()
    image_path = image_dir / "开心-01-abc1.png"
    image_path.write_bytes(original)
    captured = {}

    async def send(path: Path):
        captured["path"] = path
        captured["bytes"] = path.read_bytes()
        # Replacing the source while the native send is in progress must not
        # alter the already opened/snapshotted payload.
        image_path.write_bytes(png_bytes((100, 0, 0)))

    library = MemeLibrary(root, send, spool_dir=tmp_path / "spool")
    categories = await library.categories()
    assert categories == {
        "categories": [{"name": "开心", "count": 1}],
        "total_categories": 1,
        "total_images": 1,
    }
    listing = await library.list("开心")
    assert listing["total"] == 1
    assert listing["items"][0]["category"] == "开心"
    assert listing["items"][0]["name"] == "开心-01-abc1"
    assert await library.send_meme(listing["items"][0]["id"]) == {"sent": True, "mime_type": "image/png"}
    assert captured["bytes"] == original
    assert captured["path"].exists() is False
    assert hashlib.sha256(captured["bytes"]).hexdigest() == hashlib.sha256(original).hexdigest()


@pytest.mark.asyncio
async def test_rejects_links_unsupported_images_and_invalid_ids(tmp_path):
    root = tmp_path / "meme"
    (root / "安全").mkdir(parents=True)
    (root / "安全" / "ok.png").write_bytes(png_bytes())
    (root / "安全" / "fake.png").write_bytes(b"not an image")
    external = tmp_path / "outside.png"
    external.write_bytes(png_bytes())
    try:
        os.link(external, root / "安全" / "hard.png")
        os.symlink(external, root / "安全" / "sym.png")
    except OSError:
        pytest.skip("filesystem does not support test links")
    library = MemeLibrary(root, lambda path: None)
    listing = await library.list("安全", limit=10)
    assert [item["name"] for item in listing["items"]] == ["ok"]
    for bad in ("../../outside.png", "g" * 32, ""):
        with pytest.raises(MemeLibraryError):
            await library.send_meme(bad)
    with pytest.raises(MemeLibraryError):
        await library.list("安全", limit=0)
    with pytest.raises(MemeLibraryError):
        await library.list("安全", offset=-1)
    with pytest.raises(MemeLibraryError):
        await library.list("安全", offset=True)
    with pytest.raises(MemeLibraryError, match="meme_categories"):
        await library.list("../outside")


@pytest.mark.asyncio
async def test_unknown_resource_and_native_send_failure_are_bounded(tmp_path):
    root = tmp_path / "meme"
    image_dir = root / "空"
    image_dir.mkdir(parents=True)
    (image_dir / "x.png").write_bytes(png_bytes())
    captured = {}

    async def failed_send(path):
        captured["path"] = path
        raise RuntimeError(f"private path={path}")

    library = MemeLibrary(root, failed_send, spool_dir=tmp_path / "spool")
    with pytest.raises(MemeLibraryError, match="未找到"):
        await library.send_meme("0" * 32)
    resource_id = (await library.list("空"))["items"][0]["id"]
    with pytest.raises(MemeLibraryError, match="发送失败") as exc:
        await library.send_meme(resource_id)
    assert str(captured["path"]) not in str(exc.value)
    assert captured["path"].exists() is False


@pytest.mark.asyncio
async def test_cancel_during_snapshot_wait_cleans_worker_result(tmp_path, monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    created = {}
    sends = []
    library = MemeLibrary(tmp_path / "unused", lambda path: sends.append(path), spool_dir=tmp_path / "spool")

    def slow_snapshot(_resource_id):
        started.set()
        time.sleep(0.08)
        path = tmp_path / "spool" / "late-snapshot.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes())
        created["path"] = path
        finished.set()
        return path, "image/png"

    monkeypatch.setattr(library, "_snapshot", slow_snapshot)
    send_task = asyncio.create_task(library.send_meme("a" * 32))
    assert await asyncio.to_thread(started.wait, 1)
    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task
    assert await asyncio.to_thread(finished.wait, 1)
    # The task's completion callback performs cleanup on the event loop.
    for _ in range(50):
        if not created["path"].exists():
            break
        await asyncio.sleep(0.01)
    assert created["path"].exists() is False
    assert sends == []


@pytest.mark.asyncio
async def test_root_regular_files_do_not_count_as_categories(tmp_path):
    root = tmp_path / "meme"
    root.mkdir()
    (root / "README.md").write_text("library notes", "utf-8")
    (root / "NOTICE.md").write_text("attribution", "utf-8")
    for index in range(70):
        (root / f"metadata-{index}.json").write_text("{}", "utf-8")
    (root / "开心").mkdir()
    (root / "开心" / "one.png").write_bytes(png_bytes())
    library = MemeLibrary(root, lambda path: None)
    assert (await library.categories()) == {
        "categories": [{"name": "开心", "count": 1}],
        "total_categories": 1,
        "total_images": 1,
    }
    assert (await library.list("开心"))["total"] == 1


@pytest.mark.asyncio
async def test_categories_count_only_verified_images_and_exact_category_names(tmp_path):
    root = tmp_path / "meme"
    for name in ("开心", "开心聊天", "日常 开心"):
        (root / name).mkdir(parents=True)
    (root / "开心" / "ok.png").write_bytes(png_bytes())
    (root / "开心" / "broken.png").write_bytes(b"not a png")
    (root / "开心聊天" / "ok.png").write_bytes(png_bytes((3, 4, 5)))
    (root / "日常 开心" / "ok.png").write_bytes(png_bytes((9, 8, 7)))
    (root / "空分类").mkdir()

    library = MemeLibrary(root, lambda path: None)
    assert await library.categories() == {
        "categories": [
            {"name": "开心", "count": 1},
            {"name": "开心聊天", "count": 1},
            {"name": "日常 开心", "count": 1},
        ],
        "total_categories": 3,
        "total_images": 3,
    }
    for category, expected_name in (("开心", "ok"), ("开心聊天", "ok"), ("日常 开心", "ok")):
        result = await library.list(category)
        assert result["category"] == category
        assert [item["name"] for item in result["items"]] == [expected_name]
        assert result["total"] == 1
    with pytest.raises(MemeLibraryError, match="meme_categories"):
        await library.list("开心 聊天")
    with pytest.raises(MemeLibraryError, match="meme_categories"):
        await library.list("空分类")


@pytest.mark.asyncio
async def test_category_pagination_reaches_all_images_with_terminal_page(tmp_path):
    root = tmp_path / "meme"
    folder = root / "分页分类"
    folder.mkdir(parents=True)
    for index in range(57):
        (folder / f"{index:03}.png").write_bytes(png_bytes((index % 255, 2, 3)))
    library = MemeLibrary(root, lambda path: None)

    first = await library.list("分页分类", limit=50)
    assert first["total"] == 57
    assert len(first["items"]) == 50
    assert first["offset"] == 0
    assert first["has_more"] is True
    assert first["next_offset"] == 50
    second = await library.list("分页分类", limit=50, offset=first["next_offset"])
    assert len(second["items"]) == 7
    assert second["offset"] == 50
    assert second["has_more"] is False
    assert second["next_offset"] is None
    assert len({item["id"] for item in first["items"] + second["items"]}) == 57

    past_end = await library.list("分页分类", offset=2000)
    assert past_end["items"] == []
    assert past_end["has_more"] is False
    assert past_end["next_offset"] is None
    with pytest.raises(MemeLibraryError):
        await library.list("分页分类", limit=True)
    with pytest.raises(MemeLibraryError):
        await library.list("分页分类", offset=2001)


def test_explicit_category_only_import_preserves_hash_source_and_license(tmp_path):
    import importlib.util
    import json

    script = ROOT / "scripts/import-shared-memes.py"
    spec = importlib.util.spec_from_file_location("shared_meme_import_test", script)
    assert spec and spec.loader
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)

    source = tmp_path / "legacy"
    folder = source / "memes" / "happy|开心"
    folder.mkdir(parents=True)
    body = png_bytes()
    (folder / "7.png").write_bytes(body)
    (source / "LICENSE.md").write_text("license", "utf-8")
    (source / "NOTICE.md").write_text("notice", "utf-8")
    (source / "memes_data.json").write_text(json.dumps({"happy|开心": "开心分类"}), "utf-8")
    (source / "semantic_metadata.json").write_text(json.dumps({"pack_id": "test-pack", "images": {
        "stale-entry": {"content_sha256": "f" * 64, "relative_path": "memes/happy/old.png"}
    }}), "utf-8")
    destination = tmp_path / "shared"

    assert importer.import_pack(source, destination, category_only=False) == {
        "imported": 0, "verified_semantics": 0, "category_only": 0
    }
    result = importer.import_pack(source, destination, category_only=True)
    assert result == {"imported": 1, "verified_semantics": 0, "category_only": 1}
    item = json.loads((destination / "provenance.json").read_text("utf-8"))["items"][0]
    assert item["path"].startswith("开心/开心-01-")
    assert item["sha256"] == hashlib.sha256(body).hexdigest()
    assert item["original_path"] == "memes/happy|开心/7.png"
    assert item["semantic_status"] == "only_category/semantic_unverified"
    assert (destination / "LICENSE.md").read_text("utf-8") == "license"
    assert (destination / "NOTICE.md").read_text("utf-8") == "notice"
