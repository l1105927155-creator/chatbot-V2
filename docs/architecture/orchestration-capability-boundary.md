# Architecture boundary — orchestration vs capability

> Status: frozen review principle  
> Applies from: Phase 4 onward  
> Parent plan: [PLAN.md](../../PLAN.md)

## Why this boundary exists

V2 exists to simplify long-term development around two existing frameworks:

- AstrBot owns QQ integration and deterministic capabilities.
- DSH owns free-form reasoning and session cognition.
- The canonical journal owns QQ-visible conversation facts.
- MCP is the capability boundary between DSH and AstrBot.

Phase 4 proved that MCP works, but it also exposed a structural risk: the DSH router already has access to conversation identity, session state, journal cursor, AstrBot callbacks, and DSH runtime state. That makes it the easiest place to attach every new cross-framework feature.

If that convenience becomes the default development path, the router will gradually become a new integration platform and V2 will reproduce the maintenance problem it was created to remove.

## Frozen principle

**Conversation orchestration and capability provision are separate responsibilities.**

The DSH router owns conversation execution:

- decide whether an AstrBot event should wake DSH;
- map a QQ conversation to its DSH session;
- serialize turns for that conversation;
- maintain journal cursor and turn-recovery state;
- coordinate the normal DSH reply path.

The MCP capability boundary owns AstrBot capabilities exposed to DSH:

- define stable typed capability contracts;
- adapt AstrBot-owned services/data to those contracts;
- expose canonical-history access;
- expose allowed platform actions;
- evolve as business capabilities are added.

A new AstrBot business capability should not require the router to understand that capability's business meaning.

## Review test

For every later feature, ask:

> If this is a new AstrBot capability, why does the DSH router need to change?

A router change is justified when the feature changes conversation scheduling, session continuity, turn admission, or cursor/recovery semantics.

A router change is a warning sign when it exists only because a new tool, business service, data source, or permissioned action was added.

## Desired growth pattern

As Phase 5 and Phase 6 expand the system, the expected growth is:

```text
new business capability
        ↓
AstrBot service/plugin
        ↓
MCP capability contract
        ↓
DSH may use it
```

The router should remain largely stable.

Likewise, permission policy may change which capabilities a DSH session can use, but that should not require the router to become the capability registry or business-policy engine.

## Acceptance for future phases

The architecture remains aligned when:

- adding a new typed AstrBot capability does not require teaching the router that capability's business logic;
- router changes remain tied to conversation/session/cursor behavior;
- capability contracts can evolve independently of DSH wake-routing logic;
- the canonical journal remains the only QQ history source;
- normal QQ sends still pass through AstrBot;
- no new bridge-style platform emerges between AstrBot and DSH.

Physical packaging is secondary. The boundary may be implemented as separate plugins, separate services within one plugin, or another small composition that fits AstrBot lifecycle. Review focuses on responsibility and change coupling, not directory layout.
