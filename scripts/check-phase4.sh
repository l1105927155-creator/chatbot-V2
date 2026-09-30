#!/usr/bin/env bash
# Verify the Phase 4 MCP boundary with the pinned V2 runtime.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
"${script_dir}/check-phase3.sh"
cd "$repo_root"
"${repo_root}/.runtime/astrbot/.venv/bin/python" - <<'PY'
from importlib.metadata import version
for package, expected in (("mcp", "1.30.0"), ("uvicorn", "0.54.0")):
    if version(package) != expected:
        raise SystemExit(f"V2 MCP runtime requires {package}=={expected}; install router requirements.txt")
PY
"${repo_root}/.runtime/astrbot/.venv/bin/python" -m pytest -q \
  tests/test_mcp_server.py tests/test_mcp_integration.py
