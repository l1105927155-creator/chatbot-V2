#!/usr/bin/env bash
# Verify the V2 DSH integration against the pinned AstrBot and DSH runtimes.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
"${script_dir}/check-phase2.sh"
python3 "${script_dir}/verify-dsh-source.py" "${repo_root}/upstream.lock.json" \
  "${repo_root}/.runtime/dsh-source-full.zip" "${repo_root}/.runtime/dsh-source"

"${repo_root}/.runtime/astrbot/.venv/bin/python" - \
  "${repo_root}/upstream.lock.json" \
  "${repo_root}/.runtime/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json" <<'PY'
import json
import pathlib
import sys

lock = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["deepseek_harness"]
installed = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if installed["version"] != lock["version"]:
    raise SystemExit("V2 DSH runtime version differs from the lock")
from deepseek_harness import DeepSeekHarness  # noqa: F401
PY

cd "$repo_root"
"${repo_root}/.runtime/astrbot/.venv/bin/python" -m pytest -q \
  tests/test_dsh_state.py tests/test_dsh_client.py \
  tests/test_dsh_route.py tests/test_dsh_service.py tests/test_dsh_bootstrap.py \
  tests/contracts/test_dsh_routing_runtime.py
