# Phase 4 — AstrBot MCP capability findings

> Status: completed, 2026-09-30
> Requirements: [Phase 4](phase-4.md)
> Baseline: [Phase 3 startup follow-up](phase-3-startup-validation.md)

## Implementation and upstream boundary

The existing V2 router plugin owns one official FastMCP Streamable HTTP service
on a loopback socket (`127.0.0.1`, `V2_MCP_PORT`, default 6210). Uvicorn runs as
an asyncio task in the AstrBot process. The service starts before conversation
admission and closes with the plugin; it does not install signal handlers over
AstrBot's host handlers or create another runtime process.

Pinned AstrBot `Context.register_web_api` provides handler registration and
response coercion, not a supported ASGI/lifespan mount. The official MCP SDK's
ASGI application/lifespan is therefore served by this small plugin-owned
listener. No upstream AstrBot or DSH source was patched. Dependencies are
`mcp==1.30.0` and `uvicorn==0.54.0` in the router's `requirements.txt`.

The pinned ACP `session/new` and `session/resume` accept standard HTTP MCP
server declarations. DSH mounts them inside the individual Agent scope. One
shared ACP process still owns independent conversation sessions. The persistent
conversation/session/cursor store is unchanged.

Each conversation gets an opaque transport token derived by the integration,
not the model. HTTP authority exists only inside its serialized active turn.
The token selects the fixed `(platform_id, bot_id, conversation_key)` and current
AstrBot callbacks. No tool accepts a destination conversation, group, peer, Bot,
or platform ID. Resume supplies fresh connection declarations after restart;
an active session cannot silently change its MCP binding.

This is the Phase 4 current-source contract, not a new role/policy platform.

## Typed MCP contracts

| Tool | Inputs | Structured result | Meaning |
| --- | --- | --- | --- |
| `history_recent` | `limit` (1–100, default 50) | `entries: JournalEntry[]` | Newest current-conversation journal window, ascending order |
| `history_before` | positive exclusive `before_id`, `limit` (1–100) | `entries: JournalEntry[]` | Older current-conversation window |
| `history_search` | literal text `query` (1–200 chars), optional positive exclusive `before_id`, `limit` (1–100) | `entries: JournalEntry[]` | Scoped normalized-text search, ascending order; literal `%`/`_` are escaped |
| `current_group_info` | none | `applicable`, `partial`, `id`, `name`, `member_count`, `reason`; unavailable fields nullable | AstrBot's native current-group name/count query; private chats return not applicable |
| `qq_send_origin` | plain `text` (1–4000 chars) | `submitted`, `platform_confirmed` booleans | Await fixed-origin AstrBot send; submission does not claim platform confirmation |

`JournalEntry` contains journal ID, direction, actor ID/name, source, real
message ID, reply reference, normalized content, and platform timestamp.
All tools advertise input and output schemas via MCP. Context/header details
are not tool parameters. History reads use the existing canonical JournalStore
API; only its scoped `search` read method was added, with no schema or second
history store.

The selected real capability is native `AstrMessageEvent.get_group`, overridden
by the pinned aiocqhttp adapter. It obtains OneBot group metadata through
AstrBot and returns `Group`. The MCP result exposes only the name/count summary,
not the roster, owners or admins. Missing name/count yields `partial=true`.
Native AstrBot may log API failures and return fallback information; that
wrapper cannot independently prove whether cached fields are freshly fetched.

The MCP listener raises protocol tool errors for failed callbacks/invalid
inputs. DSH can explain a tool failure within the same successful turn. The
cursor then records consumption of the user input, not success of the failed
action. Tools never append speculative journal rows. Failed DSH/final-send
outcomes retain the established pending cursor and do not commit success.

`qq_send_origin` calls the same event-bound AstrBot text-send callback as normal
final replies. Platform `message_sent` captures the real output later, and the
next DSH delta includes it. There is no handler reflection, synthetic AstrBot
event, direct DSH OneBot path, or delivery ledger.

## Native and real QQ evidence

A separate native DSH test on an isolated journal/home actually called
`history_search`, `current_group_info`, and `qq_send_origin` via ACP-mounted MCP,
then used their results in natural text. It proves the native mount/discovery
path beyond the controlled model fakes used by integration tests.

Live tests used only the already authorized sender, Bot private conversation,
and test group. Existing NapCat transport credentials/configuration were used;
no old chatbot business code or scripts were reused.

| Story / turn | Journal inbound → outbound | Actual QQ inbound → outbound IDs | Result |
| --- | --- | --- | --- |
| A: private old history | 98 → 99 | 845800152 → 1438724165 | MCP finds old row 68 and 梅花七号 |
| B: current group capability | 108 → 109 | 1983064399 → 1191662142 | Native group result: 沙箱 测试, 1055300305, 3 members |
| C: explicit group tool send | 125 → 126, 127 | 397467623 → 744418846, 969440983 | Tool sends `MCP-P4-ORIGIN-OK`; distinct final answer follows |
| C follow-up | 128 → 130 | 256505204 → 572179159 | DSH sees tool output row 126 and actual ID 744418846 |
| D: restart / group capability + history | 155 → 156 | 1689360153 → 922865417 | Same session; both MCP calls work and recover row 126 |

Read-only QQ `get_msg` verified all six outbound IDs/texts against the journal.
The live group summary also matched an independent read-only platform query.

The original session IDs were retained:

- Private: `6ec74ba7-5817-445e-8e50-ced94b29705e`, cursor 98 after story A.
- Group: `522d4300-99dd-4209-af99-93f8f74f82d4`, cursor
  `108 → 125 → 128 → 155`, clear pending state after each successful turn.

Actual persisted DSH `tool/call` and `tool/result` events show:

- private `mcp__astrbot__history_search`, query `V2R-A`, before_id 76;
- group `mcp__astrbot__current_group_info` and its matching name/count result;
- group `mcp__astrbot__qq_send_origin`, text `MCP-P4-ORIGIN-OK`;
- history searches for that output, including after restart.

Input deltas were private `[77,98]`, then group `[79,108]`, `[109,125]`,
`[126,127,128]`, and after restart `[130,155]`. Row 68 was absent from normal
input and obtained through MCP. Consumed history was not replayed.

The model header contained the five scoped AstrBot business tools plus DSH's
three native MCP resource discovery/read helpers. No shell tools were present.
The AstrBot MCP service exposes no resources. Restart before the next user turn
created no session and no model request, preserving the startup follow-up.

## Automated checks

`./scripts/check-phase4.sh`: **76 passed**, exit 0:

- 23 Phase 1 regression tests;
- 14 Phase 2 journal/order tests;
- 32 Phase 3 mapping/routing/turn/client tests;
- 7 Phase 4 MCP protocol and journal/model integration tests.

Coverage includes actual MCP list/call transport and schemas, wrong/expired
credentials, fixed-origin send despite extra target input, bot/conversation
history isolation, literal search wildcards, nullable/partial group results,
callback/send failures, port conflicts, shutdown cancellation, shared ACP
create/resume binding, older-context lookup outside the normal delta, canonical
feedback into the following turn, and explained tool errors without action rows.
The pinned AstrBot pipeline test additionally verified that failed ACP startup
closes the already started MCP listener (focused rerun: 1 passed).

Independent read-only review found no remaining material issue. Existing
upstream deprecation warnings and the SDK client's deprecated helper warning
remain; no unreviewed dependency upgrade was made.

## Completion and limits

The temporary V2 NapCat test client was removed and the V2 test process stopped.
Other client configuration was left unchanged. Secrets and runtime data were
not committed. Phase 5 permissions/owner tools and later feature/memory
migration were not implemented.

Text-only controlled acceptance does not establish media/tool-output splitting,
all third-party plugin integration, multi-process/high availability, or recovery
of uncertain turns. The inherited dependency advisory/reproducibility limits
remain documented in Phase 3 findings. There is no production rollout claim.
