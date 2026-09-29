# chatbot-V2

A clean integration project for **AstrBot + DeepSeek Harness (DSH)**.

The repository intentionally does **not** copy the old `chatbot` codebase or vendor full upstream AstrBot/DSH sources. Upstream versions are pinned in [`upstream.lock.json`](upstream.lock.json); this repository will contain only the integration code, plugins, MCP boundary, DSH profile/policies, tests, and bootstrap/deployment assets owned by this project.

Before development, read:

- [`PLAN.md`](PLAN.md) — architecture baseline and phased roadmap.
- [`AGENTS.md`](AGENTS.md) — repository development constraints.
- [`docs/phases/phase-1.md`](docs/phases/phase-1.md) — completed AstrBot baseline.
- [`docs/phases/phase-2.md`](docs/phases/phase-2.md) — current implementation scope.

Current status: **Phase 1 completed; Phase 2 ready for implementation.** The
pinned AstrBot checkout, LLM gate, deterministic command, and seven-path history
investigation are verified. Controlled QQ tests are recorded in the
[Phase 1 findings](docs/phases/phase-1-findings.md).

Phase 2 builds the V2-owned canonical journal. It starts with a live capture-boundary
experiment: test OneBot self-message feedback first, then investigate an AstrBot
unified send boundary only if platform feedback is unsuitable. Do not integrate
DSH or MCP yet. The target runtime is an Ubuntu host.

## Phase 1: bootstrap AstrBot

The pinned AstrBot checkout is intentionally kept outside tracked source. From
the repository root, run:

```bash
./scripts/bootstrap.sh
```

This fetches only the AstrBot tag specified in `upstream.lock.json`, verifies
its annotated tag object SHA, and checks it out at:

```text
.runtime/astrbot
```

The script is idempotent. It refuses tracked or staged changes at any commit,
and preserves untracked runtime data such as `.venv/`. It does not install or
start DSH.

### Install the Phase 1 plugins

After bootstrap, link the V2-owned plugins into AstrBot's runtime directory
before starting AstrBot:

```bash
repo_root="$(pwd)"
mkdir -p "$repo_root/.runtime/astrbot/data/plugins"
ln -sfn "$repo_root/astrbot-plugins/v2_ai_gate" \
  "$repo_root/.runtime/astrbot/data/plugins/v2_ai_gate"
ln -sfn "$repo_root/astrbot-plugins/v2_phase1_probe" \
  "$repo_root/.runtime/astrbot/data/plugins/v2_phase1_probe"
```

In AstrBot's V2 runtime configuration, keep `plugin_set: ["*"]` or explicitly
include `v2_ai_gate` and `v2_phase1_probe`. Before connecting QQ, set:

```text
provider_settings.enable = false
platform_settings.empty_mention_waiting = false
provider_ltm_settings.group_icl_enable = false
provider_ltm_settings.active_reply.enable = false
provider_ltm_settings.image_caption = false
provider_ltm_settings.group_message_history_enable = true
```

The gate blocks direct built-in LLM requests that bypass the main provider
setting. The remaining settings disable built-in conversation features for
pure @-mentions and group context, and enable existing group history for
Phase 1 coverage. A message consisting only of a wake prefix is still consumed
before the group history hook; see the
[Phase 1 findings](docs/phases/phase-1-findings.md).

The current gate is a **Phase 1 safety mechanism**, not a permanently frozen V2
design. Its unconditional `on_llm_request` stop also blocks future plugin-local
LLM use. Keep it unchanged through the Phase 2 journal work unless it directly
interferes with the capture experiment; reconsider its final scope only when a
later phase requires selected plugin-local LLM capability. Do not edit upstream
AstrBot source to install V2 integration code.

The test plugin exposes `/v2probe` for a direct deterministic reply and
`/v2push` to exercise `Context.send_message` back to the same conversation.

### Isolated development startup

Use a virtual environment inside the ignored runtime checkout. This keeps
Python packages and AstrBot's generated data separate from any old project:

```bash
cd .runtime/astrbot
python3 -c 'import sys; assert sys.version_info >= (3, 12), "AstrBot v4.28.2 needs Python 3.12+"'
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio
python main.py
```

On the first start, configure AstrBot through this checkout's WebUI or its
generated runtime configuration **before connecting a QQ adapter**. Give its
WebUI and OneBot listener ports V2-specific values that are not occupied by
another local bot process. Do not point it at an old project's data or
configuration directory. If both projects use the same NapCat account, add a
separate WebSocket client for V2 and leave the existing client in place.

The AstrBot source tag is fixed and verified. Its `requirements.txt` has version
ranges rather than a dependency lock, so a fresh install can resolve different
package versions. The isolated pipeline and history tests document behavior
observed with the dependencies installed for this Phase 1 checkout; exact
dependency reproduction remains unverified.

### Phase 1 checks

From the V2 repository root:

```bash
./scripts/check-phase1.sh
```

This command refuses a missing or mismatched AstrBot checkout and runs the
bootstrap, AI gate, and history checks against the pinned version. The
seven-path matrix and verification limits are in the
[Phase 1 findings](docs/phases/phase-1-findings.md).
