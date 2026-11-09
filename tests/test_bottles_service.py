import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_bottles.service import (  # noqa: E402
    PICK,
    RULES,
    THROW,
    THROW_ANONYMOUS,
    BottleError,
    BottleService,
    parse_command,
)
from v2_bottles.store import BottleStore  # noqa: E402


def make_service(tmp_path, **config):
    store = BottleStore(tmp_path / "bottles.sqlite3")
    values = {"max_bottles": 10, "max_chars": 200, "max_disk_mb": 1024,
              "enabled_groups": ["123"]}
    values.update(config)
    return BottleService(store, values), store


def test_parse_requires_a_literal_space_for_throw_commands():
    assert parse_command("捡") == (PICK, "")
    assert parse_command("漂流瓶") == (RULES, "")
    assert parse_command("扔 一只猫") == (THROW, "一只猫")
    assert parse_command("偷偷扔 一只猫") == (THROW_ANONYMOUS, "一只猫")
    assert parse_command("扔   一只猫") == (THROW, "一只猫")

    assert parse_command("扔了一只猫") is None
    assert parse_command("偷偷扔了一只猫") is None
    assert parse_command("扔：一只猫") is None
    assert parse_command("偷偷扔：一只猫") is None
    assert parse_command("扔") is None
    assert parse_command("扔 ") is None
    assert parse_command("捡到一只猫") is None
    assert parse_command("漂流瓶是什么") is None


def test_throw_then_pick_shows_public_source(tmp_path):
    service, store = make_service(tmp_path)
    reply = service.throw(
        content="一只猫", group_id="123", sender_qq="10001",
        sender_name="昵称", anonymous=False,
    )
    assert reply == "你的瓶子已经扔向大海啦"

    picked = service.pick()
    assert "一只猫" in picked
    assert "123 - 昵称 - 10001" in picked
    assert "当前共有" not in picked
    assert not picked.startswith("漂流瓶 · ")
    first_line = picked.splitlines()[0]
    assert first_line.count("/") == 2 and first_line.count("-") == 0
    assert first_line.count(":") == 1
    store.close()


def test_pick_uses_edited_display_time_and_keeps_timestamp(tmp_path):
    service, store = make_service(tmp_path)
    service.throw(
        content="一只猫", group_id="123", sender_qq="10001",
        sender_name="昵称", anonymous=False,
    )
    row = store.recent(1)[0]
    conn = sqlite3.connect(store.path)
    try:
        conn.execute(
            "UPDATE bottles SET created_at_text=? WHERE id=?",
            ("2001/02/03 04:05", row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    picked = service.pick()
    assert picked.splitlines()[0] == "2001/02/03 04:05"
    assert store.recent(1)[0]["created_at"] == row["created_at"]
    store.close()


def test_pick_falls_back_to_timestamp_when_display_time_is_empty(tmp_path):
    service, store = make_service(tmp_path)
    service.throw(
        content="一只猫", group_id="123", sender_qq="10001",
        sender_name="昵称", anonymous=False,
    )
    row = store.recent(1)[0]
    conn = sqlite3.connect(store.path)
    try:
        conn.execute(
            "UPDATE bottles SET created_at_text='' WHERE id=?", (row["id"],)
        )
        conn.commit()
    finally:
        conn.close()

    first_line = service.pick().splitlines()[0]
    assert first_line.count("/") == 2
    assert first_line.count(":") == 1
    assert first_line != "未知时间"
    store.close()


def test_anonymous_pick_hides_source_but_store_keeps_it(tmp_path):
    service, store = make_service(tmp_path)
    reply = service.throw(
        content="秘密", group_id="123", sender_qq="10001",
        sender_name="昵称", anonymous=True,
    )
    assert reply == "你的瓶子已经偷偷扔向大海啦"
    picked = service.pick()
    assert "秘密" in picked
    assert "匿名漂流瓶" in picked
    assert "123" not in picked
    assert "昵称" not in picked
    assert "10001" not in picked

    row = store.recent(1)[0]
    assert row["group_id"] == "123"
    assert row["sender_qq"] == "10001"
    assert row["sender_name"] == "昵称"
    store.close()


def test_content_length_and_empty_validation(tmp_path):
    service, store = make_service(tmp_path, max_chars=5)
    with pytest.raises(BottleError) as error:
        service.throw(content="", group_id="123", sender_qq="1",
                      sender_name="甲", anonymous=False)
    assert error.value.kind == "empty"

    with pytest.raises(BottleError) as error:
        service.throw(content="一二三四五六", group_id="123", sender_qq="1",
                      sender_name="甲", anonymous=False)
    assert error.value.kind == "too_long"
    assert "5 字" in error.value.public_message
    store.close()


def test_count_cap_refusal_message(tmp_path):
    service, store = make_service(tmp_path, max_bottles=1)
    service.throw(content="first", group_id="123", sender_qq="1",
                  sender_name="甲", anonymous=False)
    with pytest.raises(BottleError) as error:
        service.throw(content="second", group_id="123", sender_qq="1",
                      sender_name="甲", anonymous=False)
    assert error.value.kind == "count"
    assert "上限 1 条" in error.value.public_message
    store.close()


def test_rules_start_with_total_and_state_space_requirement(tmp_path):
    service, store = make_service(tmp_path)
    service.throw(content="first", group_id="123", sender_qq="1",
                  sender_name="甲", anonymous=False)
    texts = service.rules_texts()
    assert texts[0].startswith("当前共有 1 个漂流瓶。")
    assert any("必须有空格" in text for text in texts)
    assert any("暂不支持图片" in text for text in texts)
    store.close()


def test_status_reports_caps_and_enabled_groups(tmp_path):
    service, store = make_service(tmp_path, max_bottles=7, max_chars=50,
                                  max_disk_mb=2, enabled_groups=["123", "456"])
    status = service.status()
    assert status["count"] == 0
    assert status["max_bottles"] == 7
    assert status["max_chars"] == 50
    assert status["max_disk_bytes"] == 2 * 1024 * 1024
    assert status["enabled_groups"] == ["123", "456"]
    store.close()


def test_group_enable_list_is_persistent_config(tmp_path):
    service, store = make_service(tmp_path, enabled_groups=["123"])
    assert service.group_enabled("123") is True
    assert service.group_enabled("999") is False
    store.close()
