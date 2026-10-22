#!/usr/bin/env python3
"""Score exported sandbox behavior evidence and compare locked baselines."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sandbox.evaluation import EvaluationError, compare, score  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scoring = commands.add_parser("score", help="score an exported /api/evidence/export JSON")
    scoring.add_argument("--evidence", required=True, type=Path)
    scoring.add_argument("--manifest", required=True, type=Path)
    scoring.add_argument("--output", required=True, type=Path)
    comparing = commands.add_parser("compare", help="compare a report with a locked baseline")
    comparing.add_argument("--report", required=True, type=Path)
    comparing.add_argument("--baseline", required=True, type=Path)
    comparing.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = (score(args.evidence, args.manifest) if args.command == "score"
                  else compare(args.report, args.baseline))
    except (EvaluationError, OSError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
