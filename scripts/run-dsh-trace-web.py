#!/usr/bin/env python3
"""Launch the pinned native DSH Web with read-only QQ session sources."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat

ROOT = Path(__file__).resolve().parents[1]


def discover_sources(runtime):
    """Select only Bot homes, never sandbox runs or arbitrary runtime folders."""
    state_dir = runtime / "astrbot/data/plugin_data/v2_dsh_router"
    sources = []
    for home in sorted(runtime.iterdir()):
        name = home.name
        if name not in {"dsh-home-chat", "dsh-home-owner-host"} and not re.fullmatch(r"dsh-home-group-[0-9a-f]{64}", name):
            continue
        sessions = home / "sessions"
        try:
            if not stat.S_ISDIR(home.lstat().st_mode) or not stat.S_ISDIR(sessions.lstat().st_mode):
                continue
            canonical_home = home.resolve(strict=True)
            canonical_sessions = sessions.resolve(strict=True)
            if canonical_home.parent != runtime.resolve(strict=True) or canonical_sessions.parent != canonical_home:
                continue
        except FileNotFoundError:
            continue
        # Empty group homes are normal immediately after console admission.
        # Keep them out until DSH commits its first native session file.
        has_session = False
        for directory, subdirs, files in os.walk(canonical_sessions, followlinks=False):
            subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
            if any(re.fullmatch(r"session(?:\.v[1-9][0-9]*)?\.jsonl(?:\.zstd)?", name)
                   and not (Path(directory) / name).is_symlink()
                   for name in files):
                has_session = True
                break
        if not has_session:
            continue
        suffix = name.removeprefix("dsh-home-")
        label = {"chat": "私聊", "owner-host": "Owner 私聊"}.get(suffix, suffix)
        state = state_dir / ("state-" + suffix + ".sqlite3")
        if state.is_file():
            # Read metadata only. Do not construct StateStore (it performs migrations).
            with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True) as db:
                keys = [row[0] for row in db.execute("SELECT conversation_key FROM conversation_state ORDER BY conversation_key")]
            if keys:
                label = "、".join(keys)
        sources.append({"root": str(sessions.resolve()), "label": label})
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT / ".runtime")
    parser.add_argument("--home", type=Path, default=ROOT / ".runtime/dsh-web-home")
    parser.add_argument("--source", action="append", default=[], metavar="LABEL=DSH_HOME")
    parser.add_argument("--port", type=int, default=3082)
    parser.add_argument("--poll-ms", type=int, default=1000)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    home = args.home.resolve()
    sources = []
    for value in args.source:
        label, separator, directory = value.partition("=")
        if not separator or not label or not directory:
            parser.error("--source requires LABEL=DSH_HOME")
        sources.append({"label": label, "root": str((Path(directory) / "sessions").resolve(strict=True))})
    if not sources:
        sources = discover_sources(runtime)
    if not sources and args.source:
        parser.error("no Bot session sources; use --source LABEL=DSH_HOME")
    if not 1 <= args.port <= 65535 or not 100 <= args.poll_ms <= 60000:
        parser.error("invalid port or poll interval")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    patch = home / "qq-trace.patch.json"
    entries = [
        {"id": "session-persistence-jsonl", "disabled": True},
        {"insert": [
            {"id": "v2-qq-trace-persistence", "name": str(ROOT / "dsh/plugins/qq-trace/persistence.mjs"),
             "config": {"root": str(home / "sessions"), "sources": sources,
                        **({"discovery": {"runtimeRoot": str(runtime)}} if not args.source else {})}},
            {"id": "v2-qq-trace", "name": str(ROOT / "dsh/plugins/qq-trace/web/index.mjs"),
             "config": {"pollMs": args.poll_ms}},
            {"id": "v2-subscription-catalog-export",
             "name": str(ROOT / "dsh/plugins/agent-profile/subscription-catalog-export.mjs")},
        ]},
    ]
    descriptor = os.open(patch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(entries, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if args.prepare_only:
        print(patch)
        return
    node = shutil.which("node")
    if not node:
        parser.error("node is required")
    binary = runtime / "dsh-runtime/node_modules/.bin/dsh"
    if not binary.is_file():
        parser.error("bootstrap the pinned DSH runtime first")
    env = dict(os.environ, DSH_HOME=str(home))
    os.execve(node, [node, str(binary), "--profile", "web", "--patch", str(patch),
                     "--host", "127.0.0.1", "--port", str(args.port), "--no-open"], env)


if __name__ == "__main__":
    main()
