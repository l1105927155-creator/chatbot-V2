#!/usr/bin/env python3
"""Verify V2's reused DSH tree against its locked official source archive."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import zipfile


def verify(lock_path, archive_path, source_path):
    entry = json.loads(Path(lock_path).read_text(encoding='utf-8'))['deepseek_harness']
    archive, source = Path(archive_path), Path(source_path)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['source_archive_sha256']:
        raise ValueError('DSH source archive differs from the locked SHA256')
    prefix = f"deepseek-harness-{entry['ref']}/"
    expected = set()
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            if not info.filename.startswith(prefix):
                raise ValueError('Unexpected DSH source archive root')
            relative = info.filename[len(prefix):]
            if not relative or info.is_dir():
                continue
            if '..' in PurePosixPath(relative).parts:
                raise ValueError('Unsafe DSH archive member')
            expected.add(relative)
            target = source / relative
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                if not target.is_symlink() or os.fsencode(os.readlink(target)) != zipped.read(info):
                    raise ValueError(f'DSH source link differs from pinned archive: {relative}')
                continue
            if target.is_symlink() or not target.is_file() or target.read_bytes() != zipped.read(info):
                raise ValueError(f'DSH source differs from pinned archive: {relative}')
    extras = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file() or p.is_symlink()} - expected
    extras = {p for p in extras if p != '.v2-pinned-commit' and not ('__pycache__' in PurePosixPath(p).parts and p.endswith('.pyc'))}
    if extras:
        raise ValueError(f'Unverified DSH source files: {sorted(extras)[:3]}')


if __name__ == '__main__':
    try:
        verify(*sys.argv[1:])
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        raise SystemExit(str(exc))
