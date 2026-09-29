# Phase 1 — Minimal AstrBot Baseline

> Status: ready for implementation  
> Parent plan: [PLAN.md](../../PLAN.md)

## Goal

Establish a clean, reproducible AstrBot baseline for V2 and determine what AstrBot 4.28.2 actually records in its platform message history.

This phase is intentionally narrow.

It does **not** integrate DSH, implement MCP, design the final canonical journal, migrate old business plugins, or build the final permission model.

## Required outcome

At the end of Phase 1 we must know, from tests and runtime evidence:

1. whether AstrBot can run with its default conversational LLM path disabled;
2. whether deterministic commands/plugins still work normally;
3. exactly which inbound/outbound QQ message paths are persisted by AstrBot 4.28.2;
4. which gaps Phase 2 must solve for the canonical journal.

## Baseline

Use the versions pinned in `upstream.lock.json`.

Current AstrBot baseline:

- repository: `AstrBotDevs/AstrBot`
- release: `v4.28.2`

Do not develop against a floating upstream branch.

## Scope

### 1. Reproducible AstrBot checkout

Add the minimum bootstrap/development assets necessary to obtain and run the pinned AstrBot version without vendoring the full upstream source into this repository.

The generated/runtime checkout must live outside tracked project source, or in an ignored runtime directory.

### 2. Disable AstrBot default AI replies

Implement the smallest V2-owned AstrBot plugin/configuration needed to prevent the default AstrBot LLM reply path.

Requirements:

- ordinary messages must not reach AstrBot's default conversational LLM;
- plugin/command execution must remain available;
- plugin-local LLM use is not part of this phase unless an existing deterministic test plugin requires it;
- do not disable AstrBot globally in a way that also disables its platform/plugin runtime.

Prefer the documented event/plugin hook (for example `event.should_call_llm(False)`) rather than modifying AstrBot core.

### 3. Minimal deterministic command/plugin check

Use a tiny deterministic V2 test plugin or an existing built-in command to prove that:

```text
QQ/event -> AstrBot plugin/command -> deterministic response
```

still works while default AI replies are disabled.

Do not migrate Steam, Twitter, Bilibili, Moegirl, member memory, or other legacy business plugins in this phase.

### 4. PlatformMessageHistory coverage investigation

Test the pinned AstrBot behavior, not assumptions from documentation.

Build a coverage matrix for at least:

| Case | Expected investigation |
| --- | --- |
| group inbound user message | persisted or not |
| group default LLM response | persisted or not; may be tested in an isolated harness even though V2 disables it |
| group plugin/command response | persisted or not |
| group proactive `Context.send_message` | persisted or not |
| private inbound user message | persisted or not |
| private plugin/command response | persisted or not |
| private proactive send | persisted or not |

For each case record:

- code path tested;
- whether a row is written;
- stored role/sender/conversation key;
- whether a real platform message ID is available;
- notable content loss or normalization;
- whether the behavior depends on `group_message_history_enable`.

Do not infer one case from another. Test or inspect each relevant path directly.

### 5. Record the result

Add a Phase 1 findings document containing the coverage matrix and evidence.

Suggested path:

`docs/phases/phase-1-findings.md`

The findings must distinguish:

- verified behavior;
- source-code-only findings;
- runtime-tested behavior;
- unverified assumptions.

## Explicit non-goals

Do **not** implement any of the following in Phase 1:

- DSH session creation or routing;
- MCP server/client integration;
- final canonical journal storage;
- journal cursors;
- DSH history injection;
- long-term memory;
- owner/ordinary-user permission profiles;
- `tools/pre-execute` policy;
- cross-session send;
- policy ledger;
- execution claim;
- HMAC execution context;
- delivery ledger;
- OneBot fallback transport;
- business plugin migration;
- direct OneBot control from DSH.

If a missing Phase 1 requirement appears to need one of these, stop and document the dependency instead of expanding scope.

## Upstream modification rule

A missing `PlatformMessageHistory` behavior is **not** permission to patch AstrBot core during this phase.

If a gap is found:

1. reproduce it;
2. record the relevant upstream code path;
3. write the expected V2 journal requirement;
4. leave the implementation decision to Phase 2.

Phase 2 may choose:

- a thin AstrBot plugin/adapter;
- reuse of `PlatformMessageHistory`;
- a separate V2 journal table;
- another minimal extension point.

Phase 1 must not choose prematurely.

## Expected repository changes

Likely files/directories:

```text
AGENTS.md
upstream.lock.json               # only if correcting a verified pin
.gitignore
scripts/
  bootstrap.*
astrbot-plugins/
  v2_ai_gate/                    # or an equivalently small plugin
  v2_phase1_probe/               # only if needed for deterministic tests
tests/
  ...
docs/phases/
  phase-1.md
  phase-1-findings.md
```

Do not create unused directory scaffolding.

## Acceptance criteria

Phase 1 is complete only when all of the following are true:

- the pinned AstrBot baseline can be reproduced from this repository;
- AstrBot default conversational LLM replies are disabled by V2-owned integration code/configuration;
- at least one deterministic AstrBot command/plugin still responds correctly;
- the seven history cases above have an explicit result;
- discovered history gaps are documented instead of silently patched into upstream;
- no DSH/MCP/business-plugin/permission architecture has been implemented;
- tests or controlled runtime evidence are recorded in `phase-1-findings.md`.

## Handoff to Phase 2

Phase 2 begins with one decision only:

> Can AstrBot's existing history storage satisfy the V2 canonical journal contract through a thin adapter, or does V2 need its own journal table?

That decision must be based on Phase 1 evidence.
