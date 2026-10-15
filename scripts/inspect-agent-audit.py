#!/usr/bin/env python3
"""Read operational metadata and join actual OneBot receipt IDs to Journal."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_journal"))
from runtime_audit import AuditStore
from journal import JournalStore


def main():
    data = ROOT / ".runtime/astrbot/data/plugin_data"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=data / "v2_dsh_router/audit")
    parser.add_argument("--journal", type=Path, default=data / "v2_journal/journal.sqlite3")
    parser.add_argument("--receipts", action="store_true", help="include per-receipt IDs and exact Journal evidence")
    args = parser.parse_args()
    audit = AuditStore(args.audit, create=False)
    journal = JournalStore(args.journal, read_only=True)
    receipts = list(audit.receipt_evidence(journal))
    confirmed = sum(row["journal_confirmed"] for row in receipts)
    counts = Counter(row.get("event") for row in audit.rows())
    result = {"receipts_observed": len(receipts), "journal_confirmed": confirmed,
              "receipt_confirmation_unresolved": len(receipts) - confirmed,
              "send_attempts": counts["send/start"], "send_failures": counts["send/failed"],
              "completed_turns": counts["turn/completed"], "paused_or_failed_turns": counts["turn/failed"],
              "atomic_snapshot": False, "retention": "up to eight rotating 8 MiB metadata files",
              "scope": "Only retained known API receipts; lost receipts are not inferred from text or time.",
              "complete_acceptance_requires": "Stop writers before collecting audit and Journal together."}
    if args.receipts:
        result["receipts"] = receipts
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
