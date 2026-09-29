#!/usr/bin/env bash
# Verify the pinned AstrBot baseline and the Phase 2 journal contract.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
runtime_dir="${ASTRBOT_SOURCE:-${repo_root}/.runtime/astrbot}"

"${script_dir}/check-phase1.sh"
runtime_dir="$(realpath -e -- "$runtime_dir")"
cd "$repo_root"
ASTRBOT_SOURCE="$runtime_dir" ASTRBOT_ROOT="$runtime_dir" \
  "$runtime_dir/.venv/bin/python" -m pytest -q \
  tests/test_journal.py \
  tests/contracts/test_journal_plugin.py
