#!/usr/bin/env python3
"""Allow this deployment's named content plugin in the exact observed bundle.

The user explicitly authorized this one-entry change. This is not a general
plugin policy change and refuses unknown NapCat bundles.
"""
import argparse
import hashlib
import os
from pathlib import Path

ORIGINAL_SHA256 = 'fe9a95075ca4f56ca1a7e0bd0902b93b68426401c006de5dc169a66656ddea04'
BEFORE = b'  "napcat-plugin-qce"\n]), ime = ['
AFTER = b'  "napcat-plugin-qce",\n  "napcat-plugin-v2-content"\n]), ime = ['


def patched(source):
    if hashlib.sha256(source).hexdigest() == ORIGINAL_SHA256:
        if source.count(BEFORE) != 1:
            raise ValueError('NapCat plugin allowlist anchor mismatch')
        return source.replace(BEFORE, AFTER, 1)
    if source.count(AFTER) == 1:
        original = source.replace(AFTER, BEFORE, 1)
        if hashlib.sha256(original).hexdigest() == ORIGINAL_SHA256:
            return source
    raise ValueError('Unreviewed NapCat bundle; refusing patch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    source = args.bundle.read_bytes()
    target = patched(source)
    if target == source:
        print('Exact NapCat plugin allowance already installed')
        return
    with args.backup.open('xb') as stream:
        stream.write(source)
        stream.flush()
        os.fsync(stream.fileno())
    temporary = args.bundle.with_name('.napcat-v2-content-tmp.mjs')
    temporary.write_bytes(target)
    os.chmod(temporary, args.bundle.stat().st_mode & 0o777)
    os.replace(temporary, args.bundle)
    print('Installed single named plugin allowance; original bundle backed up')


if __name__ == '__main__':
    main()
