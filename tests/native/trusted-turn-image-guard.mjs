import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import { ToolCallId } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import ToolRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SystemPrompt from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import { OwnerHostGuard } from '../../dsh/plugins/agent-profile/owner-host-guard.mjs'
import { GroupSandbox } from '../../dsh/plugins/agent-profile/group-sandbox.mjs'

const root = mkdtempSync(join(tmpdir(), 'v2-trusted-image-'))
chmodSync(root, 0o700)
const hash = data => createHash('sha256').update(data).digest('hex')
const sourceFilename = sid => `${hash(sid)}.json`
async function fixture(identity, imageBodies, options = {}) {
  const sourceRoot = join(root, `sources-${Math.random().toString(16).slice(2)}`)
  mkdirSync(sourceRoot, { mode: 0o700 })
  const sid = `session-${Math.random().toString(16).slice(2)}`
  const source = {
    platform_id: 'qq', bot_id: 'bot', conversation_key: identity.conversation_key,
    requester_id: identity.requester_id ?? 'owner', inbound_message_id: 'msg-1',
    epoch: 'epoch-1', session_id: sid, nonce: 'nonce-1',
    prompt_sha256: hash('inspect these images'),
  }
  const manifestItems = imageBodies.map((data, index) => {
    const filename = `image-${(String(index + 1).repeat(48)).slice(0, 48)}.bin`
    writeFileSync(join(sourceRoot, filename), data, { mode: 0o600 })
    return { filename, sha256: hash(data), mime_type: index ? 'image/jpeg' : 'image/png' }
  })
  source.image_manifest = JSON.stringify({ version: 1, images: manifestItems })
  const sourcePath = join(sourceRoot, sourceFilename(sid))
  const writeSource = value => writeFileSync(sourcePath, JSON.stringify(value), { mode: 0o600 })
  writeSource(source)
  const attachmentService = {
    async saveImages(inputs) {
      if (options.onSave) await options.onSave({ source, sourcePath, inputs, writeSource })
      return inputs.map(input => ({
        attachmentId: hash(input.data), mediaType: input.mediaType,
        bytes: input.data.byteLength, width: 1, height: 1,
      }))
    },
  }
  const ctx = new Context()
  await ctx.plugin(SystemPrompt, {})
  await ctx.plugin(ToolRuntime)
  ctx.provide('attachments', attachmentService)
  const owner = identity.conversation_key === 'private:owner'
  const workspace = join(root, `workspace-${Math.random().toString(16).slice(2)}`)
  mkdirSync(workspace, { mode: 0o700 })
  if (owner) {
    new OwnerHostGuard(ctx, { sourceRoot, epoch: 'epoch-1',
      owners: JSON.stringify([{ platform_id: 'qq', bot_id: 'bot', user_id: 'owner' }]) })
  } else {
    new GroupSandbox(ctx, { sourceRoot, epoch: 'epoch-1', workspaceRoot: workspace,
      platformId: 'qq', botId: 'bot', groupId: '42' })
  }
  let executions = 0
  ctx.tools.register({ name: 'bash', description: 'test bash',
    parameters: { type: 'object', properties: {} },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    async execute() { executions++; return 'ran' },
  })
  const refs = imageBodies.map((data, index) => ({
    attachmentId: hash(data), mediaType: index ? 'image/jpeg' : 'image/png',
    bytes: data.byteLength, width: 1, height: 1,
  }))
  if (options.reverseClaimed) refs.reverse()
  const agent = { session: { id: sid } }
  const message = { content: [
    { type: 'text', text: 'inspect these images' },
    ...refs.map(attachment => ({ type: 'image', attachment })),
  ] }
  const dispatch = () => ctx.tools.execute({ signal: new AbortController().signal,
    callId: ToolCallId('image-guard-' + Math.random().toString(16).slice(2)),
    name: 'bash', arguments: { command: 'true' }, agent })
  return { agent, ctx, message, source, sourceRoot, sourcePath, writeSource, workspace,
    dispatch, executions: () => executions }
}

try {
  const bodies = [Buffer.from('first source image'), Buffer.from('second source image')]
  for (const identity of [
    { conversation_key: 'private:owner', requester_id: 'owner' },
    { conversation_key: 'group:42' },
  ]) {
    const f = await fixture(identity, bodies)
    f.ctx.emit('agent/inbox/claimed', { agent: f.agent, turn: 1, message: f.message })
    assert.match((await f.dispatch()).content[0].text, /not been verified/,
    'tools stay unavailable until trusted images have been normalized and matched')
    await f.ctx.waterfall(f.ctx, 'agent/pre-step', { agent: f.agent, turn: 1,
      signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
    assert.match((await f.dispatch()).content[0].text, /ran/)
    assert.equal(f.executions(), 1)
    const manifest = JSON.parse(f.source.image_manifest)
    writeFileSync(join(f.sourceRoot, manifest.images[0].filename), 'replaced source image', { mode: 0o600 })
    assert.match((await f.dispatch()).content[0].text, /digest mismatch/,
    'raw proof mutation revokes subsequent tool access')
    rmSync(f.sourceRoot, { recursive: true, force: true })
    rmSync(f.workspace, { recursive: true, force: true })
  }

  const reversed = await fixture({ conversation_key: 'private:owner', requester_id: 'owner' }, bodies,
    { reverseClaimed: true })
  reversed.ctx.emit('agent/inbox/claimed', { agent: reversed.agent, turn: 1, message: reversed.message })
  await assert.rejects(reversed.ctx.waterfall(reversed.ctx, 'agent/pre-step', { agent: reversed.agent, turn: 1,
    signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] })), /references do not match/,
  'claimed image order must match normalized trusted proofs')
  assert.match((await reversed.dispatch()).content[0].text, /not been verified/)

  let race
  race = await fixture({ conversation_key: 'private:owner', requester_id: 'owner' }, bodies, {
    onSave: async ({ source, writeSource }) => writeSource({ ...source, nonce: 'revoked-during-await' }),
  })
  race.ctx.emit('agent/inbox/claimed', { agent: race.agent, turn: 1, message: race.message })
  await assert.rejects(race.ctx.waterfall(race.ctx, 'agent/pre-step', { agent: race.agent, turn: 1,
    signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] })), /source changed/,
  'nonce is rechecked after the asynchronous image normalization')
  assert.match((await race.dispatch()).content[0].text, /source changed/)
  for (const f of [reversed, race]) {
    rmSync(f.sourceRoot, { recursive: true, force: true })
    rmSync(f.workspace, { recursive: true, force: true })
  }
  process.stdout.write('trusted image binding and owner/group tool guards passed\n')
} finally {
  rmSync(root, { recursive: true, force: true })
}
