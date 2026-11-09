import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_bottles.service import BottleError, BottleService  # noqa: E402
from v2_bottles.store import BottleStore  # noqa: E402
from v2_bottles.texts import DEFAULT_TEXTS, TextStore, _render  # noqa: E402


def test_defaults_partial_update_and_reopen(tmp_path):
    path = tmp_path / "texts.json"
    texts = TextStore(path)
    assert texts.get("empty_pool") == DEFAULT_TEXTS["empty_pool"]
    texts.update({"empty_pool": "空池啦", "throw_success": "扔出去啦"})
    assert texts.get("empty_pool") == "空池啦"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["throw_success"] == "扔出去啦"

    reopened = TextStore(path)
    assert reopened.get("empty_pool") == "空池啦"
    assert reopened.get("anonymous_source") == DEFAULT_TEXTS["anonymous_source"]


def test_unknown_key_and_oversize_are_rejected(tmp_path):
    texts = TextStore(tmp_path / "texts.json")
    with pytest.raises(ValueError):
        texts.update({"unknown_key": "x"})
    with pytest.raises(ValueError):
        texts.update({"empty_pool": "x" * 4001})
    assert texts.get("empty_pool") == DEFAULT_TEXTS["empty_pool"]


def test_render_only_replaces_known_placeholders():
    assert _render("时间 {time}，未知 {other}", {"time": "T"}) == "时间 T，未知 {other}"


def test_service_uses_custom_texts_and_updates_immediately(tmp_path):
    store = BottleStore(tmp_path / "bottles.sqlite3")
    texts = TextStore(tmp_path / "texts.json")
    service = BottleService(
        store, {"max_bottles": 10, "max_chars": 5}, texts=texts
    )
    assert service.pick() == DEFAULT_TEXTS["empty_pool"]

    texts.update({"empty_pool": "没有瓶子，先扔一个。", "throw_success": "瓶子出海啦"})
    assert service.pick() == "没有瓶子，先扔一个。"
    reply = service.throw(
        content="猫", group_id="1", sender_qq="2", sender_name="n", anonymous=False
    )
    assert reply == "瓶子出海啦"

    with pytest.raises(BottleError) as error:
        service.throw(
            content="一二三四五六", group_id="1", sender_qq="2",
            sender_name="n", anonymous=False,
        )
    assert error.value.public_message == texts.get("too_long", length=6, limit=5)

    texts.update({"rules_summary": "总数 {count}"})
    assert service.rules_texts()[0] == "总数 1"
    store.close()
