# Phase 2 capture findings: OneBot self-message feedback

## Scope and choice

**Chosen boundary: Candidate A, NapCat `message_sent` feedback.** The installed
NapCat instance reported actual Bot sends with OneBot message IDs in both test
conversations. AstrBot v4.28.2 does not forward `message_sent` through its
normal `AstrMessageEvent` pipeline, but its aiocqhttp `CQHttp` client exposes an
event subscription API. A temporary V2 plugin registered for `message_sent` at
`@filter.on_platform_loaded()` and received the feedback without editing
AstrBot core. Group and private inbound `message` events continued through the
normal V2 event pipeline.

The alternative unified-send wrapper is deferred. `after_message_sent` only
covers `RespondStage`; it omits direct `event.send` and `Context.send_message`
paths. A wrapper would also have to account for every OneBot action generated
by a segmented AstrBot send. The observed platform feedback supplies the
actual IDs after QQ accepted those sends.

## Tested setup

- Date: 2026-09-29. AstrBot pinned to `v4.28.2`, commit
  `3c7adafa1397e182d60b1016bf88759265113c8a`.
- V2 used its ignored, isolated AstrBot runtime and reverse WebSocket listener
  `127.0.0.1:6200`. Its extra NapCat WebSocket client had
  `reportSelfMessage=true` and array message format. A second temporary raw
  OneBot observer used `127.0.0.1:6201` with the same option.
- Bot `<test_bot_qq>`, sender `<test_sender_qq>`, group `<test_group_qq>`, and
  private chat were the only controlled targets. The legacy `6199` NapCat
  client was disconnected during the final experiment; its QQ-specific DSH
  process and bridge were stopped. The independent DSH Web service remained
  running and was not used by V2.
- V2's default provider remained disabled. A temporary ignored observer logged
  inbound AstrBot events and subscribed to the aiocqhttp client's
  `message_sent` event. It sent fixed test payloads through V2's
  `Context.send_message` for proactive and segmented cases. A separate raw
  WebSocket observer independently received the same outbound events.

## Live QQ matrix

NapCat's read-only history API confirmed delivery. IDs below are platform
message IDs, not V2 database row IDs.

| Case | Raw OneBot / V2 observation | Message ID |
| --- | --- | --- |
| Group inbound ordinary user message | `post_type=message`; V2 AstrBot event observer received `V2P2-A` | `900988395` |
| Private inbound ordinary user message | `post_type=message`; V2 AstrBot event observer received `V2P2-C` | `864933889` |
| Group plugin `/v2probe` reply | `post_type=message_sent`; V2 plugin raw subscription and independent raw observer both received fixed reply | `1444260322` |
| Private plugin `/v2probe` reply | Same two observers received fixed reply | `419087106` |
| Group proactive `Context.send_message` | Both raw observers received `V2P2-RAW-GROUP` | `115326212` |
| Private proactive `Context.send_message` | Both raw observers received `V2P2-RAW-PRIVATE` | `422870330` |

In the first controlled sequence, V2 received seven user events in the requested
order and NapCat history contained all four expected V2 outputs. The initial
AstrBot event observer saw no outbound event because it only handled normal
`message` events. A raw observer initially made the same filter mistake. After
including `message_sent`, the raw observer and the V2 aiocqhttp subscription
both received the two proactive test messages and then the two plugin replies
above. This establishes the event-type boundary rather than assuming that an
absent normal event means NapCat sent no feedback.

## Event shape, ordering, and loops

NapCat emitted self feedback as `post_type=message_sent`,
`message_sent_type=self`, `user_id=self_id=<test_bot_qq>`, with a numeric
`message_id`. For a private self message, `target_id=<test_sender_qq>` identifies
the peer; using `user_id` or AstrBot's self-message session ID would incorrectly
identify the Bot as its own private peer. For group feedback, `group_id` and
`target_id` identify the group.

Each of the controlled plain-text sends produced one feedback event per
observer. The normal AstrBot event pipeline did not receive those
`message_sent` events, and neither `/v2probe` nor `/v2push` ran recursively.
Within the observed sequence, feedback arrived in the same order as the QQ
messages. The OneBot `time` field is only second precision; the V2 journal must
use its own monotonic insertion ID as a cursor and retain the platform time
separately. Several V2 reconnects did not replay earlier controlled messages,
but this is not a guarantee against reconnect or retry duplicates.

A V2 `MessageChain` containing a text segment and a file segment produced two
QQ messages and two IDs: text `1376343965`, file `2128530188`. The file feedback
included `file`, `file_id`, `file_size`, and a download URL. Therefore, journal
idempotency must use each platform message ID rather than an AstrBot result ID,
and content normalization must retain available file metadata without copying
the binary file. A direct call through V2's aiocqhttp client returned
`message_id=1147275184`; the `message_sent` feedback for that test message had
the same ID. AstrBot's ordinary `AiocqhttpMessageEvent._dispatch_send` currently
discards that return value, so feedback remains the shared confirmation path.

## Pinned source paths and implementation constraints

- NapCat's installed `napcat.mjs` `initializeMessage()` sets
  `post_type=message_sent` for self messages; `handleMsg()` dispatches them only
  to network clients with `reportSelfMessage=true`.
- `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py`
  registers only `on_message("group")` and `on_message("private")`, and
  `convert_message()` handles `post_type=message`, notice, and request. It has
  no `message_sent` forwarding path.
- The installed aiocqhttp `CQHttp.subscribe("message_sent", callback)` dispatches
  these events through its own event bus. AstrBot's
  `@filter.on_platform_loaded()` runs after platform instance creation, and
  `Context.get_platform_inst(id).get_client()` exposes that CQHttp client.
- Final journal implementation uses `CQHttp.hook_before("message", callback)`
  and `hook_before("message_sent", callback)`. The bus calls root before hooks
  before AstrBot's group/private handler, so inbound capture does not wait for
  conversion or get skipped by downstream allowlist and wake filters. This
  refinement followed independent review of the first journal implementation;
  the live capture matrix above predates it.
- `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py`
  splits chains containing `File`, `Node`, or `Nodes` into separate sends; a
  normal chain uses one OneBot send action. The V2 journal plugin registers its
  raw callback once per active platform client and unsubscribes on termination.

## Remaining uncertainty

The experiment covers controlled text and one small file; real images, voice,
forwarded nodes, failed sends, and large-scale reconnect behavior remain
unverified. A unique key involving platform instance, Bot ID, conversation,
and platform message ID will make repeat feedback safe. These cases do not
require a different capture boundary before implementing the Phase 2 journal.
