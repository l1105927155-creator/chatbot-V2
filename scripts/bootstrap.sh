#!/usr/bin/env bash
# Bootstrap the pinned AstrBot source without vendoring it into this repository.
# DSH intentionally is not installed or started by this script.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [--runtime-dir DIR] [--lock-file FILE]

Checks out the AstrBot version pinned in upstream.lock.json. The default runtime
directory is .runtime/astrbot. Existing local changes are never overwritten.
EOF
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
runtime_dir="${repo_root}/.runtime/astrbot"
lock_file="${repo_root}/upstream.lock.json"

while (($#)); do
  case "$1" in
    --runtime-dir)
      (($# >= 2)) || { usage >&2; exit 2; }
      runtime_dir="$2"
      shift 2
      ;;
    --lock-file)
      (($# >= 2)) || { usage >&2; exit 2; }
      lock_file="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v git >/dev/null || { printf 'git is required.\n' >&2; exit 1; }
command -v python3 >/dev/null || { printf 'python3 is required to read %s.\n' "$lock_file" >&2; exit 1; }
[[ -f "$lock_file" ]] || { printf 'Lock file does not exist: %s\n' "$lock_file" >&2; exit 1; }

mapfile -t lock_values < <(python3 - "$lock_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as file:
    lock = json.load(file)

try:
    astrbot = lock["astrbot"]
    for key in ("repository", "ref", "tag_object_sha"):
        value = astrbot[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"astrbot.{key} must be a non-empty string")
        print(value)
except (KeyError, ValueError) as error:
    raise SystemExit(f"Invalid AstrBot lock entry: {error}")
PY
)

if ((${#lock_values[@]} != 3)); then
  printf 'Invalid AstrBot lock entry in %s.\n' "$lock_file" >&2
  exit 1
fi

upstream_url="${lock_values[0]}"
upstream_ref="${lock_values[1]}"
expected_tag_sha="${lock_values[2]}"

if [[ ! "$upstream_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || [[ ! "$expected_tag_sha" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Invalid AstrBot ref or tag_object_sha in %s.\n' "$lock_file" >&2
  exit 1
fi

created_runtime=false
if [[ -e "$runtime_dir" || -L "$runtime_dir" ]]; then
  [[ ! -L "$runtime_dir" ]] || {
    printf 'Runtime directory must not be a symbolic link: %s\n' "$runtime_dir" >&2
    exit 1
  }
  [[ -d "$runtime_dir" ]] || {
    printf 'Runtime path is not a directory: %s\n' "$runtime_dir" >&2
    exit 1
  }
else
  mkdir -p "$(dirname -- "$runtime_dir")"
  git init -q "$runtime_dir"
  created_runtime=true
fi

runtime_dir="$(realpath -e -- "$runtime_dir")"
git_toplevel="$(git -C "$runtime_dir" rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$git_toplevel" ]] || {
  printf 'Runtime directory is not a Git worktree: %s\n' "$runtime_dir" >&2
  exit 1
}
git_toplevel="$(realpath -e -- "$git_toplevel")"
[[ "$git_toplevel" == "$runtime_dir" ]] || {
  printf 'Runtime directory must be the Git worktree root; refusing parent-repository access: %s\n' "$runtime_dir" >&2
  exit 1
}

# Runtime data such as .venv and data/ may be untracked. Only tracked or staged
# upstream-source changes are unsafe to preserve across a checkout.
if ! git -C "$runtime_dir" diff --quiet || ! git -C "$runtime_dir" diff --cached --quiet; then
  printf 'Runtime checkout has tracked or staged changes; refusing to modify it: %s\n' "$runtime_dir" >&2
  exit 1
fi

existing_origin="$(git -C "$runtime_dir" config --get remote.origin.url || true)"
if [[ -z "$existing_origin" ]]; then
  if [[ "$created_runtime" == true ]]; then
    git -C "$runtime_dir" remote add origin "$upstream_url"
  else
    printf 'Existing runtime Git worktree has no origin; refusing to adopt it: %s\n' "$runtime_dir" >&2
    exit 1
  fi
elif [[ "$existing_origin" != "$upstream_url" ]]; then
  printf 'Runtime origin differs from the lock; refusing to change it.\nExpected: %s\nActual:   %s\n' "$upstream_url" "$existing_origin" >&2
  exit 1
fi

# Store the fetched tag under a V2-owned ref, preserving any user tag refs.
bootstrap_tag_ref="refs/v2-bootstrap/tags/${upstream_ref}"
git -C "$runtime_dir" fetch --quiet --depth=1 --no-tags origin "+refs/tags/${upstream_ref}:${bootstrap_tag_ref}"

actual_tag_sha="$(git -C "$runtime_dir" rev-parse --verify "$bootstrap_tag_ref")"
tag_type="$(git -C "$runtime_dir" cat-file -t "$bootstrap_tag_ref")"
if [[ "$tag_type" != "tag" ]] || [[ "$actual_tag_sha" != "$expected_tag_sha" ]]; then
  printf 'Pinned tag verification failed for %s.\nExpected annotated tag object: %s\nActual object: %s (%s)\n' \
    "$upstream_ref" "$expected_tag_sha" "$actual_tag_sha" "$tag_type" >&2
  exit 1
fi

pinned_commit="$(git -C "$runtime_dir" rev-parse --verify "${bootstrap_tag_ref}^{commit}")"
current_commit="$(git -C "$runtime_dir" rev-parse --verify HEAD 2>/dev/null || true)"

if [[ "$current_commit" != "$pinned_commit" ]]; then
  git -C "$runtime_dir" checkout --detach --quiet "$pinned_commit"
fi

if [[ -n "$(git -C "$runtime_dir" status --porcelain --untracked-files=all)" ]]; then
  printf 'AstrBot is pinned at %s in %s; local changes were preserved.\n' "$pinned_commit" "$runtime_dir"
else
  printf 'AstrBot is pinned at %s in %s.\n' "$pinned_commit" "$runtime_dir"
fi
