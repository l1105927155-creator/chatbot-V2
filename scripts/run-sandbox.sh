#!/usr/bin/env bash
# Start the isolated QQ simulator and its pinned AstrBot runtime.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"
exec python3 -m sandbox.runtime "$@"
