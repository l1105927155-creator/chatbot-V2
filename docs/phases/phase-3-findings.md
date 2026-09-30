# Phase 3 — DSH chat-loop findings

> Status: completed, 2026-09-30
> Scope: [Phase 3](phase-3.md); no MCP, memory migration, or owner tools.
> Historical baseline: process ownership, routing and model configuration below
> were corrected by [Phase 3 remediation](phase-3-remediation-findings.md).

## Implementation

- `v2_dsh_router` uses the existing journal read API and AstrBot text-send path.
- Its SQLite store binds `(platform_id, bot_id, conversation_key)` to the native
  DSH session ID, consumed journal cursor, and an unresolved-turn marker.
- An ordinary text wake reads only rows after the saved cursor through its own
  inbound journal row. Later queued inbound messages retain their own turn.
- A per-conversation lock covers delta selection, DSH, sending, and cursor commit.
  Different conversations run independently, including during session resume.
- The cursor commits after DSH completes and AstrBot's send returns. The Bot's
  `message_sent` feedback is captured independently by `v2_journal`; its row is
  input to the following turn, not a second DSH-owned QQ history.
- Duplicate wakes at or below the cursor are ignored. An error/cancellation after
  beginning a turn preserves its pending marker and pauses automatic retries.
  Transport failure cannot establish whether DSH accepted input or QQ saw a send;
  manual inspection is required before resolving that conversation.
- Shutdown drains active DSH work before closing all child processes. Cancelling
  an async waiter cannot cancel a running synchronous transport thread.
- `V2_QQ_ALLOWED_CONVERSATIONS` explicitly scopes admission; empty means idle.
  This is a controlled test scope, not the later-phase permission system.

Runtime, source, model state, and credentials remain in ignored V2-owned paths.
No old chatbot business code, program, or script was reused. Only the previously
authorized existing NapCat transport/credential configuration was used for
live tests. Test messages targeted only the authorized Bot private chat and
test group. V2 Bot replies traveled through AstrBot.

## Pinned upstream behavior and native extension choice

DSH source: `4878cdabd87d4041bdaff61d04c966883b9fd07a`; npm runtime:
`@deepseek-ai/dsh@0.2.0-rc.1`. AstrBot: v4.28.2.

The first real SDK restart test failed with `session already exists`. In the
pinned `packages/sdk/server/src/server.ts`, `createSession` always calls
`ctx.agents.create`; the normal Python `DeepSeekHarness.run(session_id=...)`
entry point does not provide a resume method. A stable mapping alone therefore
could not meet restart acceptance.

The pinned native ACP server advertises and implements `session/resume`
(`packages/acp/acp/src/index.ts`). V2 uses official `HarnessClient`
JSON-RPC transport with `initialize`, `session/new`, `session/resume`,
`session/prompt`, and `session/close`. A V2 profile patch replaces the SDK server
with the official ACP startup/server plugins on `sdk-minimal`; no upstream core
was modified and no additional bridge or runtime service was introduced.

The profile disables persistent bash/pwsh. Actual model request headers had
`tools=[]`, both before and after the restart test. This phase returns text;
MCP and richer permissions remain outside this implementation.

## AstrBot routing evidence

The initial live ordinary-message test was journaled but received no reply.
The first observer allowlist omitted built-in `persist_group_message` and
`on_message`, which register through `platform_adapter_type(ALL)` in the pinned
AstrBot. Conservative admission consequently skipped that message.

The corrected allowlist identifies those observers by exact module and handler
name. A regression contract loads the real pinned plugin registry and runs
WakingCheck, Process, and Respond stages. Ordinary text reaches DSH exactly once;
`/v2probe` replies directly without a DSH turn. The subsequent live tests passed.

Only known pinned observers are allowed alongside the router. Slash commands,
self/non-text input, stopped/marked deterministic events, and unknown activated
handlers are skipped. A new deterministic plugin requires an explicit handled
marker or an evidence-based observer classification; arbitrary plugin behavior
is not inferred from its name.

## Real QQ acceptance, 2026-09-30

IDs below are the receiving Bot's actual platform IDs captured in the journal,
not the sender instance's outgoing acknowledgements.

| Step | Journal inbound → outbound | Platform inbound → outbound | Result |
| --- | --- | --- | --- |
| Private ordinary A2 | 42 → 43 | 1931458544 → 771154804 | DSH remembers 秋叶五号 and replies |
| Private `/v2probe` | 51 → 52 | 1234792693 → 754123995 | `v2 phase 1 probe: ok`; no DSH wake |
| Private follow-up B | 54 → 55 | 37217661 → 228343646 | DSH correctly cites probe result and code |
| Restart, private C | 56 → 57 | 1500126181 → 1253703605 | Same session resumes; both facts retained |
| Group ordinary G | 58 → 59 | 136702130 → 1404469478 | DSH replies through AstrBot in test group |

Read-only QQ `get_msg` checks confirmed the four private outbound IDs and texts.
The group reply was observed through canonical `message_sent` feedback.

Private DSH session remained `6ec74ba7-5817-445e-8e50-ced94b29705e` across the
AstrBot restart. Persisted cursor progression was `42 → 54 → 56`; pending state
was clear after each successful turn. `/v2probe` left the cursor at 42.

The actual DSH input deltas were inspected in its persisted user-message input:

- First turn: `[11,12,13,14,15,23,24,37,42]`, existing private history through A2.
- Follow-up: `[43,51,52,54]`, prior Bot reply, user command, plugin reply, B.
- After restart: `[55,56]`, prior Bot reply and C only; consumed history was not
  injected again.

Journal IDs are global, so other conversation rows create expected gaps.
The retained row 37 is the initial failed routing attempt described above.
A separate local two-process native ACP test also recovered an earlier code
from the same persisted session.

After acceptance, the V2 test process was stopped and its temporary NapCat
reverse client removed. The old chatbot connection remains disabled.

## Reproduction and checks

`bootstrap-dsh.sh` completed successfully: verified official archive SHA256 and
source file/link contents, installed the tracked npm dependency lock with
`npm ci --omit=dev`, and built/installed the pinned Python SDK. The SDK metadata
version is `0.0.0.dev0`, so content verification establishes its source revision.
The npm artifact reports the locked release version; npm metadata does not
establish that artifact's exact Git commit. Python SDK installation uses
`--no-deps` with the explicit V2 npm runtime and existing AstrBot dependencies.

`check-phase3.sh` covers Phase 1/2 regressions, state migration/concurrency,
source-tampering rejection, ACP adapter lifecycle, resume independence,
close-all behavior, routing through the actual pinned AstrBot pipeline,
per-conversation serialization, duplicate wakes, bounded deltas, pending-turn
restart behavior, and cancellation draining. See the completed test run below.

## Limits

- Lost/failed turns require manual recovery; no automatic replay or retry ledger
  is claimed. Multi-process writers and high availability are not supported.
- The live test used private chat plus one group text reply, not attachments,
  long-output splitting, production rollout, or arbitrary third-party plugins.
- AstrBot Python dependencies retain upstream version ranges; exact fresh Python
  dependency resolution is not locked by this phase.
- The locked npm dependency tree reports moderate advisories during install;
  see the verification record below. No unreviewed dependency upgrade was made.
- No MCP tools, earlier-history retrieval tool, member-memory migration, owner
  execution, or Phase 4 implementation is included.

### Completed verification record

- `./scripts/bootstrap-dsh.sh`: exit 0; pinned source verification, npm install,
  SDK wheel installation, and runtime version/import checks passed.
- `./scripts/check-phase3.sh`: exit 0; **59 passed** (23 Phase 1, 14 Phase 2,
  22 Phase 3). Upstream deprecation warnings remain.
- `npm audit --omit=dev`: 8 moderate affected dependency entries, zero high or
  critical entries. They trace to `fflate` malformed-ZIP64 `unzipSync` infinite
  loop via the pinned LibreOffice/office packages. This text-only profile does
  not mount office tools; the upstream package dependency remains installed.
