#!/usr/bin/env python3
"""Stage minimal, private business-plugin configs; --apply installs them."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OLD_CONFIG = Path("/home/abasun/chatbot/astrbot/data/config")
STAGE = ROOT / ".runtime/business-plugin-config-stage"


def read_private_json(path: Path) -> dict[str, Any]:
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) & 0o077):
        raise RuntimeError(f"unsafe source config metadata: {path.name}")
    if st.st_size > 2_000_000:
        raise RuntimeError(f"source config is unexpectedly large: {path.name}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"source config is not a JSON object: {path.name}")
    return data


def atomic_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def build_stage(source_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    bili = read_private_json(source_dir / "astrbot_plugin_bilibili_config.json")
    twitter = read_private_json(source_dir / "astrbot_plugin_twitter_config.json")

    bili_proxy = bili.get("proxy")
    bili_proxy = bili_proxy.strip() if isinstance(bili_proxy, str) else ""
    # The old SESSDATA was checked against Bilibili's read-only credential
    # endpoint on 2026-10-19 and was rejected, so it is deliberately omitted.
    bili_out = {"sessdata": "", "proxy": bili_proxy, "rai": False,
                "node": False, "enable_ai_summary": False}

    basic = twitter.get("basic") or {}
    if not isinstance(basic, dict):
        basic = {}
    api_base = basic.get("twitter_fxtwitter_api_base")
    api_base = (api_base.strip().rstrip("/") if isinstance(api_base, str)
                else "") or "https://api.fxtwitter.com"
    proxy = basic.get("twitter_proxy")
    proxy = proxy.strip() if isinstance(proxy, str) else ""
    twitter_out = {
        "basic": {
            "twitter_data_provider": "fxtwitter",
            "twitter_fxtwitter_api_base": api_base,
            "twitter_proxy": proxy,
        },
        "translation": {"twitter_translate_enabled": False},
    }
    safe_report = {
        "source_configs_private": True,
        "bilibili": {
            "sessdata_configured_in_source": bool(bili.get("sessdata")),
            "sessdata_live_validated": False,
            "sessdata_staged": False,
            "proxy_staged": bool(bili_proxy),
        },
        "twitter": {
            "source_provider": str((basic.get("twitter_data_provider") or "nitter")),
            "provider_staged": "fxtwitter",
            "custom_api_base_staged": api_base != "https://api.fxtwitter.com",
            "proxy_staged": bool(proxy),
            "translation_enabled_staged": False,
        },
    }
    return {
        "astrbot_plugin_bilibili_config.json": bili_out,
        "astrbot_plugin_twitter_config.json": twitter_out,
    }, safe_report


def apply_stage(stage: Path, target_dir: Path) -> list[str]:
    installed = []
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    for name in (
        "astrbot_plugin_bilibili_config.json",
        "astrbot_plugin_twitter_config.json",
    ):
        source = stage / name
        st = source.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise RuntimeError(f"unsafe staged config metadata: {name}")
        target = target_dir / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"unsafe existing target config: {name}")
            target_stat = target.lstat()
            if target_stat.st_nlink != 1 or target_stat.st_uid != os.getuid():
                raise RuntimeError(f"unsafe existing target metadata: {name}")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = target.with_name(f"{name}.bak.{stamp}")
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with target.open("rb") as source_stream, os.fdopen(backup_fd, "wb") as backup_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        backup_stream.write(chunk)
                    backup_stream.flush()
                    os.fsync(backup_stream.fileno())
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
        payload = json.loads(source.read_text(encoding="utf-8"))
        atomic_private_json(target, payload)
        installed.append(name)
    return installed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=OLD_CONFIG)
    parser.add_argument("--target-dir", type=Path, default=ROOT / ".runtime/astrbot/data/config")
    parser.add_argument("--apply", action="store_true", help="promote staged configs into target AstrBot data/config")
    args = parser.parse_args()
    configs, report = build_stage(args.source_dir)
    for name, value in configs.items():
        atomic_private_json(STAGE / name, value)
    atomic_private_json(STAGE / "safe-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply:
        installed = apply_stage(STAGE, args.target_dir)
        print(json.dumps({"applied": installed, "mode": "0600"}, ensure_ascii=False))
    else:
        print(f"Staged only under {STAGE}; official AstrBot config was not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
