#!/usr/bin/env python3
"""Verify native file sends and /new when OneBot self feedback is dropped."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("file_receipt_checks", ROOT / "scripts/check-qq-files.py")
files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(files)


class ReceiptResetCheck(files.FileCheck):
    drop_feedback = True

    def request(self, method, path, data=None, **kwargs):
        if (method == "POST" and path == "/api/send" and isinstance(data, dict)
                and any(s.get("data", {}).get("text") == "把刚保存的原文件作为附件发回来。"
                        for s in data.get("message", [])) and self.drop_feedback):
            data = {**data, "faults": {"drop_feedback": True}}
        return super().request(method, path, data, **kwargs)

    def story_and_reset(self, key, drop):
        self.drop_feedback = drop
        name, payload, content_type, kind = self.payloads()[0]
        result = self.story(key, name, payload, content_type, kind)
        actions = self.http("/api/state")["actions"]
        action = next(a for a in reversed(actions)
                      if a["action"] in ("send_group_msg", "send_private_msg")
                      and any(s.get("type") == "file" for s in a["request"].get("message", [])))
        assert action["feedback"] == ("dropped" if drop else "delivered")
        message_id = str(action["response"]["data"]["message_id"])
        rows = [r for r in self.rows() if r["message_id"] == message_id]
        assert len(rows) == 1 and rows[0]["direction"] == "outbound"
        before = self.state(key)
        event = self.wake(key, "/new", sender=self.owner)
        self.answer(event, "已开始新的 Agent 对话")
        after = self.wait(lambda: self.state(key) if self.state(key)["session_id"] != before["session_id"]
                          else None, "rotated native session")
        command = next(r for r in self.rows() if r["message_id"] == str(event["message_id"]))
        assert after["history_after_journal_id"] == command["journal_id"]
        return {**result, "feedback": action["feedback"], "exactly_one_file_row": True,
                "new_session": True, "history_floor": after["history_after_journal_id"]}

    def run(self):
        try:
            self.start()
            for key, drop in [("group:91000", True), (self.key, True), ("group:91000", False)]:
                self.case(key + ("-dropped" if drop else "-normal"),
                          lambda key=key, drop=drop: self.story_and_reset(key, drop))
        finally:
            self.stop()
        result = {"passed": bool(self.results) and all(r["passed"] for r in self.results),
                  "cases": self.results, "real_qq_connections": 0}
        self.save("result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.data_dir or ROOT / ".runtime" / ("file-receipt-reset-" + uuid.uuid4().hex[:12])
    result = ReceiptResetCheck(directory).run()
    print(json.dumps({"passed": result["passed"], "result": str(directory / "result.json")}))
    raise SystemExit(0 if result["passed"] else 1)
