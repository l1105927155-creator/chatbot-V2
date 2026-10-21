"""Confirm a native model configuration publication before reading ACP choices."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class SharedModels:
    def __init__(self, profile, status, epoch):
        self.profile = Path(profile)
        self.status = Path(status)
        self.epoch = epoch

    def snapshot(self, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                raw = self.profile.read_bytes()
                if len(raw) > 2 * 1024 * 1024:
                    raise RuntimeError("共享模型配置过大。")
                fingerprint = hashlib.sha256(raw).hexdigest()
                status_bytes = self.status.read_bytes()
                if len(status_bytes) > 2 * 1024 * 1024:
                    raise RuntimeError("共享模型目录过大。")
                status = json.loads(status_bytes)
                if status.get('epoch') == self.epoch and status.get('source_sha256') == fingerprint:
                    if status.get('ok') is not True:
                        raise RuntimeError("DSH Web模型配置未通过校验，请检查并修正模型设置。")
                    catalog = status.get('catalog')
                    if not isinstance(catalog, list):
                        raise RuntimeError("共享模型目录格式无效。")
                    # Recheck after reading the publication to avoid pairing a
                    # replacement source with an old acknowledgement.
                    if self.profile.read_bytes() == raw:
                        return fingerprint, catalog
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("DSH Web模型配置尚未同步，请稍后重试。")
            time.sleep(0.025)
