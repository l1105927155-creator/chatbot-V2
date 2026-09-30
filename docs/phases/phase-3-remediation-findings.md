# Phase 3 remediation findings

> Status: completed, 2026-09-30
> Requirements: [Phase 3 remediation](phase-3-remediation.md)
> Original functional evidence: [Phase 3 findings](phase-3-findings.md)
> Historical startup validation below was removed by the completed
> [startup follow-up](phase-3-startup-validation.md).

## Changes

### One ACP process

One router owns one initialized `AcpRuntime` / official `HarnessClient`.
Native `session/new` assigns conversation IDs; persisted IDs are lazily resumed
on the shared connection. Each session has its own prompt/resume lock and
`session/update` notification filter. Distinct sessions may prompt concurrently.
Closing one session sends native `session/close` and leaves the process alive.
Shutdown attempts every active session close, then closes the shared transport,
even if one session fails. A failed resume never substitutes a new session ID or
closes the process used by another conversation.

The pinned Python transport correlates responses by request UUID, serializes
stdin writes, and creates separate notification subscription queues. Pinned ACP
owns a session map and a prompt slot per session. These native contracts support
the shared process without adding a broker or changing upstream core.

Storage is opened before ACP starts. If initialization is cancelled while the
synchronous startup thread runs, V2 drains that thread and closes its returned
client before propagating cancellation. Both failure paths have regressions.

### Deterministic handling outcome

The router no longer classifies `activated_handlers` by module or handler name.
V2 deterministic handlers set `event.set_extra("v2_deterministic_handled", True)`
when they take ownership, before sending/yielding a result. An observer that does
not take ownership leaves the marker unset. Stopped events and slash commands
also bypass DSH; otherwise eligible ordinary text reaches DSH.

Pinned AstrBot clears per-handler results before the next handler, so a late
router cannot reliably infer ownership from `event.get_result()`. The explicit
V2 marker is the integration contract. Plugins that reply to ordinary text must
adopt it (or stop the event); unmarked third-party replies are not automatically
classified. Adding a conforming plugin requires no central router identity edit.

The real pinned pipeline contract loads a second disposable deterministic
plugin, with no router identity changes. Its marked text receives only the
plugin reply. Ordinary text reaches DSH despite built-in passive observers;
`/v2probe` stays direct.

### Explicit provider/model configuration

`V2_DSH_PROVIDER` and `V2_DSH_MODEL` are required startup values. The structural
profile uses native Cordis `!!js process.env...` expressions, not a fixed model.
Credentials remain in the process environment appropriate for the provider.
Missing/blank/malformed configuration fails before launching the process.

An actual native startup test revealed that `session/new` rejects unknown
providers but accepts nonexistent DeepSeek model IDs. Pinned
`packages/llm/llm-deepseek/src/model-info.ts` supplies fallback metadata for
uncatalogued IDs; `packages/acp/acp/src/model-control.ts` inserts the current
model in its options. The advisory catalog is not a reliable validity whitelist.

V2 therefore creates a separate startup validation session, runs one short
`Startup configuration check. Reply only OK.` prompt, and closes it. No output
is sent to QQ or entered into the canonical journal, and it is never mapped to a
QQ conversation. This performs a real provider request, incurs model usage,
and leaves an isolated persisted DSH session. Provider/model/credential/service
failures reject router initialization. Native tests proved that an unknown
provider and a nonexistent model fail; the explicitly selected live model passed.
A future service outage can still occur after a successful startup check.

## Real QQ verification

Tests used the already authorized sender instance and only the Bot private chat
and test group. Replies used AstrBot and existing canonical `message_sent`
capture. No old chatbot business code or scripts were used.

| Step | Journal inbound → outbound | Actual platform inbound → outbound | Result |
| --- | --- | --- | --- |
| Private A, concurrent with group G | 68 → 70 | 868259164 → 578855051 | 梅花七号 |
| Group G, concurrent with private A | 69 → 71 | 178628230 → 193529287 | 松树八号 |
| Private `/v2probe` | 72 → 73 | 366497303 → 1211601271 | Direct probe reply; cursor unchanged |
| Private follow-up B | 74 → 75 | 869337799 → 737231310 | Correct private code and probe reply |
| Restart, private C | 76 → 77 | 254915795 → 1326301815 | 梅花七号 |
| Restart, group C | 78 → 79 | 1448932203 → 290276522 | 松树八号 |

All six outbound IDs/texts were also confirmed using read-only QQ `get_msg`.
The simultaneous private/group test had exactly one V2 ACP Node process,
PID 1676730. Replies contained only their respective conversation's code.

Both session IDs survived the integration restart:

- Private: `6ec74ba7-5817-445e-8e50-ced94b29705e`; cursor `68 → 74 → 76`.
- Group: `522d4300-99dd-4209-af99-93f8f74f82d4`; cursor `69 → 78`.

All successful turns cleared pending state. The private follow-up input delta
was `[70,72,73,74]`; restart inputs were private `[75,76]` and group `[71,78]`.
These were read from actual persisted DSH user-message inputs. Previously
consumed history was not reinjected. Latest model headers still had `tools=[]`.

A separate native check created two new sessions on one process, closed one
without stopping the process, then resumed both IDs after restarting the client.

## Automated verification and limits

`./scripts/check-phase3.sh`: **66 passed**, exit 0 (23 Phase 1, 14 Phase 2,
29 Phase 3). Coverage includes shared-client reuse, session isolation/serialization,
independent resume, failed-resume isolation, close-all despite errors, required
configuration, cancelled startup cleanup, storage failure before process launch,
the actual AstrBot pipeline, and prior journal/cursor/pending-turn regressions.
Independent focused review found no remaining material issue after startup
cleanup was corrected. Existing upstream deprecation warnings remain.

The temporary NapCat V2 client was removed and V2 stopped after verification;
legacy connection configuration was left untouched. Credentials were not committed.

Previously documented limits still apply: uncertain turns need manual recovery;
there is no multi-process/high-availability support; text-only acceptance does
not prove arbitrary plugins or media handling. The dependency pin/advisory limits
in the original findings remain. Phase 4/MCP has not been implemented.
