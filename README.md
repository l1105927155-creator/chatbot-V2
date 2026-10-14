# chatbot-V2

A clean integration project for **AstrBot + DeepSeek Harness (DSH)**.

The repository intentionally does **not** copy the old `chatbot` codebase or vendor full upstream AstrBot/DSH sources. Upstream versions are pinned in [`upstream.lock.json`](upstream.lock.json); this repository will contain only the integration code, plugins, MCP boundary, DSH profile/policies, tests, and bootstrap/deployment assets owned by this project.

Before development, read:

- [`PLAN.md`](PLAN.md) — architecture baseline and phased roadmap.
- [`docs/architecture/orchestration-capability-boundary.md`](docs/architecture/orchestration-capability-boundary.md) — frozen integration responsibility boundary.
- [`AGENTS.md`](AGENTS.md) — repository development constraints.
- [`docs/phases/phase-1.md`](docs/phases/phase-1.md) — completed AstrBot baseline.
- [`docs/phases/phase-2.md`](docs/phases/phase-2.md) — completed journal scope.
- [`docs/phases/phase-2.1.md`](docs/phases/phase-2.1.md) — completed ordering contract.
- [`docs/phases/phase-3.md`](docs/phases/phase-3.md) — completed DSH chat-loop scope.
- [`docs/phases/phase-3-remediation.md`](docs/phases/phase-3-remediation.md) — completed implementation-alignment work.
- [`docs/phases/phase-3-startup-validation.md`](docs/phases/phase-3-startup-validation.md) — completed Phase 3 follow-up.
- [`docs/phases/phase-4.md`](docs/phases/phase-4.md) — completed MCP capability phase.
- [`docs/architecture/agent-bot-runtime.md`](docs/architecture/agent-bot-runtime.md) — current Agent-Bot architecture.
- [`docs/architecture/dsh-version-decision.md`](docs/architecture/dsh-version-decision.md) — pinned-version decision.
- [`docs/phases/agent-profile-validation.md`](docs/phases/agent-profile-validation.md) — current implementation and evidence.

Current work rebuilds the Agent Profile around stable capabilities, dynamic persona and skill scenes, external Journal history, and independent trust domains. DSH remains pinned to the lock file; native plugin seams currently satisfy the verified mechanisms.

## V2 plugin documentation

| Plugin | Purpose |
| --- | --- |
| [V2 AI Gate](astrbot-plugins/v2_ai_gate/README.md) | Suppress AstrBot conversational LLM requests |
| [V2 Journal](astrbot-plugins/v2_journal/README.md) | Canonical platform-confirmed chat history |
| [V2 DSH Router](astrbot-plugins/v2_dsh_router/README.md) | DSH sessions, trust domains, scoped MCP and profile controls |
| [V2 Phase 1 Probe](astrbot-plugins/v2_phase1_probe/README.md) | Deterministic message-chain diagnostics |

## Current Agent Profiles

The router uses two independent ACP runtimes:

- Safe chat: all admitted groups and non-owner private chats; `v2-agent-group.patch.yml`, `.runtime/dsh-home-chat`, `.runtime/dsh-workspace-chat`, and `state-chat.sqlite3`. No generic host file or terminal tools.
- Owner operations: only exact configured owners in their own private chat; `v2-agent-owner.patch.yml`, `.runtime/dsh-home-owner`, `.runtime/dsh-workspace-owner`, and `state-owner.sqlite3`. Each tool call checks the current host-issued request, not remembered identity.

Keep `V2_DSH_PROVIDER`, `V2_DSH_MODEL`, credentials, and `V2_QQ_ALLOWED_CONVERSATIONS` in the process environment. Owner identities use `V2_QQ_OWNERS`, a JSON array of exact `platform_id`, `bot_id`, and `user_id` strings. An empty array disables the owner runtime. An owner speaking in a group still uses safe chat capabilities.

Owner Bash constrains the existing native dsh-subprocess scope and verifies cgroup limits before executing, so native timeout/cancellation retains control of descendants. It requires Linux, working user systemd scopes/cgroup limits, and `/usr/bin/bwrap`. It sees only essential read-only runtime files and `/workspace`; it has no network or inherited credential environment. Limits are 512 MiB memory, 64 tasks, one CPU, and a maximum 60-second foreground call. Background execution and escalation are unavailable. Missing infrastructure fails the tool rather than falling back to host execution. Owner home/workspace must be private 0700 directories owned by the runtime user; socket, device, FIFO, and multiply linked files are rejected in the workspace. Disk budgeting remains an acceptance gap.

Configured owners can select the current admitted conversation's runtime state:

```text
/v2agent persona default|analyst|companion|developer
/v2agent scene casual-chat|research|development
```

These deterministic commands wait for the current turn to finish, update private host state, and acknowledge through AstrBot. They do not invoke the model, replace the session, or grant tools. A command before the first ordinary message creates only the native session and its MCP binding. A pending uncertain turn blocks selection until recovery. Each normal input projects at most 100 recent entries and 32 KiB. Oversized entries are clearly marked, and scoped history tools can retrieve their complete originals. The stored cursor is the accepted projection boundary; omitted facts remain externally queryable in the canonical Journal. This bounds bootstrap/backlog context rather than deleting history.

The scene exposes only installed skill definitions. The repository provides chat-history, filesystem, and development; web-research remains a test fixture rather than a production capability. Skill discovery shares one native provider and falls back to shared polling when filesystem watcher capacity is exhausted. Loading development skills does not grant Bash to safe-chat sessions.

Run `scripts/check-agent-profile.sh` for pinned-source checks, native mechanisms, actual controlled ACP turns, owner isolation, and message-chain regressions. These checks use disposable data and scripted model adapters. Real-provider synthetic safe-chat and owner acceptance has passed. Authorized real QQ acceptance passed only for Bot 2869323076 / private:1105927155: persona and scene commands, model replies, deterministic probe visibility through Journal history, persistent session restart, native skill loading, and isolated workspace write/read. Temporary personal-account HTTP access and the V2 test process were closed after verification; existing QQ services were preserved. Production rollout, group QQ acceptance, workspace disk budgeting, and real-provider large-context stress remain open. Old `state.sqlite3` / Phase 5 sessions are not automatically migrated.

When V2 provider credentials are available in the environment, run the isolated real-model check explicitly:

```bash
.runtime/astrbot/.venv/bin/python scripts/check-agent-profile-live.py
.runtime/astrbot/.venv/bin/python scripts/check-agent-profile-live.py --domain owner
```

It creates disposable safe-chat sessions, checks three synthetic turns, observes successful native skill loading, changes persona, and resumes after a process restart. It never connects QQ. Its ignored `.runtime/agent-profile-live-result.json` records only verification metadata; an advertised model window is not evidence of a large-context stress test.

The following Phase 1–4 setup sections and Phase 5 documents describe the retained historical baseline. For current development, use the Agent Profile phase document above.

The journal records OneBot inbound messages and platform-confirmed Bot
`message_sent` feedback. Phase 3 adds the V2-owned DSH router; Phase 4 attaches scoped AstrBot MCP
capabilities to its native DSH sessions.

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
Set `V2_DSH_PROVIDER` and `V2_DSH_MODEL` explicitly in the V2 process
environment. For example, the live acceptance used `deepseek-official` and
`deepseek-v4-flash`; these are test selections, not structural profile defaults.
Keep provider credentials (for example `DEEPSEEK_API_KEY`) in the process
environment; do not put them in this repository. Startup initializes the ACP
transport without creating sessions or invoking
inference. Missing or malformed provider/model selection fails clearly; provider
availability is established by actual conversation turns. A failed turn is
reported without committing successful cursor progress. The V2 profile in
`dsh/profile` disables local shell
tools and supplies only the conversational instructions and journal delta.

Set `V2_QQ_ALLOWED_CONVERSATIONS` for the QQ conversations admitted to DSH,
for example `group:<group_id>,private:<peer_qq>`. An unset or empty value
leaves the router idle. This keeps a test connection from replying in other
groups.

Link `astrbot-plugins/v2_dsh_router` into V2's
`.runtime/astrbot/data/plugins` and include `v2_dsh_router` in the V2
`plugin_set`, after `v2_journal` has been configured. The router owns a small
SQLite mapping under `data/plugin_data/v2_dsh_router`. One ACP process hosts
all conversation sessions, with independent prompt locks and session-scoped
notification subscriptions. Each ordinary QQ wake
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

Deterministic V2 handlers that own an ordinary-text message must set
`event.set_extra("v2_deterministic_handled", True)` before replying. Passive
observers leave the marker unset. Routing uses this handling outcome instead
of handler-name classification. See the
[remediation findings](docs/phases/phase-3-remediation-findings.md).

## Phase 4: AstrBot MCP capabilities

Install the router's direct MCP runtime dependencies in the V2 environment:

```bash
.runtime/astrbot/.venv/bin/python -m pip install -r astrbot-plugins/v2_dsh_router/requirements.txt
```

The router now owns a loopback MCP listener in the AstrBot process.
`V2_MCP_PORT` defaults to 6210; use an available V2-specific port. The existing
provider/model and allowed-conversation settings remain required. Startup does
not create DSH sessions or run model probes.

Each active QQ turn attaches current-conversation HTTP MCP declarations through
native ACP session creation/resume. The tools are `history_recent`,
`history_before`, `history_search`, `current_group_info`, and `qq_send_origin`.
Their inputs contain no destination ID; history and sends are bound by the
runtime to the source conversation. Group information uses AstrBot's native
`get_group` query. Normal final text is sent automatically; `qq_send_origin`
provides a separate workflow output when needed. Only platform feedback creates
its canonical outbound journal row and real message ID.

```bash
./scripts/check-phase4.sh
```

Contracts, real QQ tool calls, message IDs, restart evidence and limits are in
[Phase 4 findings](docs/phases/phase-4-findings.md).


## Phase 5: conversation authorization and native tools

Linux deployment uses the pinned native sandbox plus `bwrap` and util-linux
`flock`. Run `scripts/bootstrap-dsh.sh` to install locked dependencies and link
`dsh/node_modules` to that installation for V2 native plugin imports. This link
is ignored; do not install a separate set of dependencies under `dsh/`.

Set real owner identities explicitly in the AstrBot runtime environment:

```bash
export V2_QQ_OWNERS='[{"platform_id":"v2-phase1-napcat","bot_id":"2869323076","user_id":"1105927155"}]'
```

The default is `[]` (no owner). Entries are exact platform/Bot/user triples;
text or nicknames cannot establish identity. Use the actual adapter ID for a
different deployment. Owner can use the enabled native `read`, `write`, `edit`
and foreground sandboxed `bash`, and the typed `conversation_scope_get` /
`conversation_scope_update` tools. Updates target the current conversation only,
require its expected revision, and replace the public ordinary scope. Preserve
other entries when adding/removing one file. There is no QQ approval workflow.

Ordinary scopes have independent `read`, `write`, `delete` arrays of canonical
absolute file paths. They default to empty and persist under
`.runtime/dsh-home/v2-conversation-scopes/`. Every filesystem operation checks the
latest committed scope. Existing-file overwrite/edit requires read plus write;
file creation cannot create a missing parent directory. Ordinary shell is denied
regardless of a mentioned allowed path. This profile has no ordinary delete
primitive; a delete entry does not enable shell or invent a tool. Owner can
delete disposable workspace files through the native foreground shell.

Only V2 passes current requester facts from the committed inbound journal row.
Private runtime source files live outside the workspace, bind epoch/session/
nonce and the actual claimed native prompt digest, and clear after completion or
successful native drain. Old queued input cannot borrow a newly published
source. Missing/expired sources fail closed; failed shutdown retains sources and
can be retried. Scope/source control files cannot be granted to ordinary members.
Background jobs, persistent shells and derived Agent execution are unavailable.
Neither scope changes nor denied tools recreate sessions or rewind journal
cursors. Known model context is not erased by revocation.

```bash
./scripts/check-phase5.sh
```

This runs preceding integration contracts, pinned native authorization tests and
source lifecycle tests without sending QQ messages. Real QQ results and known
upstream limits are recorded in [Phase 5 findings](docs/phases/phase-5-findings.md).

## Start the current Agent-Bot

With DEEPSEEK_API_KEY, V2_DSH_PROVIDER, V2_DSH_MODEL and V2_QQ_ALLOWED_CONVERSATIONS set explicitly in the environment, run scripts/run-agent-bot.sh. V2_QQ_OWNERS is an exact identity JSON array and defaults to no owner runtime. The launcher runs AstrBot in the foreground; its router starts and cleans up the two native DSH ACP runtimes. Credentials are inherited only, never saved by this launcher. The existing runtime dashboard is localhost:6186, OneBot reverse WS localhost:6200, and MCP localhost:6210. ACP runtimes have no independent Web page; localhost:3081 belongs to the separate old DSH service.
