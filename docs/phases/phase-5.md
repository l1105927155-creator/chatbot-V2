# Phase 5 — DSH native capability boundaries and minimal integration

> Status: remediation-first; native validation must precede minimal integration  
> Parent plan: [PLAN.md](../../PLAN.md)  
> Required development entry: [Phase 5 remediation](phase-5-remediation.md)  
> Architecture boundary: [orchestration vs capability](../architecture/orchestration-capability-boundary.md)

## Product goal

Use the pinned DSH runtime's native tool restrictions, execution guards, sandbox
and approval mechanisms to enforce the capabilities intended for each QQ turn.
Retain owner-scoped system operations. Keep AstrBot's capabilities available
through MCP and guides without rebuilding DSH execution in V2.

Authority comes from trusted inbound source facts and explicit configuration,
not model judgment, prompt wording, nicknames, quoted history or remembered
identity. A group conversation/session is not permanently an owner identity.

Current Phase 4 tools and disabled shell profile describe the implemented
baseline, not the final product capability ceiling.

## Required sequence

1. Follow the remediation document's native verification requirements. Record
   actual behavior, failures and scope limits in
   `docs/phases/phase-5-native-findings.md`.
2. After evidence establishes feasibility, connect only the missing source,
   action-correlation and one-shot confirmation pieces through thin adapters.
   Preserve native DSH tool execution.
3. If satisfying a requirement needs a different authorization architecture,
   record the reproducible upstream gap and return to requirements review.
   Do not build an alternative policy/approval platform during this phase.

The existence of an API is not proof that the current integration enforces it.
In particular, `tools.restrict()` masks global tools; scope-local registrations
remain visible. Native, Agent-local and MCP execution paths need separate
verification. ACP has one-shot permission requests, but the current V2 client
does not implement that interaction.

## Responsibilities and limits

- Router owns turn admission, session mapping, serialization, cursor and
  recovery. It may transport trusted event facts but does not own business
  capability definitions or an expanding permission platform.
- DSH native mechanisms own system-tool execution and the enforced capability
  boundary. A QQ approval adapter correlates the exact native action and
  returns a one-shot decision; it does not reimplement file/shell tools.
- MCP providers retain argument, current-origin and active-turn checks.
  Conversation-scoped transport tokens are not owner roles or approvals.
- Preserve the five Phase 4 MCP tool contracts. Do not add V2 workspace
  mutation MCP tools by default, a role database, policy ledger, execution
  claims, HMAC execution context or a separate approval service.
- Packaging follows actual lifecycle needs; no forced plugin/process split.
- Retain owner `1105927155` as the already confirmed deployment/test identity.
  Owner workspace write/delete remain required sensitive acceptance actions
  through the native DSH capability chain. Other system tools are opened only
  as required by verified scenarios; owner status is not unlimited authority.

## Required acceptance

- Ordinary users retain chat, scoped history, existing AstrBot queries and
  current-conversation output.
- Direct invocation tests independently of model compliance prove denied
  dangerous operations have no side effects; injection cannot expand authority.
- Owner writes and deletes only disposable content in an isolated DSH workspace
  through native tools, with no side effect before exact-action confirmation.
- Reject, cancel, timeout, disconnect, mismatched correlation and replay cannot
  authorize execution. Changed action/path/content requires its own decision.
- Same-group sender changes, concurrent conversations and restart do not
  transfer capability grants or one-shot approvals.
- Existing sessions, cursors, deterministic-command interleaving, MCP output
  journal capture and startup without inference/session probes remain valid.

Only one controllable QQ account is available: the already authorized temporary
owner-config removal/restoration in isolated acceptance may prove ordinary and
owner behavior. Direct programmatic tests must separately cover same-group
different-sender isolation. Do not narrow permanent product behavior to simplify
live testing.

Record actual implementation and test evidence, including limitations, in
`docs/phases/phase-5-findings.md`. Historical Phase 4 tests are not Phase 5
permission/approval acceptance. Full details and evidence requirements are in
[the remediation document](phase-5-remediation.md).

## Handoff to Phase 6

Business migration starts after native boundaries and minimal integration pass
acceptance while keeping Router business knowledge stable. A generic V2
authority platform is not a prerequisite.
