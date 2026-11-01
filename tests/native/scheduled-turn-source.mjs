import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtempSync, chmodSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { readTurnSource, scheduledToolAllowed, sameTurnSource } from '../../dsh/plugins/agent-profile/trusted-turn-guard.mjs'

const root = mkdtempSync(join(tmpdir(), 'scheduled-turn-source-'))
chmodSync(root, 0o700)
const epoch = 'epoch-test'
const base = {
  platform_id: 'qq', bot_id: '1', conversation_key: 'group:3', requester_id: '42',
  inbound_message_id: 'source-1', epoch, session_id: 'legacy-session', nonce: 'nonce',
  prompt_sha256: createHash('sha256').update('prompt').digest('hex'),
}
function publish(sessionId, fields) {
  const payload = {...base, session_id: sessionId, ...fields}
  writeFileSync(join(root, createHash('sha256').update(sessionId).digest('hex') + '.json'),
    JSON.stringify(payload), {mode: 0o600})
  return payload
}
try {
  publish('legacy-session', {})
  const legacy = readTurnSource(root, epoch, 'legacy-session')
  assert.equal(legacy.kind, undefined)
  const scheduledRaw = publish('scheduled-session', {
    kind: 'scheduled', task_id: 'diary', run_id: 'run-1',
  })
  const scheduled = readTurnSource(root, epoch, 'scheduled-session')
  assert.equal(scheduled.kind, 'scheduled')
  assert.equal(sameTurnSource(scheduled, {...scheduled, run_id: 'run-2'}), false)
  assert.equal(scheduledToolAllowed('bash'), true)
  assert.equal(scheduledToolAllowed('skill'), true)
  assert.equal(scheduledToolAllowed('mcp__astrbot__history_recent'), true)
  assert.equal(scheduledToolAllowed('mcp__astrbot__task_list'), true)
  assert.equal(scheduledToolAllowed('mcp__astrbot__qq_send_origin'), true)
  assert.equal(scheduledToolAllowed('mcp__astrbot__qq_file_send'), true)
  for (const name of ['background_research', 'group_app',
    'mcp__astrbot__bili_subscribe', 'mcp__astrbot__twitter_subscribe',
    'mcp__astrbot__task_manage']) {
    assert.equal(scheduledToolAllowed(name), false, name)
  }
  const forged = publish('forged-session', {kind: 'scheduled', task_id: 'diary'})
  assert.throws(() => readTurnSource(root, epoch, 'forged-session'), /invalid current turn source/)
  assert.equal(scheduledRaw.run_id, 'run-1')
  assert.equal(forged.run_id, undefined)
} finally {
  rmSync(root, {recursive: true, force: true})
}
