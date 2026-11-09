import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins"))

from v2_bottles.service import BottleError, BottleService  # noqa: E402
from v2_bottles.settings import DEFAULT_SETTINGS, SettingsStore  # noqa: E402
from v2_bottles.store import BottleStore  # noqa: E402


def test_defaults_save_and_reload(tmp_path):
    path = tmp_path / "settings.json"
    settings = SettingsStore(path)
    assert settings.get("max_chars") == 200
    assert settings.get("max_chars") == DEFAULT_SETTINGS["max_chars"]

    settings.update({"max_chars": 50})
    assert settings.get("max_chars") == 50
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["max_chars"] == 50

    reopened = SettingsStore(path)
    assert reopened.get("max_chars") == 50


def test_invalid_settings_are_rejected(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    for value in (0, -1, 10001, "200", True, 1.5):
        with pytest.raises(ValueError):
            settings.update({"max_chars": value})
    with pytest.raises(ValueError):
        settings.update({"unknown": 1})
    assert settings.get("max_chars") == 200


def test_service_uses_new_limit_immediately(tmp_path):
    store = BottleStore(tmp_path / "bottles.sqlite3")
    settings = SettingsStore(tmp_path / "settings.json")
    service = BottleService(
        store, {"max_bottles": 10, "max_chars": 200}, settings=settings
    )
    assert service.max_chars == 200

    settings.update({"max_chars": 5})
    assert service.max_chars == 5
    with pytest.raises(BottleError) as error:
        service.throw(
            content="123456", group_id="1", sender_qq="2",
            sender_name="n", anonymous=False,
        )
    assert error.value.kind == "too_long"
    assert "5 字" in error.value.public_message

    settings.update({"max_chars": 10})
    assert service.max_chars == 10
    reply = service.throw(
        content="123456", group_id="1", sender_qq="2",
        sender_name="n", anonymous=False,
    )
    assert reply == service.text("throw_success")
    store.close()
