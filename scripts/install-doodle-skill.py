#!/usr/bin/env python3
"""Copy doodle skill and pinned Rough.js CJS bundle into ONE verified mounted group volume."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'dsh/plugins/agent-profile'))
from group_volume import verified_group_workspace  # noqa: E402


def install(workspace: Path, bundle: Path) -> Path:
    workspace = workspace.absolute()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError('workspace must be a real mounted group directory')
    fd = verified_group_workspace(workspace)
    import os
    os.close(fd)
    if workspace.name != 'workspace' or workspace.parent.parent.name != 'group-storage':
        raise ValueError('not a V2 group-storage workspace')
    source = REPO / 'dsh/skills/doodle'
    if not Path('/usr/bin/node').is_file():
        raise RuntimeError('group sandbox needs Node at /usr/bin/node')
    if bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_size < 10000:
        raise ValueError('expected a local non-symlink Rough.js 4.6.6 CJS bundle')
    package_file = bundle.parent.parent / 'package.json'
    if not package_file.is_file():
        raise ValueError('Rough.js package.json is required to verify the pinned version')
    package = json.loads(package_file.read_text(encoding='utf-8'))
    if package.get('name') != 'roughjs' or package.get('version') != '4.6.6':
        raise ValueError('Rough.js dependency must be version 4.6.6')
    root = workspace / 'skills'
    if root.is_symlink():
        raise ValueError('unsafe skills directory')
    root.mkdir(mode=0o700, exist_ok=True)
    if not root.is_dir():
        raise ValueError('skills must be a directory')
    target = root / 'doodle'
    if target.exists() or target.is_symlink():
        raise FileExistsError('skills/doodle already exists: preserve member changes; no overwrite')
    target.mkdir(mode=0o700)
    try:
        for name in ('SKILL.md', 'README.md', 'render.cjs', 'example.json'):
            shutil.copyfile(source / name, target / name)
        vendor = target / 'vendor'
        vendor.mkdir(mode=0o700)
        shutil.copyfile(bundle, vendor / 'rough.cjs.js')
    except Exception:
        shutil.rmtree(target)
        raise
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(install(args.workspace, args.bundle))
    except (ValueError, FileExistsError, OSError, RuntimeError) as exc:
        parser.exit(1, f'doodle: {exc}\n')
