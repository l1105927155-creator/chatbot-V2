#!/usr/bin/env bash
# Foreground launch: credentials are inherited, never written by this script.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
: "${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY in the process environment}"
: "${V2_QQ_ALLOWED_CONVERSATIONS:?Set the admitted QQ conversations explicitly}"
: "${V2_DSH_PROVIDER:?Set the verified DSH provider}"
: "${V2_DSH_MODEL:?Set the verified DSH model}"
export V2_QQ_OWNERS="${V2_QQ_OWNERS:-[]}"
export V2_MCP_PORT="${V2_MCP_PORT:-6210}"
export LC_MESSAGES=C
command -v node >/dev/null || { echo 'Node.js must be available on PATH' >&2; exit 1; }
python_bin="$repo_root/.runtime/astrbot/.venv/bin/python"
[[ -x "$python_bin" && -x "$repo_root/.runtime/dsh-runtime/node_modules/.bin/dsh" ]] || { echo 'Run the pinned bootstrap and dependency setup first' >&2; exit 1; }
cd "$repo_root/.runtime/astrbot"
exec "$python_bin" main.py
