#!/usr/bin/env bash
# Install only the DeepSeek Harness revision and runtime locked by chatbot-V2.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${repo_root}/.runtime"
source_dir="${runtime_dir}/dsh-source"
install_dir="${runtime_dir}/dsh-runtime"
venv_python="${runtime_dir}/astrbot/.venv/bin/python"

for binary in python3 curl unzip npm; do
  command -v "$binary" >/dev/null || { printf '%s is required.\n' "$binary" >&2; exit 1; }
done
[[ -x "$venv_python" ]] || { printf 'Bootstrap AstrBot and its .venv first: %s\n' "$venv_python" >&2; exit 1; }

mapfile -t pinned < <(python3 - "$repo_root/upstream.lock.json" <<'PY'
import json
import sys
entry = json.load(open(sys.argv[1], encoding="utf-8"))["deepseek_harness"]
print(entry["repository"])
print(entry["ref"])
print(entry["version"])
print(entry["source_archive_sha256"])
PY
)
[[ "${#pinned[@]}" == 4 ]] || { printf 'Invalid DSH lock entry.\n' >&2; exit 1; }
[[ "${pinned[0]}" == "https://github.com/deepseek-ai/deepseek-harness.git" ]] || {
  printf 'Unexpected DSH repository in lock.\n' >&2; exit 1;
}
[[ "${pinned[1]}" =~ ^[0-9a-f]{40}$ ]] || { printf 'DSH lock ref must be a commit SHA.\n' >&2; exit 1; }
[[ "${pinned[2]}" == "0.2.0-rc.1" ]] || { printf 'Unexpected DSH runtime version.\n' >&2; exit 1; }

mkdir -p "$runtime_dir" "$install_dir"
archive_path="${runtime_dir}/dsh-source-full.zip"
if [[ ! -f "$archive_path" ]]; then
  temp_archive="$(mktemp "${runtime_dir}/dsh-download.XXXXXX")"
  trap 'rm -f -- "$temp_archive"' EXIT
  curl --fail --location --retry 3 --silent --show-error \
    "https://codeload.github.com/deepseek-ai/deepseek-harness/zip/${pinned[1]}" \
    --output "$temp_archive"
  mv -- "$temp_archive" "$archive_path"
fi
python3 - "$archive_path" "${pinned[3]}" <<'PY'
import hashlib
from pathlib import Path
import sys
if hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != sys.argv[2]:
    raise SystemExit("DSH archive SHA256 differs from the lock")
PY
if [[ -e "$source_dir" ]]; then
  [[ -d "$source_dir" && ! -L "$source_dir" ]] || { printf 'Unsafe DSH source path.\n' >&2; exit 1; }
else
  temp_dir="$(mktemp -d "${runtime_dir}/dsh-bootstrap.XXXXXX")"
  trap 'rm -rf -- "$temp_dir"' EXIT
  unzip -tq "$archive_path" >/dev/null
  unzip -q "$archive_path" -d "$temp_dir"
  extracted="$temp_dir/deepseek-harness-${pinned[1]}"
  [[ -d "$extracted/python/sdk" ]] || { printf 'Pinned DSH SDK source is missing.\n' >&2; exit 1; }
  mv -- "$extracted" "$source_dir"
  printf '%s\n' "${pinned[1]}" > "$source_dir/.v2-pinned-commit"
fi
python3 "$repo_root/scripts/verify-dsh-source.py" "$repo_root/upstream.lock.json" "$archive_path" "$source_dir"

cp -- "$repo_root/dsh/runtime/package.json" "$repo_root/dsh/runtime/package-lock.json" "$install_dir/"
npm ci --prefix "$install_dir" --omit=dev
"$venv_python" -m pip install --no-deps "$source_dir/python/sdk"
"$venv_python" - "$install_dir" "${pinned[2]}" <<'PY'
import json
import pathlib
import sys
installed = pathlib.Path(sys.argv[1]) / "node_modules/@deepseek-ai/dsh/package.json"
if json.loads(installed.read_text(encoding="utf-8"))["version"] != sys.argv[2]:
    raise SystemExit("Installed DSH version differs from the V2 lock")
from deepseek_harness import DeepSeekHarness  # noqa: F401
print(f"V2 DSH runtime ready: {sys.argv[2]}")
PY
