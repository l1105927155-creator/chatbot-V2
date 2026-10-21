# chatbot-V2

AstrBot + DeepSeek Harness (DSH) 的干净集成项目。上游版本固定于 [`upstream.lock.json`](upstream.lock.json)，当前架构与阶段边界以 [`PLAN.md`](PLAN.md) 顶部及[执行域纠偏契约](docs/phases/execution-domain-correction-2026-10-16.md)为准。

维护入口：

- [`PLAN.md`](PLAN.md)：当前架构、契约和工作顺序。
- [`docs/architecture/agent-bot-runtime.md`](docs/architecture/agent-bot-runtime.md)：当前运行时说明。
- [`docs/phases/personal-agent-bot.md`](docs/phases/personal-agent-bot.md)：产品化阶段状态与历史实施记录。
- [`docs/personal-agent-acceptance.md`](docs/personal-agent-acceptance.md)：真实 QQ 固定故事验收清单。
- [`docs/personal-agent-operations.md`](docs/personal-agent-operations.md)：部署与运维。
- [`AGENTS.md`](AGENTS.md)：开发约束。
- [QQ 文件收发](docs/qq-file-transfer-validation-2026-10-18.md)：任意文件格式、工作区隔离与验证范围。
- [人格与指南热更新](docs/guide-hot-reload-validation-2026-10-18.md)：可编辑文件、生效时机与验证。
- [QQ 原生视觉](docs/phases/qq-native-vision-2026-10-18.md)：图片上下文、文件任务与来源绑定。
- [Agent 用户命令](docs/phases/agent-command-surface.md)：真实会话重置、停止执行、Owner 权限和历史起点；已部署，见[验证记录](docs/agent-command-surface-validation.md)。

当前版本保留两个信任域和三种工具组合：`owner-host`、`group-sandbox`、`safe-chat`。Owner 精确身份的独立私聊使用部署账户可用的主机环境；每个已准入群（包括群内 Owner）有独立的沙箱运行时、工作区、会话和状态；普通非 Owner 私聊使用 `safe-chat`。人格、Scene 和 Skill 不授予权限。正常 QQ 收发经过 AstrBot，Journal 是唯一聊天事实记录。具体资源边界见[执行域实施记录](docs/execution-domain-validation-2026-10-16.md)。

**当前状态：执行域实现已部署，隔离自动验证已通过；新的真实 QQ 固定故事尚未验收，连续三天观察尚未开始。** 不把模拟 QQ、真实模型隔离测试或旧合同（Owner 受限、群无 Bash）的结果记作新执行域的真实 QQ 验收。

## V2 plugin documentation

| Plugin | Purpose |
| --- | --- |
| [V2 AI Gate](astrbot-plugins/v2_ai_gate/README.md) | Suppress AstrBot conversational LLM requests |
| [V2 Journal](astrbot-plugins/v2_journal/README.md) | Canonical platform-confirmed chat history |
| [V2 DSH Router](astrbot-plugins/v2_dsh_router/README.md) | DSH sessions, trust domains, scoped MCP and profile controls |
| [V2 Phase 1 Probe](astrbot-plugins/v2_phase1_probe/README.md) | Deterministic message-chain diagnostics |

## Current runtime and checks

运行时由 AstrBot 插件启动 DSH ACP 和本地 MCP。配置准入会话与 Owner 精确身份后，Owner 私聊进入 `owner-host`；每个准入群分别进入自己的 `group-sandbox` runtime；普通非 Owner 私聊进入 `safe-chat`。群内 Owner 仍属于该群沙箱。各群隔离 home、工作区、Session 与状态；群 Shell 使用独立工作区和资源边界。

当前执行域的实现细节、已部署证据和边界见[实施记录](docs/execution-domain-validation-2026-10-16.md)，运行命令与环境变量见[运维手册](docs/personal-agent-operations.md)。不要从下方历史 Phase 配置推导当前 Profile 或权限。

运行隔离回归：

```bash
bash scripts/check-agent-profile.sh
# 四项审查整改的独立模拟 QQ 回归（不使用付费模型）
.runtime/astrbot/.venv/bin/python scripts/check-review-remediation.py
```

隔离真实模型检查需显式执行 `scripts/check-agent-profile-live.py` 并提供运行环境凭据；它不连接 QQ。自动测试和隔离模型测试不能替代真实 QQ 验收。

## Historical phase setup (reference only)

下列 Phase 1–5 配置和启动段落是阶段实施记录，仅用于理解历史决策与复现对应旧阶段；**它们不是当前启动说明，也不描述当前执行权限**。当前启动请使用上面的运维手册。旧文档中的 Profile 名称、Owner 沙箱限制、群聊无 Bash、端口与迁移状态均不得覆盖当前契约。

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
upstream limits are recorded in [Phase 5 findings](docs/phases/phase-5-native-findings.md).

## Current startup

Use the deployment-specific commands in the [operations guide](docs/personal-agent-operations.md). It is the startup and service-management source of truth; historical Phase 1–5 commands above are retained only as records.
