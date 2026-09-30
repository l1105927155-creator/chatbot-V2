# Phase 3 follow-up — startup validation side effect

> Status: completed (2026-09-30)
> Review target: `3ecb6553f5c35421e2fe4e1c313e08bbb486cfbd`  
> Parent plan: [PLAN.md](../../PLAN.md)

## Why this needs correction

Phase 3 now requires explicit provider/model configuration, which matches the product goal.

The current startup path goes further: every router start performs a real model request in a separate validation session. That creates recurring model usage and a persistent DSH session unrelated to any QQ conversation.

Those side effects do not improve the core product guarantee. A successful startup request also cannot prove that the provider will still be available when a real user message arrives.

## Required outcome

Startup should establish only that the configured V2 runtime can be initialized with an explicit provider/model selection.

Actual provider/model availability is proven by real DSH turns when users invoke the conversational path.

A failed real turn must remain a failed turn: it must not be recorded as successfully consumed conversation progress.

## Acceptance

- restarting the V2 router does not itself consume a model request;
- restarting the V2 router does not create an unrelated persistent DSH session;
- missing or malformed provider/model configuration still fails clearly;
- real provider/model failures are visible on the actual DSH turn that encountered them;
- the existing session mapping, journal cursor, restart recovery, and QQ chat-loop behavior remain unchanged.

When these conditions pass, Phase 3 is fully closed and Phase 4 can start.


## Implementation and verification

Removed the startup-only `session/new`, `session/prompt`, and `session/close`
sequence from `AcpRuntime`. Startup sends only ACP `initialize`; explicit
provider/model environment configuration, capability negotiation, shared
process ownership, and cancellation cleanup remain in place. Availability
errors now surface when a real session/turn uses the selected route.

Evidence:

- `./scripts/check-phase3.sh`: **68 passed**, exit 0 (23 Phase 1, 14 Phase 2,
  31 Phase 3); upstream deprecation warnings remain.
- A transport contract verifies that two client starts issue only `initialize`,
  never session creation or a prompt. Missing/malformed configuration and
  startup failure/cancellation cleanup regressions remain covered.
- Two native ACP starts using an isolated DSH home produced zero session log
  files. Two starts against the existing V2 home resumed both prior private and
  group session IDs, then closed them. Session-file identities, model request
  header counts, conversation mappings, and saved cursors were unchanged.
- An explicitly invoked integration turn with a nonexistent model ID used the
  actual pinned ACP/provider route and an isolated synthetic journal/state.
  It failed with `JsonRpcError`; no send occurred, the saved cursor stayed 0,
  and the pending upper cursor was retained. The startup itself created no
  session. Service regressions also verify failure survival across restart and
  no automatic replay.

This follow-up did not send new live QQ messages. The actual pinned AstrBot
pipeline regression passed, and the prior real QQ evidence in
[remediation findings](phase-3-remediation-findings.md) remains the chat-loop
baseline. No normal turn/send/journal logic or upstream source was changed.
Earlier validation-only sessions are historical artifacts; they were not
removed or rebound to QQ conversations. Phase 4 can now begin.
