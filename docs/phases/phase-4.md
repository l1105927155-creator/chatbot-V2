# Phase 4 — AstrBot MCP capability boundary

> Status: completed (2026-09-30)
> Evidence: [Phase 4 findings](phase-4-findings.md)
> Parent plan: [PLAN.md](../../PLAN.md)

## Product goal

Give DSH a standard, typed way to use AstrBot-owned information and capabilities while keeping the existing division of responsibility intact:

- AstrBot owns QQ integration, deterministic capabilities, and canonical QQ history.
- DSH owns free-form reasoning and decides when a capability is useful.
- MCP is the contract between them.

Phase 4 is successful when DSH can use an AstrBot capability as part of a normal conversation without introducing a second history system or bypassing AstrBot's normal QQ path.

## Capability set for this phase

The first MCP surface must prove three product needs.

### Historical context

DSH can request older canonical-journal context when the normal journal delta is not enough.

This should let the Agent answer questions that depend on earlier conversation history without replaying the full QQ history into every turn.

### One real AstrBot capability

Expose one concrete AstrBot-owned deterministic capability through a typed MCP tool.

The purpose is to prove that an existing AstrBot capability can be reused by DSH as a service contract, then incorporated into a natural-language answer.

Choose a capability whose result can be verified deterministically in tests and in one live conversation.

### Current-conversation output

DSH can request an AstrBot-managed send to the current QQ conversation when a tool workflow needs an explicit platform-side output.

The resulting QQ-visible message must still appear in the canonical journal through the normal platform path.

## Conversation behavior

The expected user experience is:

```text
user asks a question requiring an AstrBot capability
        ↓
DSH understands the request
        ↓
DSH calls the typed AstrBot capability through MCP
        ↓
DSH receives the structured result
        ↓
DSH explains the result naturally
        ↓
reply reaches QQ through AstrBot
        ↓
canonical journal records the visible exchange
```

The same DSH session and journal cursor model established in Phase 3 remains the conversation backbone.

## Contract quality

The first MCP tools should expose stable business meaning rather than AstrBot implementation details.

For example, a tool should represent “query this capability” or “read this history window”, not “invoke this handler function”.

Tool inputs and outputs should be explicit enough that they can be tested independently of prompt wording.

## Failure behavior

A capability failure should be visible to DSH as a tool failure that can be explained or recovered from in the same conversational turn.

A failed MCP call must not make the journal or DSH cursor claim that an action occurred when it did not.

## Acceptance stories

Phase 4 is complete when all of the following are demonstrated.

### Story A — older history

1. A QQ conversation contains relevant information older than the current journal delta.
2. The user asks about that earlier information.
3. DSH uses the history capability and answers with the correct older context.
4. The normal session remains the same; the full history was not replayed into every turn.

### Story B — AstrBot capability

1. The user asks for information that requires the selected AstrBot capability.
2. DSH calls the typed MCP tool.
3. The returned data matches the deterministic AstrBot capability result.
4. DSH continues the conversation using that result.

### Story C — current-conversation send

1. A DSH tool workflow requests an explicit QQ-visible send to the current conversation.
2. AstrBot performs the send.
3. The actual QQ message appears in the canonical journal with the platform-confirmed message identity available from the existing capture path.
4. A later DSH turn can observe that output through the same journal history.

### Story D — restart continuity

After the MCP-enabled deployment is restarted, an existing QQ conversation resumes its prior DSH session and can continue using the Phase 4 capabilities without losing its history relationship.

## Evidence required

The completion record should identify:

- the exact capability exposed;
- the MCP contracts presented to DSH;
- automated contract tests;
- one live QQ capability invocation;
- journal evidence for any QQ-visible output;
- restart evidence using an existing conversation/session.

Phase 4 findings should be recorded in:

`docs/phases/phase-4-findings.md`
