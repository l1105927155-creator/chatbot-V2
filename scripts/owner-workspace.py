#!/usr/bin/env python3
"""Prepare/check/unmount a fixed-capacity Owner workspace as the Bot user."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dsh/plugins/agent-profile"))
from owner_volume import DEFAULT_BYTES, prepare, unmount, verified_workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "unmount"))
    parser.add_argument("--workspace", type=Path, default=ROOT / ".runtime/dsh-workspace-owner")
    parser.add_argument("--image", type=Path, default=ROOT / ".runtime/owner-storage/workspace.ext4")
    parser.add_argument("--size-mib", type=int, default=DEFAULT_BYTES // (1024 ** 2))
    args = parser.parse_args()
    if args.action == "prepare":
        data = prepare(args.workspace, args.image, args.size_mib * 1024 ** 2)
        print(json.dumps({"prepared": True, **data}))
    elif args.action == "check":
        fd = verified_workspace(args.workspace)
        os.close(fd)
        print(json.dumps({"bounded_workspace_verified": True, "workspace": str(args.workspace)}))
    else:
        unmount(args.workspace)
        print(json.dumps({"unmounted": True, "workspace": str(args.workspace)}))


if __name__ == "__main__":
    main()
