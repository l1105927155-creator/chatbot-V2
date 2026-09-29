# Phase 2.1 — Journal Ordering Contract

> Status: completed — 2026-09-29
> Parent plan: [PLAN.md](../../PLAN.md)  
> Input evidence: [Phase 2 journal findings](phase-2-journal-findings.md)
> Verification: [Phase 2.1 findings](phase-2.1-findings.md)

## Goal

Verify and, if necessary, enforce the ordering semantics that Phase 3 will rely on when using `journal_id` as a DSH cursor.

Phase 2 proved that V2 can capture real inbound QQ messages and platform-confirmed Bot outputs with real OneBot message IDs.

Phase 2.1 does **not** redesign the journal. It answers one narrower question:

> Does `journal_id` reliably reflect the order in which V2 observes OneBot message events, even when multiple events are handled concurrently?

Do not integrate DSH until this contract is proven.

## Why this phase exists

The current aiocqhttp reverse-WebSocket path creates asynchronous tasks for incoming payloads. The journal plugin then serializes SQLite writes with `_write_lock`.

That guarantees:

- only one journal write executes at a time;

but it does not, by itself, prove:

- tasks acquire the lock in the same order that OneBot payloads arrived;
- an earlier OneBot event cannot be written after a later event;
- a Phase 3 DSH wake triggered by event B cannot read the journal before event A has obtained its cursor.

Phase 3 intends to use:

```text
journal.after(last_seen_journal_id)
```

as the normal incremental context boundary. Therefore ordering must be an explicit tested contract rather than an assumption.

## Required contract

For one V2 process and one OneBot client:

> If message event A is observed by the V2 journal capture boundary before message event B, A must obtain a lower `journal_id` than B.

Additionally, before a normal inbound event continues into the AstrBot pipeline, that event's journal row must already exist.

This phase does not attempt to establish a distributed total order across multiple hosts or multiple independent OneBot clients.

## Step 1 — Reproduce concurrent delivery

Add a deterministic contract test that sends multiple `message` and `message_sent` events through the actual aiocqhttp event bus with controlled scheduling.

The test must be capable of deliberately delaying one capture/write path so that simple task scheduling can attempt to reorder writes.

Cover at least:

- inbound A followed by inbound B;
- inbound followed by outbound feedback;
- outbound feedback followed by inbound;
- group and private conversations;
- a short burst of multiple events.

Record whether the current implementation preserves capture order without additional code.

Do not assume that `asyncio.Lock` fairness proves the contract. Demonstrate behavior at the V2 capture boundary.

## Step 2 — Add the smallest ordering mechanism if needed

If the current implementation can reorder observations, fix it at the raw capture boundary.

Preferred shape:

```text
aiocqhttp before hook
        ↓
ordered in-process queue
        ↓
single journal writer
        ↓
SQLite
```

Requirements:

- one small in-process ordering primitive;
- no delivery ledger;
- no execution-claim system;
- no HMAC context;
- no DSH-specific state;
- no AstrBot core patch.

The capture callback may await acknowledgement that its own journal row has been committed before allowing the normal inbound event to continue.

Do not create background behavior whose failure can silently lose ordering guarantees. Shutdown must drain or explicitly reject pending entries.

If the existing implementation already satisfies the contract through the event-bus call structure, document and test that instead of adding a queue unnecessarily.

## Step 3 — Cursor semantics

After ordering is proven, document the exact semantics of `journal_id`:

- monotonically increasing within one journal database;
- assigned in OneBot observation order as defined above;
- stable across process restart;
- suitable for `after(journal_id)` incremental reads;
- not a platform timestamp;
- not comparable across independent journal databases.

The `created_at` OneBot timestamp remains platform metadata and must not replace `journal_id` for cursor ordering.

## Scope interaction

The journal currently captures raw OneBot messages before AstrBot allowlist, wake, and downstream pipeline filters.

That is intentional for now:

- the journal records the QQ conversation visible to this OneBot client;
- AstrBot processing scope and journal data scope are not the same concept.

Do **not** move journal capture behind AstrBot's allowlist merely to make the scopes match.

Phase 2.1 does not design DSH read authorization. Later permission work must decide which conversations a caller may read.

## Provenance

Do not expand outbound provenance work in this phase.

`source=unknown` for platform-confirmed outbound feedback is acceptable until a later phase has a concrete need to correlate a DSH or AstrBot capability invocation with the resulting platform message.

Do not introduce a delivery ledger solely to improve `source`.

## Tests and evidence

Add a findings document:

`docs/phases/phase-2.1-findings.md`

It must record:

- whether the original implementation could reorder writes;
- the exact concurrency test used;
- any implementation change made;
- the final ordering invariant;
- restart/cursor verification;
- remaining limits.

Add or extend the Phase 2 check command so the ordering contract is part of the normal regression suite.

## Explicit non-goals

Do not implement:

- DSH runtime/profile;
- DSH session mapping;
- DSH wake routing;
- per-DSH-session cursor persistence;
- MCP;
- business plugin migration;
- long-term memory;
- permissions;
- cross-conversation send;
- delivery ledger;
- execution claims;
- distributed ordering;
- multi-host journal replication.

## Acceptance criteria

Phase 2.1 is complete only when:

- concurrent OneBot events are exercised by deterministic tests;
- the relationship between capture order and `journal_id` is proven;
- if reordering was possible, the smallest necessary serialization mechanism is implemented;
- an inbound event cannot proceed into the normal AstrBot pipeline before its own journal row is committed;
- restart does not invalidate existing cursor semantics;
- `after(journal_id)` remains correct after the ordering change;
- Phase 1 and Phase 2 regressions still pass;
- no DSH/MCP/permission architecture has been added.

## Handoff to Phase 3

Only after Phase 2.1 is complete may Phase 3 treat `journal_id` as the normal DSH incremental cursor.

The next phase remains:

```text
Phase 3 — DSH minimal chat loop

AstrBot unhandled conversational event
        ↓
conversation ↔ DSH session mapping
        ↓
read journal entries after last_seen_journal_id
        ↓
DSH turn
        ↓
reply through AstrBot
        ↓
platform-confirmed journal row
```

Phase 4 remains MCP integration.
