# Phase 2.1 findings: journal observation order

## Result

The Phase 2 implementation preserved capture order under ordinary concurrent
delivery, but **could reorder on cancellation**: cancelling a callback while
its `asyncio.to_thread()` call was still writing released `_write_lock` before
the SQLite thread finished. An independent review reproduced B receiving
`journal_id=1` while earlier A, held in its worker thread, later received `2`.

The fix schedules one writer task before the callback's first suspension,
shields it from callback cancellation, and keeps `_write_lock` until its SQLite
thread has finished even if the writer task is cancelled. The callback waits
for its writer to finish before propagating cancellation. Plugin termination
unsubscribes the hooks and drains writers already registered by callbacks;
cancellation of termination itself does not cancel queued writers. A callback
which the event bus had queued but which starts after termination begins is
explicitly rejected and logged as an incomplete-history error. No new queue or
database table was added. For one V2 process and one aiocqhttp client, the
final implementation assigns `journal_id` in before-hook callback order for
successfully recorded events.

This is an **observation-order** guarantee at the V2 root before hook. It does
not claim that callbacks begin in the same order as WebSocket frames when
another, more specific aiocqhttp before hook delays an earlier event. The
pinned aiocqhttp reverse-WebSocket receiver creates one task per payload and
the event bus runs more specific before hooks ahead of root hooks. Phase 3 must
use the committed journal cursor, not OneBot's second-resolution `time` field
or assumptions about frame arrival order.

## Reproduction and evidence

The pinned aiocqhttp event bus is exercised in
`tests/contracts/test_journal_plugin.py::test_capture_order_survives_concurrent_slow_write`.
Five deterministic cases cover:

| Case | Events |
| --- | --- |
| inbound then inbound | two private messages |
| inbound then outbound | private user message and Bot feedback |
| outbound then inbound | group Bot feedback and user message |
| cross conversation | group and private events interleaved |
| burst | ten interleaved inbound/outbound, group/private events |

Each case holds the first actual `JournalStore.append()` in a worker thread.
Later event tasks reach the plugin callback while that write is still blocked.
Before releasing it, the test confirms the SQLite table is empty. After all
tasks finish, every row's `journal_id` matches callback capture order exactly.
The original Phase 2 implementation passed all five ordinary-concurrency
cases. The additional cancellation case holds A inside its SQLite worker,
cancels A's callback, cancels B while its writer is queued for the lock, then
starts C. It also cancels plugin termination while the writers are pending.
After releasing A, final IDs are A=1, B=2, C=3. The experiment tests actual event-bus delivery and a
real SQLite file; it does not infer the result from lock documentation alone.

`test_raw_inbound_precedes_slow_adapter_and_filtered_event` additionally reads
the inbound journal row from inside the downstream aiocqhttp subscriber before
allowing that subscriber to continue. The subscriber then models an AstrBot
pipeline filter that drops the event. This proves that the inbound row is
committed before normal processing starts, even for an event that is later
filtered.

The ordering test reopens the journal through a fresh `JournalStore`, checks
per-conversation `after(0)` and `after(first_journal_id)`, then appends another
message. Its new ID follows the prior burst and `after(last_journal_id)`
returns that row. Existing Phase 2 storage tests also cover restart and
duplicate message IDs.

## Cursor contract

- `journal_id` increases within one SQLite journal database for successful new
  rows and is assigned in V2 capture-callback order for one client.
- It survives process restart and is suitable for `after(journal_id)` reads.
- It is not a QQ timestamp and is not comparable across independent databases
  or multiple OneBot clients.
- Duplicate platform message IDs return their existing row; a retry does not
  obtain a new cursor.
- If normalization, storage, or shutdown admission fails, that event has no
  cursor. The plugin logs the failure and marks its in-memory error count;
  Phase 2.1 does not provide replay or repair. A future DSH integration must
  not assume a complete history after such a failure.

## Verification and limits

`./scripts/check-phase2.sh` includes the ordering and cancellation cases and
passes the Phase 1, Phase 2, and Phase 2.1 checks. No DSH, MCP, permission,
or business-plugin code was added. The concurrent tests use the pinned
aiocqhttp event bus rather than a live QQ traffic burst. High-volume load,
delayed third-party before hooks, multiple clients, and storage-failure
recovery remain outside this contract.
