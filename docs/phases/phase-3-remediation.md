# Phase 3 remediation — align the DSH integration with V2 goals

> Status: completed (2026-09-30)
> Verification: [remediation findings](phase-3-remediation-findings.md)
> Review target: commit `ce9075c58854c167a0b45a065df3ed1985415d9d`  
> Parent plan: [PLAN.md](../../PLAN.md)  
> Phase 3 evidence: [phase-3-findings.md](phase-3-findings.md)

## Purpose

Phase 3 meets its functional acceptance story, but review against the V2 product goals found three implementation choices that should be corrected before Phase 4 builds on them.

The purpose of this remediation is not to redesign the working chat loop. It is to keep the integration layer small, make native DSH/AstrBot capabilities do the work they already support, and avoid turning Phase 3 implementation details into long-term architecture.

## 1. Use one ACP runtime for multiple DSH sessions

### Observed implementation

`DshClient` currently creates and retains one `AcpRuntime` process per mapped QQ conversation.

Conceptually:

```text
conversation A -> ACP process A -> session A
conversation B -> ACP process B -> session B
conversation C -> ACP process C -> session C
```

### Why this drifts from the requirement

The V2 goal is one maintainable integration between AstrBot and DSH, with QQ conversations mapped to DSH sessions.

The pinned DSH ACP implementation already owns multiple independent sessions on one connection. Its documented contract states that one connection can run several sessions at once, and the server stores active sessions in its own session map.

Creating a separate DSH process per conversation duplicates lifecycle management that the upstream runtime already provides. It also makes later MCP attachment and process supervision scale with conversation count rather than with the V2 runtime itself.

### Target shape

```text
AstrBot / v2_dsh_router
        ↓
one DSH ACP runtime
        ├─ QQ conversation A -> DSH session A
        ├─ QQ conversation B -> DSH session B
        └─ QQ conversation C -> DSH session C
```

The persistent mapping remains:

```text
(platform_id, bot_id, conversation_key) -> session_id
```

On restart, the shared ACP runtime resumes the mapped sessions as they are needed.

### Acceptance

- one router instance owns one ACP runtime process;
- multiple QQ conversations can create/use independent sessions through that process;
- per-conversation DSH turns remain independent and correctly serialized;
- restart resumes existing session IDs;
- closing one session does not terminate other sessions;
- the existing private/group/restart acceptance story still passes.

## 2. Make routing depend on handling outcome, not a growing handler identity table

### Observed implementation

`routing.py` currently contains an `_OBSERVERS` set of exact AstrBot module/handler identities.

An event is withheld from DSH when `activated_handlers` contains a handler that the router does not classify as an observer.

This was sufficient to pass the pinned AstrBot Phase 3 tests, but it makes the router increasingly aware of AstrBot internals and every plugin that may participate in message processing.

### Why this drifts from the requirement

The product rule is simple:

> deterministic AstrBot capability first; if it actually handles the message, it owns the reply; otherwise DSH handles free-form conversation.

The router should therefore consume a stable handling outcome, not maintain a catalog of handlers that are presumed harmless or authoritative.

The current probe already demonstrates a useful V2-owned signal:

```python
event.set_extra("v2_deterministic_handled", True)
```

The remediation should establish the smallest reliable handling contract that works with AstrBot's pinned pipeline and V2-owned plugins.

### Target shape

The decision should reduce to semantics close to:

```text
event actually handled by deterministic AstrBot path
    -> no DSH turn

event remains ordinary conversational input
    -> DSH turn
```

The implementation may use a V2 event marker, a suitable native event/result lifecycle signal, or a combination proven against the pinned pipeline. The important result is that adding an unrelated deterministic plugin does not require editing a central module/handler-name allowlist.

### Acceptance

- `_OBSERVERS`-style exact handler identity classification is no longer the primary routing contract;
- the probe command still bypasses DSH;
- ordinary text still reaches DSH;
- a second test deterministic plugin can handle a message without adding its module/handler name to router source;
- passive AstrBot observers do not accidentally suppress ordinary DSH chat;
- routing behavior remains covered against the actual pinned AstrBot pipeline.

## 3. Move model selection out of the fixed QQ profile

### Observed implementation

`dsh/profile/v2-qq.patch.yml` currently fixes:

```yaml
provider: deepseek-official
model: deepseek-v4-flash
```

### Why this drifts from the requirement

The repository pins the DSH runtime version. It does not define one permanent conversational model as part of the V2 architecture.

Provider/model selection is runtime configuration. Keeping a specific model in the structural profile turns a Phase 3 test choice into a repository-level product decision and makes later model changes require editing the integration composition.

### Target shape

Keep the V2 profile responsible for the QQ Agent composition and expose provider/model selection through explicit runtime configuration.

Tests may still supply a fixed known provider/model so their behavior is reproducible.

### Acceptance

- the reusable QQ profile does not hard-code `deepseek-v4-flash`;
- startup fails clearly when required model configuration is absent or invalid;
- tests set their provider/model explicitly;
- the live chat-loop acceptance still passes with an explicitly selected model.

## Completion criteria

This remediation is complete when all three corrections are implemented and the Phase 3 acceptance suite remains green.

The resulting baseline should still provide:

```text
QQ
 -> AstrBot deterministic handling
 -> otherwise DSH session
 -> journal delta
 -> DSH reply through AstrBot
 -> platform-confirmed journal row
```

with one shared DSH ACP runtime managing the conversation sessions and a routing contract based on actual message ownership rather than a growing catalog of plugin identities.

After that baseline is stable, Phase 4 can add the MCP boundary without inheriting these Phase 3 implementation choices.


## Follow-up review

A later review found one side effect in the model-configuration validation added by this remediation: router startup performs a real model request and leaves a persistent validation-only DSH session.

That issue is tracked separately in [Phase 3 startup-validation follow-up](phase-3-startup-validation.md). The three architecture corrections in this document remain accepted.
