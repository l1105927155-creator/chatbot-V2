#!/usr/bin/env bash
# Verify pinned native mechanisms without starting models or QQ integration.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
python3 "$repo_root/scripts/verify-dsh-source.py" \
  "$repo_root/upstream.lock.json" "$repo_root/.runtime/dsh-source-full.zip" "$repo_root/.runtime/dsh-source"
cd "$repo_root"
"$repo_root/.runtime/astrbot/.venv/bin/python" - <<'PY'
import json
from pathlib import Path
lock = json.loads(Path('upstream.lock.json').read_text())['deepseek_harness']
manifest = json.loads(Path('dsh/runtime/package-lock.json').read_text())['packages']
installed = json.loads(Path('.runtime/dsh-runtime/node_modules/.package-lock.json').read_text())['packages']
packages = ('dsh', 'dsh-tools', 'dsh-fs-local', 'dsh-fs-sandbox', 'dsh-bash-sandbox',
            'dsh-sandbox-local', 'dsh-mcp-client', 'cordis', 'dsh-system-prompt',
            'dsh-scope', 'dsh-llm', 'dsh-session-projection', 'dsh-sandbox-policy',
            'dsh-subprocess-local')
for package in packages:
    key = 'node_modules/@deepseek-ai/' + package
    path = Path('.runtime/dsh-runtime') / key / 'package.json'
    expected = manifest[key]
    if json.loads(path.read_text())['version'] != expected['version']:
        raise SystemExit(f'{package} differs from the committed npm lock')
    for field in ('version', 'resolved', 'integrity'):
        if installed.get(key, {}).get(field) != expected.get(field):
            raise SystemExit(f'{package} {field} differs from the committed npm lock')
if manifest['node_modules/@deepseek-ai/dsh']['version'] != lock['version']:
    raise SystemExit('committed npm runtime differs from upstream.lock.json')
PY
"$repo_root/.runtime/astrbot/.venv/bin/python" -m pytest -q tests/native
