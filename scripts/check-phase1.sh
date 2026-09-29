#!/usr/bin/env bash
# Run Phase 1 checks only against the exact AstrBot checkout in upstream.lock.json.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
runtime_dir="${ASTRBOT_SOURCE:-${repo_root}/.runtime/astrbot}"
lock_file="${repo_root}/upstream.lock.json"

[[ -d "$runtime_dir" ]] || { printf 'Missing AstrBot runtime: %s\n' "$runtime_dir" >&2; exit 1; }
runtime_dir="$(realpath -e -- "$runtime_dir")"
git_root="$(git -C "$runtime_dir" rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$git_root" && "$(realpath -e -- "$git_root")" == "$runtime_dir" ]] || {
  printf 'AstrBot runtime is not an independent Git checkout: %s\n' "$runtime_dir" >&2
  exit 1
}

mapfile -t pin < <(python3 - "$lock_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as file:
    locked = json.load(file)["astrbot"]
print(locked["ref"])
print(locked["tag_object_sha"])
PY
)
[[ ${#pin[@]} -eq 2 ]] || { printf 'Invalid AstrBot lock file.\n' >&2; exit 1; }

tag_ref="refs/v2-bootstrap/tags/${pin[0]}"
actual_tag="$(git -C "$runtime_dir" rev-parse --verify "$tag_ref" 2>/dev/null || true)"
tag_type="$(git -C "$runtime_dir" cat-file -t "$tag_ref" 2>/dev/null || true)"
[[ "$tag_type" == tag && "$actual_tag" == "${pin[1]}" ]] || {
  printf 'AstrBot tag object does not match upstream.lock.json.\n' >&2
  exit 1
}
expected_commit="$(git -C "$runtime_dir" rev-parse "${tag_ref}^{commit}")"
actual_commit="$(git -C "$runtime_dir" rev-parse --verify HEAD)"
[[ "$actual_commit" == "$expected_commit" ]] || {
  printf 'AstrBot HEAD is not the pinned tag commit.\n' >&2
  exit 1
}
if ! git -C "$runtime_dir" diff --quiet || ! git -C "$runtime_dir" diff --cached --quiet; then
  printf 'AstrBot tracked source has local changes.\n' >&2
  exit 1
fi

python_bin="${runtime_dir}/.venv/bin/python"
[[ -x "$python_bin" ]] || { printf 'Missing AstrBot virtual environment Python.\n' >&2; exit 1; }
"$python_bin" - <<'PY'
import importlib.util
import sys

if sys.version_info < (3, 12):
    raise SystemExit("AstrBot v4.28.2 requires Python 3.12 or newer")
if importlib.util.find_spec("pytest") is None:
    raise SystemExit("Install pytest and pytest-asyncio in the AstrBot virtual environment")
if importlib.util.find_spec("pytest_asyncio") is None:
    raise SystemExit("Install pytest-asyncio in the AstrBot virtual environment")
PY

printf 'Checking AstrBot %s at %s\n' "${pin[0]}" "$actual_commit"
cd "$repo_root"
ASTRBOT_SOURCE="$runtime_dir" ASTRBOT_ROOT="$runtime_dir" \
  "$python_bin" -m pytest -q \
  tests/test_bootstrap_isolation.py \
  tests/test_ai_gate.py \
  tests/contracts/test_platform_message_history_paths.py
