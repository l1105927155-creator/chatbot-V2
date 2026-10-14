#!/usr/bin/env bash
# Phase 5 native authorization and all preceding integration contracts.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
"${script_dir}/check-phase4.sh"
"${script_dir}/check-phase5-native.sh"
cd "$repo_root"
"${repo_root}/.runtime/astrbot/.venv/bin/python" -m pytest -q tests/test_turn_source.py
