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
node tests/native/agent-profile-decision-guide.mjs
node tests/native/agent-profile-scenario.mjs
node tests/native/agent-profile-web-boundaries.mjs
node tests/native/agent-profile-owner-sandbox.mjs
node tests/native/group-execution-boundaries.mjs
node tests/native/agent-profile-owner-host.mjs
"$repo_root/.runtime/astrbot/.venv/bin/python" -m pytest -q \
  tests/native/test_agent_profile_startup.py tests/native/test_agent_profile_owner_startup.py tests/native/test_agent_profile_owner_host_acp.py tests/native/test_agent_profile_acp.py \
  tests/native/test_agent_model_defaults.py tests/native/test_agent_guide_hot_reload.py \
  tests/native/test_agent_profile_compaction_acp.py \
  tests/native/test_agent_profile_research.py \
  tests/native/test_agent_research_limits.py \
  tests/native/test_agent_web_egress.py \
  tests/native/test_agent_profile_cancel.py \
  tests/native/test_agent_profile_recovery.py \
  tests/native/test_agent_profile_mcp.py tests/test_persona_state.py tests/test_trust_domain.py \
  tests/native/test_owner_volume.py \
  tests/test_group_volume.py tests/native/test_group_sandbox_runtime.py tests/native/test_group_sandbox_acp.py \
  tests/contracts/test_agent_profile_runtime.py
"$repo_root/.runtime/astrbot/.venv/bin/python" -m pytest -q \
  tests/test_runtime_audit.py tests/contracts/test_runtime_audit_send.py \
  tests/test_agent_service.py tests/test_agent_backup.py tests/test_agent_health.py \
  tests/test_delivery_error.py tests/test_turn_error.py tests/test_reply_delivery.py \
  tests/contracts/test_turn_error_lifecycle.py
"$script_dir/check-phase4.sh"
