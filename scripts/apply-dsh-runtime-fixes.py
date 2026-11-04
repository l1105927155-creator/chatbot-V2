#!/usr/bin/env python3
"""Apply the audited ACP fix to the exact pinned runtime; reject other bytes."""
from pathlib import Path
import hashlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA256 = 'c08ed51ebe53b7b45ca8f475630afc0271bf4ccb8b53f13b456fe2b4568a61b3'
OLD_IMPORT = 'import { ReasoningEffortId, createUserMessage, errorChain } from "@deepseek-ai/dsh-llm";'
NEW_IMPORT = OLD_IMPORT.replace('ReasoningEffortId,', 'LlmError, ReasoningEffortId,')
OLD_GUARD = '\t\t\tif (!this.hasResolvedState) throw error;'
NEW_GUARD = '''\t\t\t// Missing persisted routes must remain selectable for explicit repair.
\t\t\t// Keep all other first-resolution failures visible.
\t\t\tif (!this.hasResolvedState && !(error instanceof LlmError
\t\t\t\t&& ["UNKNOWN_MODEL", "NO_ADAPTER"].includes(error.failure.code))) throw error;'''


def apply(runtime):
    lock = json.loads((ROOT / 'upstream.lock.json').read_text())['deepseek_harness']
    if lock['ref'] != '4878cdabd87d4041bdaff61d04c966883b9fd07a':
        raise RuntimeError('reassess ACP compatibility fix for the new pinned version')
    package = runtime / 'node_modules/@deepseek-ai/dsh-acp/lib/index.js'
    current = package.read_text()
    original = current.replace(NEW_IMPORT, OLD_IMPORT).replace(NEW_GUARD, OLD_GUARD)
    if hashlib.sha256(original.encode()).hexdigest() != BASE_SHA256:
        raise RuntimeError('ACP bytes differ from the audited pinned runtime')
    if original.count(OLD_IMPORT) != 1 or original.count(OLD_GUARD) != 1:
        raise RuntimeError('ACP patch anchors differ')
    fixed = original.replace(OLD_IMPORT, NEW_IMPORT).replace(OLD_GUARD, NEW_GUARD)
    if current == fixed:
        return False
    if current != original:
        raise RuntimeError('ACP is partially patched')
    temporary = package.with_name('.index.js.v2-acp-fix-tmp')
    with temporary.open('x') as stream:
        stream.write(fixed)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(package.stat().st_mode & 0o777)
    os.replace(temporary, package)
    return True


if __name__ == '__main__':
    if len(sys.argv) > 2:
        raise SystemExit('usage: apply-dsh-runtime-fixes.py [runtime_directory]')
    runtime = Path(sys.argv[1]) if len(sys.argv) == 2 else ROOT / '.runtime/dsh-runtime'
    print('V2 ACP compatibility fix:', 'applied' if apply(runtime) else 'already applied')
