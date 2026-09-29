# Phase 1 findings: AstrBot PlatformMessageHistory

## Evidence scope

- **Pinned source:** AstrBot `v4.28.2`, commit `3c7adafa1397e182d60b1016bf88759265113c8a`.
- **Isolated runtime:** `tests/contracts/test_platform_message_history_paths.py` loads that checkout, invokes the actual AstrBot methods, and uses its real `SQLiteDatabase` and `PlatformMessageHistoryManager`. Its QQ/OneBot sender is a fake object.
- **Live QQ:** on 2026-09-29, V2 AstrBot used its isolated runtime and loopback WebSocket port `6200` to connect to the first NapCat instance for Bot `<test_bot_qq>`. The existing AstrBot connection on `6199` remained connected. Sender `<test_sender_qq>` sent controlled messages in test group `<test_group_qq>` and private chat. NapCat's read-only history API and V2's own SQLite/logs supplied the observations below.
- **Test cleanup:** the extra NapCat WebSocket client was removed and the V2 test process stopped after observation. The original `6199` connection remained established. QQ account and group numbers are redacted in this tracked document; they are not needed to reproduce the behavior with another test instance.
- **Source-only:** an assertion derived by reading the pinned source, without a complete live AstrBot pipeline or QQ test.
- **Not verified:** a statement deliberately left without sufficient evidence.

Run the harness after bootstrap with:

```bash
ASTRBOT_SOURCE=.runtime/astrbot ASTRBOT_ROOT=.runtime/astrbot \
  uv run --with pytest --with pytest-asyncio \
  --with-requirements .runtime/astrbot/requirements.txt \
  pytest -q tests/contracts/test_platform_message_history_paths.py
```

The harness was run against the bootstrap checkout at `.runtime/astrbot` (commit
`3c7adafa1397e182d60b1016bf88759265113c8a`):

```text
11 passed, 2 warnings
```

The pinned acceptance command `./scripts/check-phase1.sh` passed all 23
bootstrap, gate, probe, and history tests (`2` upstream deprecation warnings).

## Seven-path matrix

| Path | Evidence and result | `group_message_history_enable` | Stored fields / loss |
| --- | --- | --- | --- |
| Group inbound user message | **Live QQ + isolated runtime:** the test group's ordinary message and `/v2probe` reached V2 and wrote rows `3` and `4`. NapCat assigned inbound message IDs `1993644472` and `2145187698`. The empty-mention exception below can stop propagation before this handler. | Required; disabled skips the write in the isolated test. | `content.type="user"`; sender `<test_sender_qq>`; conversation `user_id="v2-phase1-napcat:GroupMessage:<test_group_qq>"`. The platform IDs are absent from these rows. |
| Group default LLM response | **Isolated runtime:** `Main.persist_llm_response` writes one final response row when enabled. This only verifies the built-in response hook, not a real provider/QQ round trip. | Required; disabled skips the write. | `content.type="bot"`; sender is `event.get_self_id()` (or `"bot"`) and name is fixed `"bot"`; same group UMO conversation. |
| Group plugin/command response | **Live QQ + isolated runtime:** `/v2probe` ran in V2's plugin pipeline. V2 logged the fixed response; NapCat recorded the Bot reply with message ID `49288932`. V2 history has the command row but no reply row. Source inspection locates no history insert on this `event.send` path. | No setting in the tested path made this reply persist. | No row, so role/sender/conversation and the real message ID are lost from this table. |
| Group proactive `Context.send_message` | **Live QQ + isolated runtime:** `/v2push` invoked `Context.send_message` to its origin. NapCat recorded the fixed Bot output with message ID `1460586456`; V2 wrote history row `7`. | Required; isolated test shows disabled still sends but skips persistence. | `content.type="bot"`; sender/name `"bot"`; conversation `"v2-phase1-napcat:GroupMessage:<test_group_qq>"`; no platform message ID. |
| Private inbound user message | **Live QQ + isolated runtime:** the private ordinary message and commands reached V2's event log and appeared in NapCat history, but V2 history had zero private rows. The upstream inbound hook has a group-only guard. | Irrelevant: the setting only gates group history. | No row. Private user role/sender/conversation are unavailable through this table. |
| Private plugin/command response | **Live QQ + isolated runtime:** V2's `/v2probe` reply appeared in NapCat history with message ID `396614374`, while V2 history had no private row. | Irrelevant. | No row. |
| Private proactive send | **Live QQ + isolated runtime:** `/v2push` invoked `Context.send_message` to private origin. NapCat recorded the fixed output with message ID `2066688480`; V2 history still had zero private rows. | Irrelevant. | No row. |

## Live AI gate and command check

The isolated V2 runtime had `provider_settings.enable=false`,
`platform_settings.empty_mention_waiting=false`, and both group context AI
settings disabled. Its `v2_ai_gate` and `v2_phase1_probe` plugins loaded before
the NapCat connection. After the first controlled ordinary message, V2's event
log recorded two ordinary test messages and two `/v2probe` commands across the
test group and private chat. It recorded exactly two `RespondStage` sends in
that interval, both the fixed probe reply. NapCat history independently shows
both probe replies. This is live evidence that the Phase 1 configuration left
deterministic commands active without a V2 free-chat reply for the tested
ordinary messages. The isolated pipeline test also forces
`provider_settings.enable=true` and verifies that the plugin gate prevents a
default Agent call.

## Data model and content fidelity

`PlatformMessageHistory` has an internal integer `id`, `platform_id`, `user_id`, `sender_id`, `sender_name`, `content`, and `llm_checkpoint_id`; it has **no QQ/OneBot platform message ID column**. This is both source evidence (`astrbot/core/db/po.py`) and covered by the isolated LLM row test.

The table's conversation key is named `user_id`; for the covered group paths it receives the unified message origin rather than a dedicated QQ group ID. The stored role is nested as `content.type`, rather than a top-level direction/source field.

**Isolated runtime:** the real `insert_message_chain` serializer retains plain text, converts image/record/video/file components to labels such as `[Image]`, preserves only selected `At` and `Reply` fields, and maps unknown components to a type label. It discards media file paths, binary content, and other component-specific fields. It stores neither QQ message IDs nor a provenance value distinguishing default LLM, plugin, DSH, or system output.

## Special group-inbound gap: empty mentions

`handle_empty_mention` is registered with priority `maxsize - 1`; the inbound
history hook has priority `maxsize - 2`. `StarRequestSubStage` stops iterating
lower-priority handlers once `event.is_stopped()` is true. The builtin handler
yields a `ProviderRequest` and then unconditionally calls `event.stop_event()`
in its `finally` block. Therefore, with the default
`platform_settings.empty_mention_waiting=true`, a group message consisting only
of an @-mention or configured wake prefix does **not** reach
`persist_group_message`.

**Isolated runtime:** the harness invokes the actual builtin
`handle_empty_mention` through the real `StarRequestSubStage`, while replacing
only its 60-second waiter with an immediate test waiter and disabling its reply
request. It then confirms the real persistence method is skipped and the SQLite
history table remains empty. It also invokes the actual builtin handler with
`empty_mention_waiting=false` for a pure @-mention, confirms it does not stop
the event, then confirms the group row is written. A separate actual-handler
test confirms that a prefix-only message such as `/`, when `/` appears in
`wake_prefix`, **still stops** even with that setting false: the
`is_wake_prefix_only` branch does not consult `empty_mention_waiting`. Thus the
setting repairs the @-only case but leaves the prefix-only inbound gap. This is
a Phase 1 observed coverage gap, not a Phase 2 implementation decision.

**Source-only interaction with the V2 AI gate:** AstrBot runs `on_llm_request`
hooks when it consumes that `ProviderRequest`. A gate that calls `stop_event()`
can also leave later adapter handlers skipped. It does not introduce the
empty-mention gap: the builtin handler itself subsequently stops the event even
without the gate.

## Configuration limits relevant to the AI gate

**Source-only:** `GroupChatContext._format_message` can call
`provider.text_chat(..., persist=False)` to caption an image. That call is not
the normal `ProviderRequest` path and does not pass through `on_llm_request`.
`Main.on_message` enters this group-context path when either
`provider_ltm_settings.group_icl_enable` or
`provider_ltm_settings.active_reply.enable` is true. For the Phase 1 baseline,
keep both settings false. This prevents the group-context path from enabling
that direct provider call; it is a configuration constraint, not a journal
design decision.

## Relevant pinned source locations

- Schema: `astrbot/core/db/po.py` (`PlatformMessageHistory`).
- Serializer: `astrbot/core/platform_message_history_mgr.py` (`insert_message_chain`).
- Group inbound and LLM response hooks: `astrbot/builtin_stars/astrbot/main.py` (`persist_group_message`, `persist_llm_response`).
- Proactive send hook: `astrbot/core/star/context.py` (`Context.send_message`).
- QQ send paths: `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py` and `aiocqhttp_platform_adapter.py`.

## Limits and Phase 2 input

The seven-path investigation now includes live QQ evidence for six paths;
the group default LLM response is isolated-only because V2 deliberately
disables that route. Transport failures, retries, echoed self messages, media
fidelity on real QQ, and a provider-enabled live LLM response remain unverified.
The tested NapCat WebSocket client had `reportSelfMessage=false`. This document
does not make the final canonical-journal decision.

The AstrBot source tag is verified, but its dependency ranges are not locked in
this repository. Exact package-version reproduction of the isolated runtime
remains unverified.

**Phase 2 journal input:** normal group and private plugin replies do not enter
`PlatformMessageHistory`. A unified read from that table therefore cannot
recover the complete command exchange, even though the replies reached QQ.
Phase 1 requires this gap to be reproduced and documented; its remedy belongs
to Phase 2.
