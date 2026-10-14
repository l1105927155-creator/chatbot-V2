import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import { ToolCallId } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import SystemPrompt from '../../dsh/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import ToolRuntime from '../../dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import { createScope } from '../../dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js'
import Authority from '../../dsh/plugins/qq-permission/authority.mjs'
import * as ScopeTools from '../../dsh/plugins/qq-permission/configuration.mjs'

const root = mkdtempSync(path.join(os.tmpdir(), 'v2-phase5-authority-'))
const workspace = mkdtempSync(path.join(os.tmpdir(), 'v2-phase5-workspace-'))
const sourceRoot = path.join(root, 'v2-turn-sources', 'runtime-epoch-test')
const scopeRoot = path.join(root, 'scope')
mkdirSync(sourceRoot, { recursive: true })
chmodSync(path.dirname(sourceRoot), 0o700)
chmodSync(sourceRoot, 0o700)

const epoch = 'runtime-epoch-test'
const ownerId = '1105927155'
const sourceFields = ['platform_id', 'bot_id', 'conversation_key', 'requester_id', 'inbound_message_id', 'epoch', 'session_id', 'nonce', 'prompt_sha256']
const journalDelta = 'trusted journal delta'
let callId = 0
const sideEffects = []
let localResourceCalls = 0

function privateSource(sessionId, facts) {
  return path.join(sourceRoot, `${createHash('sha256').update(sessionId).digest('hex')}.json`)
}

function makeSource(agent, requesterId, nonce) {
  return {
    platform_id: 'qq',
    bot_id: '2869323076',
    conversation_key: `group:${agent.conversation}`,
    requester_id: requesterId,
    inbound_message_id: `in-${agent.session.id}-${nonce}`,
    epoch,
    session_id: agent.session.id,
    nonce,
    prompt_sha256: createHash('sha256').update(journalDelta).digest('hex'),
  }
}

function inboxMessage(source, text = journalDelta) {
  return { id: source.inbound_message_id, content: [{ type: 'text', text }] }
}

function publish(agent, requesterId, nonce) {
  const source = makeSource(agent, requesterId, nonce)
  assert.deepEqual(Object.keys(source).sort(), [...sourceFields].sort())
  writeFileSync(privateSource(agent.session.id), JSON.stringify(source), { mode: 0o600 })
  return source
}

function defineTool(name, execute, parameters = { type: 'object', properties: {}, additionalProperties: false }) {
  return {
    name,
    description: `test native ${name}`,
    parameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) { return execute(args, exec) },
  }
}

async function dispatch(ctx, agent, name, args = {}) {
  return ctx.tools.execute({
    signal: new AbortController().signal,
    callId: ToolCallId(`phase5-${++callId}`),
    name,
    arguments: args,
    agent,
  })
}

function outputText(result) {
  return result.content.map(block => block.type === 'text' ? block.text : '').join('\n')
}

async function main() {
  try {
    const ctx = new Context()
    await ctx.plugin(SystemPrompt, {})
    await ctx.plugin(ToolRuntime)

    // Configuration path canonicalization uses the host FS contract; all tool
    // registration, guard, event delivery and execution remain pinned native DSH.
    ctx.provide('fs', {
      async resolve(value, { cwd }) { return path.resolve(cwd, value) },
      processPath(value) { return path.resolve(value) },
      async stat() { return { type: 'file' } },
    })
    await ctx.plugin(Authority, {
      sourceRoot,
      scopeRoot,
      epoch,
      owners: JSON.stringify([{ platform_id: 'qq', bot_id: '2869323076', user_id: ownerId }]),
    })
    await ctx.plugin(ScopeTools)
    const authority = ctx.v2Authority

    const emptySchema = { type: 'object', properties: {}, additionalProperties: false }
    ctx.tools.register(defineTool('read', async (args) => {
      const source = authority.current()
      if (!authority.allowFile(source, 'read', path.resolve(args.path))) throw new Error('read denied')
      sideEffects.push(`read:${args.path}`)
      return 'read ok'
    }, { type: 'object', properties: { path: { type: 'string' } }, required: ['path'], additionalProperties: false }))
    ctx.tools.register(defineTool('write', async () => { sideEffects.push('write'); return 'write ok' }))
    ctx.tools.register(defineTool('edit', async () => { sideEffects.push('edit'); return 'edit ok' }))
    ctx.tools.register(defineTool('bash', async () => { sideEffects.push('bash'); return 'bash ok' }))

    async function makeAgent(sessionId, conversation) {
      const agent = {
        id: sessionId,
        session: { id: sessionId, header: { cwd: workspace } },
        conversation,
      }
      let scope
      await ctx.plugin(Object.assign(inner => { scope = createScope(inner, agent) }, { inject: ['tools', 'systemPrompt'] }))
      agent.ctx = scope.ctx
      return agent
    }

    const agent = await makeAgent('session-main', '1055300305')
    agent.ctx.tools.register(defineTool('unknown-local', async () => { sideEffects.push('local'); return 'ran' }))
    agent.ctx.tools.register(defineTool('list_mcp_resources', async () => {
      localResourceCalls += 1
      return 'resource list ok'
    }))
    await ctx.serial('agent/created', { agent, source: 'startup' })
    assert.deepEqual(ctx.tools.schemas(agent).map(tool => tool.name).sort(), [
      'bash', 'conversation_scope_get', 'conversation_scope_update', 'edit', 'list_mcp_resources', 'read', 'unknown-local', 'write',
    ])
    assert.ok(agent.ctx.tools.schemas(agent).some(tool => tool.name === 'unknown-local'))

    const oneFile = path.join(workspace, 'persona.md')
    writeFileSync(oneFile, 'persona')
    if (authority.reserved(oneFile)) console.error({ sourceRoot: authority.sourceRoot, storeRoot: authority.store.root, oneFile })
    assert.equal(authority.reserved(oneFile), false)
    const owner = publish(agent, ownerId, 'turn-owner-1')
    ctx.emit('agent/inbox/claimed', { agent, message: inboxMessage(owner), turn: 1 })
    await ctx.waterfall('agent/pre-step', {
      agent, messages: [], turn: 1, step: 1, signal: new AbortController().signal,
    }, async () => ({ kind: 'enter', messages: [] }))

    const initial = await dispatch(ctx, agent, 'conversation_scope_get')
    assert.match(outputText(initial), /"revision":0/)
    assert.match(outputText(await dispatch(ctx, agent, 'list_mcp_resources')), /resource list ok/)
    assert.equal(localResourceCalls, 1, 'native safe MCP resource tools remain available when registered in agent scope')
    const schemas = ctx.tools.schemas(agent)
    const getSchema = schemas.find(tool => tool.name === 'conversation_scope_get')
    assert.equal(Object.hasOwn(getSchema.parameters.properties, 'conversation'), false, 'model cannot select a target conversation')

    const grant = await dispatch(ctx, agent, 'conversation_scope_update', {
      expected_revision: 0,
      ordinary: { read: [oneFile], write: [], delete: [] },
    })
    assert.match(outputText(grant), /"revision":1/)

    ctx.emit('agent/turn-stopping', { agent, turn: 1, signal: new AbortController().signal })

    // The same native ToolRuntime executes another operation in the same turn;
    // it re-reads the new scope revision instead of reusing a prior decision.
    const ordinarySameAgent = publish(agent, '2200000000', 'turn-ordinary-same-session')
    ctx.emit('agent/inbox/claimed', { agent, message: inboxMessage(ordinarySameAgent), turn: 2 })
    const allowedRead = await dispatch(ctx, agent, 'read', { path: oneFile })
    assert.match(outputText(allowedRead), /read ok/)
    assert.ok(sideEffects.includes(`read:${oneFile}`))

    // Ordinary members can inspect the shared scope but cannot update it.
    const ordinaryScope = await dispatch(ctx, agent, 'conversation_scope_get')
    assert.match(outputText(ordinaryScope), /"revision":1/)
    const ordinaryUpdate = await dispatch(ctx, agent, 'conversation_scope_update', {
      expected_revision: 1,
      ordinary: { read: [], write: [], delete: [] },
    })
    assert.match(outputText(ordinaryUpdate), /only owner may edit/)

    // Simulate an authorized owner update committed between calls in this
    // ordinary turn. Every operation reads the latest persisted revision.
    authority.store.update({ platform_id: 'qq', bot_id: '2869323076', conversation_key: 'group:1055300305' }, 1,
      { read: [], write: [], delete: [] })
    const beforeDeniedRead = sideEffects.length
    const deniedRead = await dispatch(ctx, agent, 'read', { path: oneFile })
    assert.match(outputText(deniedRead), /authorization denied|read denied/)
    assert.equal(sideEffects.length, beforeDeniedRead)

    authority.store.update({ platform_id: 'qq', bot_id: '2869323076', conversation_key: 'group:1055300305' }, 2,
      { read: [oneFile], write: [], delete: [] })
    const allowedAfterRefresh = await dispatch(ctx, agent, 'read', { path: oneFile })
    assert.match(outputText(allowedAfterRefresh), /read ok/)
    authority.store.update({ platform_id: 'qq', bot_id: '2869323076', conversation_key: 'group:1055300305' }, 3,
      { read: [], write: [], delete: [] })
    const refreshedDenied = await dispatch(ctx, agent, 'read', { path: oneFile })
    assert.match(outputText(refreshedDenied), /authorization denied|read denied/)

    const beforeDenied = sideEffects.length
    const deniedBash = await dispatch(ctx, agent, 'bash')
    assert.match(outputText(deniedBash), /ordinary shell execution is denied/)
    const unknownLocal = await dispatch(ctx, agent, 'unknown-local')
    assert.match(outputText(unknownLocal), /unavailable tool/)
    const targetInjection = await dispatch(ctx, agent, 'conversation_scope_get', { conversation: 'group:other' })
    assert.match(outputText(targetInjection), /"revision":4/, 'extra target input cannot change the host-bound scope')
    assert.equal(sideEffects.length, beforeDenied, 'denied global and local tool calls have no body side effect')

    // Turn stopping unbinds the actual scoped agent. A later tool dispatch
    // cannot reuse its previous session source.
    ctx.emit('agent/turn-stopping', { agent, turn: 2, signal: new AbortController().signal })
    const afterStop = await dispatch(ctx, agent, 'conversation_scope_get')
    assert.match(outputText(afterStop), /authorization denied/)

    // Missing, stale/expired and aborted source bindings all fail closed.
    const missing = await makeAgent('session-missing', '1055300305')
    await ctx.serial('agent/created', { agent: missing, source: 'startup' })
    ctx.emit('agent/inbox/claimed', { agent: missing, message: inboxMessage({ inbound_message_id: 'none' }), turn: 1 })
    assert.match(outputText(await dispatch(ctx, missing, 'conversation_scope_get')), /authorization denied/)

    const expired = await makeAgent('session-expired', '1055300305')
    await ctx.serial('agent/created', { agent: expired, source: 'startup' })
    publish(expired, ownerId, 'expires-before-operation')
    ctx.emit('agent/inbox/claimed', { agent: expired, message: inboxMessage({ inbound_message_id: 'expired' }), turn: 1 })
    publish(expired, ownerId, 'rotated-after-claim')
    assert.match(outputText(await dispatch(ctx, expired, 'conversation_scope_get')), /authorization denied/)

    const aborted = await makeAgent('session-aborted', '1055300305')
    await ctx.serial('agent/created', { agent: aborted, source: 'startup' })
    publish(aborted, ownerId, 'aborted-turn')
    ctx.emit('agent/inbox/claimed', { agent: aborted, message: inboxMessage({ inbound_message_id: 'aborted' }), turn: 1 })
    const controller = new AbortController()
    controller.abort()
    await ctx.waterfall('agent/pre-step', {
      agent: aborted, messages: [], turn: 1, step: 1, signal: controller.signal,
    }, async () => ({ kind: 'enter', messages: [] }))
    assert.match(outputText(await dispatch(ctx, aborted, 'conversation_scope_get')), /authorization denied/)

    // A concurrent session has independent source and scope identity.
    const other = await makeAgent('session-other', 'private:1105927155')
    await ctx.serial('agent/created', { agent: other, source: 'startup' })
    const otherOwner = publish(other, ownerId, 'other-session-owner')
    ctx.emit('agent/inbox/claimed', { agent: other, message: inboxMessage(otherOwner), turn: 1 })
    const mainConcurrent = await makeAgent('session-main-concurrent', '1055300305')
    await ctx.serial('agent/created', { agent: mainConcurrent, source: 'startup' })
    const mainRequester = publish(mainConcurrent, '2200000000', 'main-concurrent')
    ctx.emit('agent/inbox/claimed', { agent: mainConcurrent, message: inboxMessage(mainRequester), turn: 1 })
    const [otherInitial, mainLatest] = await Promise.all([
      dispatch(ctx, other, 'conversation_scope_get'),
      dispatch(ctx, mainConcurrent, 'conversation_scope_get'),
    ])
    assert.match(outputText(otherInitial), /"revision":0/)
    assert.match(outputText(mainLatest), /"revision":4/)

    const inboxBound = await makeAgent('session-inbox-bound', '1055300305')
    await ctx.serial('agent/created', { agent: inboxBound, source: 'startup' })
    const oldInbox = publish(inboxBound, ownerId, 'inbox-old')
    ctx.emit('agent/inbox/claimed', { agent: inboxBound, message: inboxMessage(oldInbox, 'stale queued prompt'), turn: 1 })
    const replacementInbox = publish(inboxBound, ownerId, 'inbox-replacement')
    ctx.emit('agent/inbox/claimed', { agent: inboxBound, message: inboxMessage(replacementInbox), turn: 1 })
    assert.match(outputText(await dispatch(ctx, inboxBound, 'conversation_scope_get')), /authorization denied/,
      'a second matching claim cannot replace the binding during the same turn')
    ctx.emit('agent/turn-stopping', { agent: inboxBound, turn: 1, signal: new AbortController().signal })
    const nextInbox = publish(inboxBound, ownerId, 'inbox-next-turn')
    ctx.emit('agent/inbox/claimed', { agent: inboxBound, message: inboxMessage(nextInbox), turn: 2 })
    assert.match(outputText(await dispatch(ctx, inboxBound, 'conversation_scope_get')), /"revision":4/,
      'a matching claim on the next turn can establish a fresh source')

    // Neither the runtime source file nor the scope store can be granted via
    // the owner configuration tool.
    const storeFile = path.join(scopeRoot, `${createHash('sha256').update(JSON.stringify(['qq', '2869323076', 'group:1055300305'])).digest('hex')}.json`)
    const reservedPaths = [privateSource(agent.session.id), storeFile]
    for (const forbidden of reservedPaths) {
      const result = await dispatch(ctx, other, 'conversation_scope_update', {
        expected_revision: 0,
        ordinary: { read: [forbidden], write: [], delete: [] },
      })
      assert.match(outputText(result), /private source\/configuration cannot be granted/)
      assert.equal(authority.store.get({ platform_id: 'qq', bot_id: '2869323076', conversation_key: other.conversation }).revision, 0)
    }

    // Verify the source persisted only host facts and that source file mode is private.
    const sourceDocument = JSON.parse(readFileSync(privateSource(other.session.id), 'utf8'))
    assert.deepEqual(Object.keys(sourceDocument).sort(), [...sourceFields].sort())
    assert.equal(statSync(sourceRoot).mode & 0o777, 0o700)
    assert.equal(statSync(privateSource(other.session.id)).mode & 0o777, 0o600)
    assert.equal(authority.store.root, scopeRoot)
    console.log('native Cordis authority/configuration ToolRuntime contract passed')
  } finally {
    rmSync(root, { recursive: true, force: true })
    rmSync(workspace, { recursive: true, force: true })
  }
}

await main()
