from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare-business-plugin-config.py"
SPEC = importlib.util.spec_from_file_location("prepare_business_plugin_config", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def source_dir(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    (root / "astrbot_plugin_bilibili_config.json").write_text(json.dumps({
        "sessdata": "private-bili-session",
        "proxy": "http://proxy-user:proxy-pass@example.test:8080",
        "enable_ai_summary": True,
    }))
    (root / "astrbot_plugin_twitter_config.json").write_text(json.dumps({
        "basic": {
            "twitter_data_provider": "nitter",
            "twitter_fxtwitter_api_base": "https://api.example.test/",
            "twitter_nitter_url": "https://private-mirror.example.test",
            "twitter_proxy": "http://proxy-user:proxy-pass@example.test:8080",
        },
        "translation": {"twitter_translate_enabled": True},
    }))
    for path in root.iterdir():
        os.chmod(path, 0o600)
    return root


def test_stage_is_allowlisted_and_does_not_copy_stale_or_translation_config(tmp_path):
    configs, report = module.build_stage(source_dir(tmp_path))
    bili = configs["astrbot_plugin_bilibili_config.json"]
    twitter = configs["astrbot_plugin_twitter_config.json"]
    assert bili["sessdata"] == ""
    assert bili["proxy"].startswith("http://")
    assert bili["enable_ai_summary"] is False
    assert bili["rai"] is False and bili["node"] is False
    assert twitter["basic"]["twitter_data_provider"] == "fxtwitter"
    assert twitter["basic"]["twitter_fxtwitter_api_base"] == "https://api.example.test"
    assert "twitter_nitter_url" not in twitter["basic"]
    assert twitter["translation"] == {"twitter_translate_enabled": False}
    serialized_report = json.dumps(report)
    assert "private-bili-session" not in serialized_report
    assert "proxy-pass" not in serialized_report
    assert "private-mirror" not in serialized_report
    assert report["bilibili"]["sessdata_configured_in_source"] is True
    assert report["bilibili"]["sessdata_staged"] is False


def test_apply_is_private_and_backs_up_existing_target(tmp_path):
    stage = tmp_path / "stage"
    target = tmp_path / "target"
    stage.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    for name in ("astrbot_plugin_bilibili_config.json", "astrbot_plugin_twitter_config.json"):
        (stage / name).write_text("{}\n")
        os.chmod(stage / name, 0o600)
    existing = target / "astrbot_plugin_twitter_config.json"
    existing.write_text('{"old":true}\n')
    os.chmod(existing, 0o600)
    applied = module.apply_stage(stage, target)
    assert len(applied) == 2
    assert stat.S_IMODE((target / "astrbot_plugin_bilibili_config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((target / "astrbot_plugin_twitter_config.json").stat().st_mode) == 0o600
    backups = list(target.glob("astrbot_plugin_twitter_config.json.bak.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"old": True}
