# AGENTS.md

## Purpose

This repository is the clean V2 integration project for AstrBot + DeepSeek Harness (DSH).

Before changing code, read:

1. `PLAN.md`
2. the current phase document under `docs/phases/`
3. `upstream.lock.json`

`PLAN.md` is the current architecture source of truth. Phase documents define the implementation boundary for the current stage.

## Working rules

- Be concise by default.
- Answer the actual task immediately.
- Verify before assuming.
- Investigate repository code, upstream source, tests, logs, configuration, and available tools before changing behavior.
- Do not treat a plausible explanation as established fact.
- Resolve technical uncertainty yourself when evidence can answer it.
- Ask the user only when a decision depends on product/business intent or information that cannot be verified.
- Do not invent product behavior, permission rules, routing rules, or compatibility requirements.

## Architecture constraints

Do not change these principles unless the user explicitly asks to revisit the architecture:

1. There is one logical canonical journal for QQ-visible conversation history.
2. AstrBot does not own free-form conversational AI replies.
3. DSH is the cognitive Agent, but never the source of authorization.
4. Normal QQ send/receive traffic goes through AstrBot; DSH does not bypass AstrBot to use OneBot as the normal chat path.

Prefer native AstrBot and DSH extension points over new middleware.

Do not introduce a new bridge, policy platform, ledger, broker, or duplicate runtime capability unless the current phase proves that the upstream frameworks cannot satisfy a required contract.

## Phase discipline

Implement only the current phase.

Do not implement later-phase capabilities "while here", even if they appear easy or useful.

In particular:

- do not pre-build MCP features before the MCP phase;
- do not migrate member memory before the memory phase;
- do not migrate the old policy ledger, execution claims, HMAC execution context, or delivery ledger by default;
- do not copy the old `chatbot` repository wholesale;
- do not fork or modify upstream AstrBot/DSH core just because an integration gap is discovered.

When an upstream gap is found:

1. record the exact observed behavior;
2. add a reproducible test or evidence when practical;
3. document the gap;
4. defer the architectural response to the phase decision point unless the current task explicitly requires the fix.

## Old repository policy

The old `l1105927155-creator/chatbot` repository is reference material only.

Code may be migrated selectively only when:

- the current phase requires the capability;
- the migrated code still matches the V2 architecture;
- dependencies and behavior are understood;
- copying it is simpler and safer than reimplementation.

Do not inherit old architecture merely because working code already exists there.

## Upstream policy

Use versions pinned in `upstream.lock.json`.

Do not silently switch to latest `master`, another release, or a locally installed version.

If upstream behavior must be inspected, inspect the pinned version first.

Prefer extension/plugin APIs over patches to upstream source.

If a core patch becomes necessary, document:

- the exact upstream limitation;
- why plugin/configuration hooks are insufficient;
- the smallest patch surface;
- how the patch is tested;
- how it can be removed after an upstream fix.

## Before implementation

For each requested development task, first determine:

- the concrete behavior being changed;
- the files expected to change;
- the evidence needed to verify the implementation;
- whether any decision depends on product intent.

For non-trivial work, state the modification plan and involved files before making changes.

Do not end with "waiting for confirmation" when the task is already sufficiently specified. Continue unless a real product decision is missing.

## Testing and completion

Do not report work as complete merely because code was written.

Verify the relevant behavior with the strongest practical evidence available:

- unit tests;
- integration tests;
- upstream behavior tests;
- runtime logs;
- controlled end-to-end checks.

Report:

- what changed;
- which files changed;
- what was actually tested;
- what remains unverified;
- any discovered upstream limitation.

Do not hide failed tests or replace evidence with assumptions.
