import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_bottles.store import BottleLimitError, BottleStore  # noqa: E402


def make_store(tmp_path):
    return BottleStore(tmp_path / "bottles.sqlite3")


def add(store, content="hello", **options):
    values = {"max_count": 10, "max_disk_bytes": 10_000_000}
    values.update(options)
    return store.add(
        content=content,
        group_id="123",
        sender_qq="10001",
        sender_name="昵称",
        anonymous=False,
        **values,
    )


def test_add_count_random_pick_does_not_delete_and_reopens(tmp_path):
    store = make_store(tmp_path)
    row = add(store)
    assert store.count() == 1

    picked = store.random_one()
    assert picked is not None
    assert picked["id"] == row["id"]
    assert picked["content"] == "hello"
    assert store.count() == 1

    store.close()
    reopened = make_store(tmp_path)
    assert reopened.count() == 1
    assert reopened.random_one()["content"] == "hello"
    reopened.close()


def test_add_stores_display_time_alongside_timestamp(tmp_path):
    store = make_store(tmp_path)
    row = add(store, now=1_793_954_534)
    assert row["created_at"] == 1_793_954_534
    assert row["created_at_text"] == "2026/11/06 16:42"
    assert store.random_one()["created_at_text"] == "2026/11/06 16:42"
    store.close()


def test_existing_database_backfills_display_time_once(tmp_path):
    path = tmp_path / "bottles.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE bottles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            group_id TEXT NOT NULL,
            sender_qq TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            anonymous INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO bottles(content, group_id, sender_qq, sender_name, "
        "anonymous, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("正常", "123", "10001", "昵称", 0, 1_793_954_534),
            ("异常", "123", "10001", "昵称", 0, -69_093_907_200),
        ],
    )
    conn.commit()
    conn.close()

    store = BottleStore(path)
    rows = {row["content"]: row for row in store.recent(10)}
    assert rows["正常"]["created_at_text"] == "2026/11/06 16:42"
    assert rows["异常"]["created_at_text"] == "未知时间"
    store.close()

    conn = sqlite3.connect(path)
    conn.execute("UPDATE bottles SET created_at_text='' WHERE content='正常'")
    conn.commit()
    conn.close()

    reopened = BottleStore(path)
    rows = {row["content"]: row for row in reopened.recent(10)}
    assert rows["正常"]["created_at_text"] == ""
    reopened.close()


def test_empty_pool_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.random_one() is None
    store.close()


def test_count_cap_is_reported(tmp_path):
    store = make_store(tmp_path)
    add(store, max_count=1)
    with pytest.raises(BottleLimitError) as error:
        add(store, content="second", max_count=1)
    assert error.value.kind == "count"
    assert store.count() == 1
    store.close()


def test_disk_cap_is_reported(tmp_path):
    store = make_store(tmp_path)
    current = store.disk_bytes()
    with pytest.raises(BottleLimitError) as error:
        add(store, content="x" * 4000, max_disk_bytes=current + 100)
    assert error.value.kind == "disk"
    assert store.count() == 0
    store.close()


def test_deleted_sqlite_pages_can_be_reused_near_disk_cap(tmp_path):
    store = make_store(tmp_path)
    first = add(store, content="a" * 20000)
    add(store, content="b" * 20000)
    assert store.delete([first["id"]]) == 1
    assert store._conn.execute("PRAGMA freelist_count").fetchone()[0] > 0
    cap = store.disk_bytes() + 512
    add(store, content="c" * 16000, max_disk_bytes=cap)
    assert store.count() == 2
    store.close()


def test_delete_selected_rows(tmp_path):
    store = make_store(tmp_path)
    first = add(store, content="first")
    second = add(store, content="second")
    third = add(store, content="third")

    assert store.delete([first["id"], third["id"]]) == 2
    assert [row["id"] for row in store.recent(10)] == [second["id"]]
    assert store.delete([999999]) == 0
    assert store.delete([]) == 0
    store.close()


def test_anonymous_metadata_is_stored_and_filterable(tmp_path):
    store = make_store(tmp_path)
    anonymous = store.add(
        content="秘密",
        group_id="111",
        sender_qq="20002",
        sender_name="甲",
        anonymous=True,
        max_count=10,
        max_disk_bytes=10_000_000,
    )
    normal = store.add(
        content="公开",
        group_id="222",
        sender_qq="30003",
        sender_name="乙",
        anonymous=False,
        max_count=10,
        max_disk_bytes=10_000_000,
    )
    assert anonymous["anonymous"] == 1
    assert normal["anonymous"] == 0

    by_group = store.recent(10, group_id="111")
    assert [row["id"] for row in by_group] == [anonymous["id"]]
    by_qq = store.recent(10, qq="30003")
    assert [row["id"] for row in by_qq] == [normal["id"]]
    assert store.recent(10)[0]["id"] == normal["id"]
    store.close()
