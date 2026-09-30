# Phase 3 — DSH Minimal Chat Loop

> Status: completed (2026-09-30)
> Parent plan: [PLAN.md](../../PLAN.md)
> Input evidence: [Phase 2.1 findings](phase-2.1-findings.md)
> Acceptance evidence: [Phase 3 findings](phase-3-findings.md)

## Goal

Connect DeepSeek Harness (DSH) to the existing AstrBot + canonical journal path and complete the first natural-language chat loop.

The target behavior is:

```text
QQ message
   ↓
AstrBot
   ↓
deterministic plugin handled?
   ├─ yes → AstrBot replies normally
   └─ no  → route to DSH
                ↓
        read journal delta
                ↓
             DSH turn
                ↓
        reply through AstrBot
                ↓
              QQ
                ↓
         journal feedback
```

Phase 3 is complete when DSH can participate in the same conversation history as AstrBot plugin replies without maintaining a second QQ history.

## 1. Pin and bootstrap DSH

Use the DSH version already pinned in `upstream.lock.json`.

Add the minimum bootstrap/runtime assets needed to run that pinned DSH version reproducibly alongside the existing AstrBot runtime.

Keep DSH runtime data separate from tracked source. The repository should retain only V2-owned profile, routing, tests, and integration code.

## 2. Conversation to DSH session mapping

Define a stable mapping:

```text
(platform_id, bot_id, conversation_key)
        ↕
DSH session
```

The mapping must survive process restart.

One QQ conversation maps to one logical DSH session unless later evidence requires a different model.

The journal conversation identity remains the source identity; DSH session IDs are integration state, not conversation identity.

## 3. Route only unhandled conversational events

Implement a thin AstrBot router that runs after deterministic plugin/command handling has had a chance to respond.

A message should reach DSH when:

- it belongs to a supported QQ conversation;
- it has not already been handled by an AstrBot command/plugin;
- it represents normal conversational input.

The router should implement deterministic AstrBot behavior first and free-form reasoning second, using only the current V2 journal/session model and the pinned upstream interfaces. The old `chatbot` project is not a source for implementation or runtime assets.

## 4. Journal delta into each DSH turn

Each mapped DSH session maintains:

```text
last_seen_journal_id
```

Before a DSH turn:

1. read journal entries for that conversation after the saved cursor;
2. convert those entries into DSH-visible conversation input;
3. run the DSH turn;
4. advance the cursor only after the turn has accepted the intended journal delta.

The normal path should inject only new journal entries rather than replaying the full QQ history on every turn.

The journal remains authoritative for QQ-visible conversation sequence.

## 5. Per-conversation serialization

Two DSH turns for the same QQ conversation must not run concurrently.

Use the smallest per-conversation serialization mechanism needed to guarantee:

```text
turn N journal delta
   → DSH turn N
   → cursor update
   → turn N+1
```

Different conversations may proceed independently.

This protects both DSH session order and journal cursor progression.

## 6. DSH reply through AstrBot

DSH output should return through AstrBot's existing platform path.

The first implementation only needs a normal text reply path.

After AstrBot sends the reply, NapCat `message_sent` feedback should create the canonical outbound journal row exactly as established in Phase 2.

The next DSH turn must therefore observe its own prior QQ-visible reply through the same journal mechanism as every other Bot output.

## 7. Cursor update semantics

Define cursor advancement precisely.

At minimum:

- the cursor is scoped to one mapped conversation/session;
- it stores the highest journal row successfully included in the DSH turn input;
- restart restores it;
- duplicate wake attempts do not replay already-consumed journal rows;
- journal rows created during an active DSH turn remain for the next delta unless intentionally included by the current turn.

Persist the session mapping and cursor together or in one small integration store if that keeps lifecycle and recovery simpler.

## 8. DSH profile

Add the minimum V2-owned DSH profile/instructions needed for this chat loop.

The profile should establish:

- DSH is the free-form conversational Agent;
- QQ-visible history comes from the V2-provided journal delta;
- replies are returned through the V2/AstrBot integration path.

Keep the profile focused on runtime role and conversation behavior.

## 9. First end-to-end acceptance story

Use one controlled QQ conversation and verify this exact sequence:

```text
User: ordinary conversational message
DSH: natural-language reply

User: /v2probe
AstrBot: v2 phase 1 probe: ok

User: ordinary follow-up referring to the command result
DSH: receives the intervening user command and AstrBot reply from the journal,
     then responds with that context available
```

Also verify restart continuity:

1. complete at least one DSH turn;
2. restart the V2 integration;
3. send another conversational message;
4. confirm the same DSH session mapping/cursor resumes without replaying the full prior journal.

## 10. Tests

Add coverage for:

- stable conversation → DSH session mapping;
- per-conversation serialization;
- journal delta construction;
- cursor advancement;
- restart recovery;
- deterministic AstrBot command handled without waking DSH;
- unhandled conversational message wakes DSH;
- DSH reply sent through AstrBot and later visible in the journal;
- command reply occurring between two DSH turns is visible to the second DSH turn.

Use controlled fakes for unit/contract tests and one live QQ end-to-end check for the final path.

## Expected repository changes

Likely paths:

```text
docs/phases/
  phase-3.md
  phase-3-findings.md

astrbot-plugins/
  v2_dsh_router/

dsh/
  profile/
  ...

tests/
  contracts/
  e2e/
```

The exact persistence file/module for conversation-session mapping and cursor may be chosen during implementation based on the smallest clear design.

## Acceptance criteria

Phase 3 is complete when:

- the pinned DSH runtime can be reproduced;
- one QQ conversation maps persistently to one DSH session;
- deterministic AstrBot command/plugin replies remain direct;
- unhandled conversational input reaches DSH;
- each DSH turn receives only the journal delta after its saved cursor;
- turns are serialized per conversation;
- DSH replies return through AstrBot and appear in the canonical journal;
- a later DSH turn sees intervening AstrBot plugin output;
- restart restores session mapping and cursor;
- the end-to-end acceptance story passes.

## Implementation review before Phase 4

The chat-loop acceptance story is complete. Before Phase 4, apply the implementation-alignment review in [Phase 3 remediation](phase-3-remediation.md).

## Handoff to Phase 4

After that remediation is complete, Phase 4 adds the first MCP boundary:

- journal/history reads;
- one typed AstrBot capability;
- current-conversation reply/send capability.
