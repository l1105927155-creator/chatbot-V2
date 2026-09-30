# Phase 5 — Programmatic permissions

> Status: ready for implementation  
> Parent plan: [PLAN.md](../../PLAN.md)  
> Architecture boundary: [orchestration vs capability](../architecture/orchestration-capability-boundary.md)

## Product goal

Make DSH capability access depend on programmatic authority rather than on model judgment, prompt wording, remembered identity, or user-provided text.

Phase 5 should establish a permission model that can support later business-capability migration without turning the router into a policy engine.

The user-visible goal is simple:

- ordinary QQ conversations get only the capabilities intended for ordinary users;
- owner conversations may receive additional administrative/system capabilities;
- sensitive actions require the level of confirmation appropriate to their effect;
- a model cannot expand its own authority by asking for, inventing, or remembering a different role.

## Authority source

The effective authority for a turn must come from trusted integration context tied to the actual QQ source.

The decision must be based on facts such as the authenticated platform/bot/conversation/user identity known to the integration.

Prompt text, display names, quoted messages, long-term memory, model output, or MCP arguments do not establish identity or role.

## Ordinary QQ capability set

An ordinary QQ Agent should be able to perform the normal conversational work already established in Phase 4:

- read permitted canonical history;
- use explicitly exposed safe AstrBot query capabilities;
- produce output to the current conversation.

The ordinary Agent should not gain system-management or unrelated-conversation authority merely because such capabilities exist elsewhere in the V2 deployment.

## Owner capability set

An owner conversation may receive additional capabilities needed for system maintenance or administration.

Owner authority should be additive and explicit: the same conversation identity should reliably receive the same intended role after restart, and an ordinary conversation should not become owner through conversational content.

This phase only needs to prove one meaningful owner-only capability end to end. It does not need to define every future administrative tool.

## Sensitive action behavior

Capabilities with materially greater side effects should not become equivalent to ordinary read-only/query tools.

Where an action requires explicit user approval, the approval must correspond to the actual pending action and must not be satisfied by unrelated prior conversation text.

The exact approval mechanism may follow the strongest suitable primitive available in the pinned DSH/AstrBot stack.

## Capability boundary

Permission decisions apply to capabilities exposed through the MCP boundary and to DSH-native tools available to the QQ Agent.

The router remains responsible for conversation/session execution. It should not become the registry of business capabilities or the main policy engine.

A capability provider must still enforce constraints that belong to the capability itself, such as current-conversation targeting or argument validity.

## Required acceptance stories

### Story A — ordinary user

1. An ordinary QQ user starts a normal DSH conversation.
2. The Agent can use the safe Phase 4 capabilities needed for that conversation.
3. Owner/system capabilities are not available for successful execution.
4. Asking the model to act as the owner does not change that result.

### Story B — owner

1. The configured owner starts a DSH conversation.
2. The Agent receives the intended owner capability set.
3. One owner-only capability succeeds.
4. Restart preserves the same authority assignment for that conversation.

### Story C — prompt injection

1. An ordinary user supplies text that explicitly instructs the Agent to ignore restrictions, impersonate the owner, or invoke an unavailable privileged capability.
2. The effective capability authority remains unchanged.
3. No privileged side effect occurs.

### Story D — sensitive approval

1. The owner requests one action classified as requiring confirmation.
2. The action does not occur before the required approval.
3. Approval of that specific action permits it to complete.
4. The resulting side effect is observable through the authoritative system responsible for that action.

### Story E — conversation isolation

1. Two QQ conversations with different authority levels are active in the same V2 runtime.
2. Their capability sets remain independent.
3. Tool use or state in one conversation does not grant authority to the other.

## Evidence required

The completion record should include:

- the trusted identity/role source used by the deployment;
- the ordinary and owner capability sets demonstrated in tests;
- one owner-only capability used successfully;
- one denied ordinary-user attempt;
- one prompt-injection attempt that fails to expand authority;
- one approval-required action, if such an action is included in the phase;
- restart evidence;
- concurrent conversation-isolation evidence.

Automated tests should verify authorization independently from model compliance. Live QQ acceptance should prove at least the ordinary/owner distinction and one real privileged action or privileged capability result.

Record completion evidence in:

`docs/phases/phase-5-findings.md`

## Handoff to Phase 6

Phase 6 may begin once adding a new business capability can rely on the Phase 5 authority model without modifying router business logic.

At that point, feature migration can focus on AstrBot service contracts and MCP exposure rather than rebuilding identity or permission handling for each feature.
