# Phase 2 — Canonical Journal

> Status: ready for implementation  
> Parent plan: [PLAN.md](../../PLAN.md)  
> Input evidence: [Phase 1 findings](phase-1-findings.md)

## Goal

Establish one V2-owned canonical journal that represents the QQ conversation actually observed by the system.

Phase 2 must answer and implement one narrow question:

> Where can V2 observe every relevant QQ inbound and outbound message reliably, without modifying AstrBot core?

Do not integrate DSH yet. Do not build MCP yet.

## Decisions already made

These are not open questions for Phase 2:

1. **V2 owns the canonical journal contract and storage.**
   AstrBot's `PlatformMessageHistory` is useful upstream behavior and evidence, but it is not the canonical journal storage for V2.

2. **Do not patch AstrBot core.**
   Prefer AstrBot plugin/adapter extension points or OneBot-observable behavior.

3. **Do not write journal rows independently inside business plugins.**
   The journal must observe shared message boundaries, not require every Steam/Twitter/Bilibili/etc. plugin to remember to log itself.

4. **A journal row represents an observed message event, not an Agent memory.**
   DSH session state, long-term memory, summaries, embeddings, permissions, and tool history belong to later phases.

## Why PlatformMessageHistory is not the canonical journal

Phase 1 verified that the existing AstrBot table does not satisfy the V2 contract:

- private inbound/outbound messages are not covered;
- normal plugin/command replies are not covered;
- QQ/OneBot platform message IDs are not stored;
- output provenance is insufficient;
- message serialization loses information needed for a transport-level record.

Do not spend Phase 2 trying to reshape that table into the V2 journal.

## Phase 2A — Capture-boundary experiment

**Do this before creating the final journal implementation.**

Two candidate observation strategies must be evaluated in order.

### Candidate A — OneBot self-message feedback

Prefer this candidate if NapCat can report messages sent by the Bot back through OneBot as normal observable message events.

Configure a controlled V2 test client with the relevant NapCat self-message reporting option and determine whether V2 can observe actual Bot outputs after QQ accepts them.

The experiment must cover at least:

| Case | Required evidence |
| --- | --- |
| group inbound user message | actual OneBot event with real message ID |
| private inbound user message | actual OneBot event with real message ID |
| group plugin reply | whether Bot self-message returns, with message ID |
| private plugin reply | whether Bot self-message returns, with message ID |
| group `Context.send_message` | whether Bot self-message returns, with message ID |
| private `Context.send_message` | whether Bot self-message returns, with message ID |

Also test:

- whether self-message feedback reaches AstrBot's normal event pipeline;
- whether it can trigger commands/plugins again;
- whether it can trigger the future DSH router;
- whether one outbound send generates one or multiple feedback events;
- whether segmented/media sends produce one or multiple platform message IDs;
- whether ordering is stable enough to use as the journal sequence input;
- whether the original outbound send response already exposes the same message ID;
- whether reconnect/retry behavior can create duplicates.

### Candidate A acceptance

Candidate A is acceptable only if:

- Bot outputs can be observed reliably in both group and private chat;
- real platform message IDs are available;
- feedback can be marked/filtered so it does not create reply loops;
- duplicates can be identified deterministically;
- normal inbound user events remain unaffected.

If these conditions are met, prefer recording the **platform-confirmed/self-reported message** over recording an earlier send intention.

### Candidate B — AstrBot unified send boundary

Evaluate this only if Candidate A is missing, unreliable, or unsuitable.

Find the narrowest extension point that can observe all required AstrBot outbound paths, including:

- `RespondStage -> event.send()`;
- direct plugin `await event.send()`;
- `Context.send_message()`;
- future DSH send through AstrBot.

Important verified constraint:

> `after_message_sent` is not a universal outbound hook.

In AstrBot v4.28.2 it is invoked by `RespondStage` after its sends. Direct plugin `event.send()` and `Context.send_message()` do not automatically pass through that hook.

Therefore, do not implement Phase 2 by merely adding an `@filter.after_message_sent()` journal handler.

Investigate the aiocqhttp/OneBot send path and available extension points first. If no plugin-level extension can observe all sends, document the exact limitation before considering a thin adapter wrapper or patch mechanism.

### Capture experiment output

Before journal implementation, create:

`docs/phases/phase-2-capture-findings.md`

It must record:

- tested configuration;
- exact source paths involved;
- live QQ evidence;
- message IDs;
- duplicate/loop behavior;
- chosen capture strategy;
- rejected alternative and why.

Do not proceed to the final journal implementation until this choice is evidence-backed.

## Phase 2B — Journal contract

After the capture boundary is chosen, implement a V2-owned journal.

Minimum logical row:

```text
journal_id        monotonic internal sequence
platform_id       AstrBot platform instance
conversation_key  stable V2 conversation identity
conversation_type group / private
direction         inbound / outbound
actor_id
actor_name
source            user / astrbot-plugin / dsh / system / unknown
message_id        QQ/OneBot message ID when available
reply_to           referenced platform message ID when available
content            normalized message content
created_at         observed platform/event time when available
observed_at        V2 journal insertion time
```

The physical schema may differ, but the contract must support these semantics.

### Conversation identity

Define one stable V2 conversation key independent of AstrBot's historical database column names.

Expected QQ shape:

```text
group:<group_id>
private:<peer_qq>
```

Include platform/bot identity if required to avoid collisions across multiple Bot accounts.

Do not make DSH session IDs part of the journal identity.

### Message identity and idempotency

Prefer the platform `message_id` as the external identity when OneBot provides one.

The journal must tolerate:

- duplicate OneBot/self-message events;
- reconnect replay if NapCat emits it;
- retry paths;
- process restart.

Define and test an idempotency key before relying on `journal_id` ordering.

Do not silently discard two legitimate segmented messages merely because they originate from one logical AstrBot result.

### Content

Store enough normalized information for later DSH context reconstruction.

At minimum preserve:

- plain text;
- mentions;
- replies/referenced message ID;
- media/file type and stable reference metadata that is actually available.

Do not copy transient binary payloads into the journal by default.

Do not reduce every media item to only `[Image]` / `[File]` if more useful transport metadata is available.

### Provenance

The canonical journal is about what happened on QQ. Provenance is useful metadata, but it must not be required for correctness when the transport cannot provide it.

For outbound messages, record the most reliable source available, for example:

- `astrbot-plugin:<plugin_name>`
- `dsh`
- `system`
- `unknown`

Do not invent provenance after the fact from message text.

If Candidate A observes only platform feedback and loses the initiating plugin identity, use a small correlation mechanism only if needed; do not recreate a general delivery ledger.

## Read API required in Phase 2

Implement only the read operations needed to prove the journal is usable:

- recent messages for one conversation;
- messages after a given `journal_id`;
- messages before a given `journal_id`;
- lookup by platform `message_id`.

A full text-search engine is not required yet.

Do not expose these through MCP in Phase 2. A Python/service API used by tests is sufficient.

## Tests

At minimum, test this sequence end-to-end with the real AstrBot + NapCat test connection:

```text
1. user group message
2. AstrBot deterministic plugin reply
3. user group follow-up
4. AstrBot proactive group send
5. user private message
6. AstrBot private plugin reply
7. AstrBot proactive private send
```

The journal must reproduce the QQ-visible ordering for those messages.

Also verify:

- every row has the expected conversation;
- real message IDs are retained when available;
- duplicate feedback does not create duplicate rows;
- restart preserves rows and cursor semantics;
- self-message feedback, if used, does not recursively trigger another reply.

## Explicit non-goals

Do not implement:

- DSH session mapping;
- DSH wake/routing;
- journal cursor stored per DSH session;
- prompt/context injection;
- MCP;
- long-term memory;
- member memory migration;
- business plugin migration;
- owner/ordinary-user permission profiles;
- shell/filesystem permission policy;
- cross-conversation send;
- general delivery ledger;
- execution claims;
- HMAC execution context;
- OneBot direct-control fallback.

If the capture experiment exposes a missing capability, document it rather than solving unrelated later-phase requirements.

## Phase 1 AI gate note

The current `v2_ai_gate` is a **Phase 1 safety gate**, not a permanently frozen V2 design.

Its unconditional `on_llm_request -> event.stop_event()` behavior blocks any AstrBot LLM request reaching that hook, including future plugin-local LLM use.

During Phase 2:

- keep the gate stable unless it interferes with the capture experiment;
- do not expand it;
- do not treat its current behavior as the final answer for later plugin migration.

A later phase may narrow the gate so AstrBot cannot own free-form chat while selected plugins may still use LLM internally.

## Expected repository changes

Likely paths:

```text
docs/phases/
  phase-2.md
  phase-2-capture-findings.md
astrbot-plugins/
  v2_journal/              # only after capture strategy is chosen
tests/
  contracts/
  e2e/
```

Storage/migration files may be added when the journal implementation is chosen.

Do not create DSH or MCP scaffolding in this phase.

## Acceptance criteria

Phase 2 is complete only when:

- the outbound capture strategy is selected from live evidence;
- the choice and rejected alternative are documented;
- group/private inbound messages enter the V2 journal;
- group/private plugin replies enter the V2 journal;
- group/private proactive sends enter the V2 journal;
- real QQ/OneBot message IDs are retained when available;
- idempotency behavior is tested;
- QQ-visible message ordering can be reconstructed;
- journal rows survive restart;
- no AstrBot core source is modified;
- no DSH/MCP/permission architecture is implemented.

## Handoff to Phase 3

Phase 3 may begin only after the journal contract is stable.

Its first end-to-end target will be:

```text
user message
    -> AstrBot
    -> deterministic plugin may reply
    -> canonical journal records actual exchange
    -> later unhandled conversational message wakes DSH
    -> DSH receives only journal entries after its last observed cursor
    -> DSH reply goes through AstrBot
    -> canonical journal records that reply
```

Phase 3 owns DSH session mapping and per-session journal cursor semantics. Phase 2 does not.
