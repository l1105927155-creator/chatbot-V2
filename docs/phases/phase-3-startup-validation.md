# Phase 3 follow-up — startup validation side effect

> Status: ready for implementation  
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
