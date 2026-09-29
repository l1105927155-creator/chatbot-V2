# Phase 2 journal verification

## Implementation

The V2-owned `v2_journal` plugin records normal OneBot `message` events and
confirmed Bot `message_sent` events at aiocqhttp client before hooks. The
inbound hook runs ahead of AstrBot message conversion, allowlist/rate filters,
and wake checks. It writes one SQLite row per observed platform
message in AstrBot's `data/plugin_data/v2_journal/journal.sqlite3`. No AstrBot
core file was changed.

The stable conversation key is `group:<group_id>` or `private:<peer_qq>`.
`platform_id` and Bot ID are separate fields so identical group/private IDs
across Bot accounts do not collide. Outbound private feedback uses
`target_id`, not the Bot's `user_id`, to identify the peer. The uniqueness key
is `(platform_id, bot_id, conversation_key, message_id)` when `message_id` is
available. Each distinct platform message ID becomes its own row, including a file segment
sent separately from text. Outbound provenance is `unknown`: OneBot feedback
does not identify the initiating plugin, and the journal does not infer it from
message text.

Content keeps text, mentions, reply IDs, and available media/file references
such as file ID, name, size, and URL. Inline base64/data payloads are excluded.
The read API supports `recent`, `after`, `before`, and lookup by platform
message ID. Returned windows are in ascending `journal_id` order.

## Live QQ sequence

On 2026-09-29, the test sender sent the Phase 2 sequence to Bot
`<test_bot_qq>` in group `<test_group_qq>` and private chat. NapCat's read-only
history API showed the same messages and platform IDs. The V2 journal stored:

| Conversation | QQ-visible message | Direction | Platform message ID | Journal ID |
| --- | --- | --- | --- | --- |
| Group | `V2J-A` | inbound | `733323561` | `4` |
| Group | `/v2probe` | inbound | `130858547` | `6` |
| Group | `v2 phase 1 probe: ok` | outbound | `806349924` | `7` |
| Group | `V2J-B` | inbound | `905108821` | `8` |
| Group | `/v2push` | inbound | `1147210337` | `9` |
| Group | `v2 phase 1 proactive: ok` | outbound | `697576265` | `10` |
| Private | `V2J-C` | inbound | `2068268100` | `11` |
| Private | `/v2probe` | inbound | `691541605` | `12` |
| Private | `v2 phase 1 probe: ok` | outbound | `1923013821` | `13` |
| Private | `/v2push` | inbound | `233880848` | `14` |
| Private | `v2 phase 1 proactive: ok` | outbound | `2076383900` | `15` |

The journal order matches the QQ-visible order within both conversations.
There were six group rows and five private rows. The gaps in `journal_id` are
expected because other-group test rows were removed from this ignored runtime
database. The initial V2 test configuration had an empty AstrBot allowlist and
briefly captured eight out-of-scope group rows. Those rows were deleted, and
the V2 test runtime allowlist was restricted to the specified group/private
origin before the restart check. The raw capture hook observes all QQ message
events delivered to the V2 OneBot client, including events AstrBot later
filters. Scope must therefore be enforced at the NapCat client if required;
the journal does not use a hard-coded test-group filter.

After stopping and restarting V2, all 11 controlled rows remained available.
The `recent`, `after`, `before`, and message-ID lookup methods returned the
expected rows and retained cursor positions. The test client reconnected on
`6200` with no observed replay of the prior sequence. Storage and plugin-hook
tests inject duplicate `message_sent` events and confirm that they do not
create extra rows; a real reconnect duplicate was not observed.

`./scripts/check-phase2.sh` passed: 23 Phase 1 checks and 7 Phase 2 checks.
The Phase 2 checks use the pinned aiocqhttp event bus and a real
SQLite database file. They do not impersonate a QQ delivery; the live sequence
above supplies that separate evidence.

An independent review found that the first implementation's AstrBot plugin
hook could miss messages filtered before ProcessStage and change wake state.
The final implementation uses aiocqhttp before hooks for both event types.
A regression test delays AstrBot's private message handler and drops its
message while checking that the journal has already stored the inbound row
before the following outbound feedback. The QQ sequence above was recorded
before this hook correction. After the correction, a second live QQ check
recorded group `/v2probe` as inbound message `28466408` / journal `26`, then
one outbound reply `2066729166` / journal `27`; the private check recorded
inbound `598251500` / journal `23`, then one outbound reply `977873636` /
journal `24`. This confirms the final hook in both conversations without a
recursive Bot reply.

## Limits

The live journal sequence covers text commands and proactive text sends.
The capture experiment separately verified a small file split into two QQ
message IDs and the file's OneBot metadata; the normalizer/store tests cover
its representation. Failed sends, real reconnect duplicates, image/voice
payloads, and high-volume concurrent ordering remain unverified. The current
AI gate remains a Phase 1 safety mechanism. DSH, MCP, per-session cursors,
and business plugins were not added in Phase 2.

If normalization or SQLite insertion fails, the plugin increments its
in-memory `capture_errors` count and logs the platform/message ID. A storage
failure log explicitly marks the history incomplete; the event bus continues
so the journal does not block QQ handling. The error count resets on restart,
and Phase 2 does not provide automatic replay or repair after a storage fault.
