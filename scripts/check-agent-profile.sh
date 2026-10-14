#!/usr/bin/env bash
# Current Agent Profile gate: pinned source, native mechanisms, host state, and V2 integration.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"
python3 scripts/verify-dsh-source.py upstream.lock.json \
  .runtime/dsh-source-full.zip .runtime/dsh-source
node tests/native/agent-profile-foundation.mjs
node tests/native/agent-profile-compaction.mjs
node tests/native/agent-profile-persona-lifecycle.mjs
node tests/native/agent-profile-scenario.mjs
node tests/native/agent-profile-owner-sandbox.mjs
"$repo_root/.runtime/astrbot/.venv/bin/python" -m pytest -q \
  tests/native/test_agent_profile_startup.py tests/native/test_agent_profile_owner_startup.py tests/native/test_agent_profile_owner_acp.py tests/native/test_agent_profile_acp.py \
  tests/native/test_agent_profile_compaction_acp.py \
  tests/native/test_agent_profile_mcp.py tests/test_persona_state.py tests/test_trust_domain.py \
  tests/contracts/test_agent_profile_runtime.py
"$script_dir/check-phase4.sh"
