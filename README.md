# chatbot-V2

A clean integration project for **AstrBot + DeepSeek Harness (DSH)**.

The repository intentionally does **not** copy the old `chatbot` codebase or vendor full upstream AstrBot/DSH sources. Upstream versions are pinned in [`upstream.lock.json`](upstream.lock.json); this repository will contain only the integration code, plugins, MCP boundary, DSH profile/policies, tests, and bootstrap/deployment assets owned by this project.

Before development, read:

- [`PLAN.md`](PLAN.md) — architecture baseline and phased roadmap.
- [`AGENTS.md`](AGENTS.md) — repository development constraints.
- [`docs/phases/phase-1.md`](docs/phases/phase-1.md) — completed AstrBot baseline.
- [`docs/phases/phase-2.md`](docs/phases/phase-2.md) — completed journal scope.
- [`docs/phases/phase-2.1.md`](docs/phases/phase-2.1.md) — completed ordering contract.
- [`docs/phases/phase-3.md`](docs/phases/phase-3.md) — current DSH chat-loop scope.

Current status: **Phases 1, 2, 2.1, and 3 completed; Phase 4 has not started.** The pinned AstrBot baseline
and V2-owned canonical journal are verified with controlled group/private QQ
messages, including DSH replies and restart continuity. Concurrent event-bus tests establish that `journal_id` preserves
V2 capture-callback order for one client. Phase 3 connects the pinned DSH
runtime to unhandled AstrBot conversations using persistent session mapping and
journal cursors. See the [Phase 1 findings](docs/phases/phase-1-findings.md),
[capture findings](docs/phases/phase-2-capture-findings.md), and
[journal verification](docs/phases/phase-2-journal-findings.md), and
[ordering findings](docs/phases/phase-2.1-findings.md), and
[DSH chat-loop findings](docs/phases/phase-3-findings.md).

The journal records OneBot inbound messages and platform-confirmed Bot
`message_sent` feedback. Phase 3 adds the V2-owned DSH router; MCP is not
integrated yet.

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

## Phase 2: canonical QQ journal

Link the V2 journal plugin into the isolated AstrBot runtime:

```bash
repo_root="$(pwd)"
ln -sfn "$repo_root/astrbot-plugins/v2_journal" \
  "$repo_root/.runtime/astrbot/data/plugins/v2_journal"
```

Include `v2_journal` in V2's `plugin_set`. Configure V2's own NapCat
WebSocket client with `reportSelfMessage=true` and array message format.
The V2 aiocqhttp adapter must receive that client on its own reverse
WebSocket port. Keep `platform_settings.ignore_bot_self_message=true`: the
journal uses aiocqhttp before hooks for ordinary inbound `message` and
confirmed outbound `message_sent` events. This records inbound events before
AstrBot's conversion and pipeline filters. A private
outbound event's `target_id` identifies its peer.

The SQLite journal lives in AstrBot's ignored runtime
`data/plugin_data/v2_journal/journal.sqlite3`. The Python read API is in
[`journal.py`](astrbot-plugins/v2_journal/journal.py): `recent`, `after`,
`before`, and `by_message_id`. Use a `Conversation` with the V2 platform ID,
Bot ID, and `group:<id>` or `private:<peer_qq>` key. No MCP endpoint is exposed.

Run the pinned source, capture-order, storage, and Phase 1 regression checks:

```bash
./scripts/check-phase2.sh
```

The [capture experiment](docs/phases/phase-2-capture-findings.md) documents
the selected boundary and its constraints. The
[journal verification](docs/phases/phase-2-journal-findings.md) records the
live QQ ordering, message IDs, restart check, and remaining limits.

## Phase 3: DSH chat loop

Bootstrap the DSH commit and npm version pinned in `upstream.lock.json` after
creating the isolated AstrBot `.venv`:

```bash
./scripts/bootstrap-dsh.sh
```

This installs DSH under `.runtime/dsh-runtime`, its pinned Python SDK source
under `.runtime/dsh-source`, and the SDK into the V2 AstrBot environment.
The official commit archive SHA256 and extracted source files are verified;
`npm ci` uses the tracked dependency lock.
Keep `DEEPSEEK_API_KEY` in the V2 process environment; do not put credentials
in this repository. The V2 profile in `dsh/profile` disables local shell
tools and supplies only the conversational instructions and journal delta.

Set `V2_QQ_ALLOWED_CONVERSATIONS` for the QQ conversations admitted to DSH,
for example `group:<group_id>,private:<peer_qq>`. An unset or empty value
leaves the router idle. This keeps a test connection from replying in other
groups.

Link `astrbot-plugins/v2_dsh_router` into V2's
`.runtime/astrbot/data/plugins` and include `v2_dsh_router` in the V2
`plugin_set`, after `v2_journal` has been configured. The router owns a small
SQLite mapping under `data/plugin_data/v2_dsh_router`. Each ordinary QQ wake
is bounded at its own journal row, and each conversation is serialized. A
failed or interrupted DSH turn leaves a durable pending marker because the
stdio transport cannot prove whether a lost request was accepted; inspect that
conversation before manually resolving it.

Run the pinned-source and Phase 3 contracts with:

```bash
./scripts/check-phase3.sh
```

The router uses the pinned native ACP `session/new`, `session/resume`, and
`session/prompt` APIs through the official Python JSON-RPC transport. The
default SDK create-session entry point cannot resume an existing session.
No upstream core patch is required. See the
[Phase 3 findings](docs/phases/phase-3-findings.md) for live evidence and limits.
