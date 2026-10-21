/** Native image lifetime and bounded terminal receipts for the QQ router. */
import { createHash, randomBytes } from 'node:crypto'
import { closeSync, constants, fsyncSync, openSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { LlmError } from '../../node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import { imageOffloadProjection } from '../../node_modules/@deepseek-ai/dsh-compaction-image-offload/lib/types/projection.js'
import { privateSourceRoot, readTurnSource, validateClaimedPrompt } from './trusted-turn-guard.mjs'

export const name = 'v2-turn-lifecycle'
export const inject = ['sessions']

/** Offload only existing surface occurrences, before this turn admits its input. */
export function offloadPreviousImages(session) {
  const targets = []
  for (const seq of session.surface.nodes) {
    const event = session.eventAt(seq)
    if (event?.type !== 'user/message' && event?.type !== 'tool/result') continue
    const message = session.deriveEventMessage(event)
    const imageIndexes = []
    let index = 0
    for (const block of message?.content ?? []) {
      if (block.type !== 'image') continue
      if (block.offloaded !== true) imageIndexes.push(index)
      index++
    }
    if (imageIndexes.length) targets.push({ seq, imageIndexes })
  }
  if (targets.length) session.append('image/offload', { targets })
  return targets
}

function writeReceipt(root, source, value) {
  const filename = createHash('sha256').update(source.session_id).digest('hex') + '.terminal.json'
  const temp = join(root, '.' + randomBytes(16).toString('hex') + '.tmp')
  const fd = openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
  try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd) } finally { closeSync(fd) }
  renameSync(temp, join(root, filename))
}

export function apply(ctx, config = {}) {
  ctx.sessions.registerMessageProjection(imageOffloadProjection)
  const seenTurns = new WeakMap()
  const turns = new Map()
  const rootPath = config.sourceRoot ?? process.env.V2_TURN_SOURCE_DIR
  const root = rootPath ? privateSourceRoot(rootPath) : undefined
  const epoch = config.epoch ?? process.env.V2_TURN_SOURCE_EPOCH

  ctx.on('agent/pre-step', ({ agent, turn }, next) => {
    if (seenTurns.get(agent) !== turn) {
      offloadPreviousImages(agent.session)
      seenTurns.set(agent, turn)
    }
    return next()
  })
  ctx.on('agent/inbox/claimed', ({ agent, message, turn }) => {
    if (!root) return
    try {
      const source = readTurnSource(root, epoch, agent.session.id)
      validateClaimedPrompt(message, source)
      turns.set(agent.session, { source, turn, kind: 'infrastructure', code: 'UNKNOWN',
        calls: new Map(), finished: new Set(), finalText: false })
    } catch {
      // Subagents have no independent QQ source. They cannot create receipts
      // for their parent or certify authorization failures as model failures.
    }
  })
  ctx.on('agent/error', ({ agent, turn, error }) => {
    const active = turns.get(agent.session)
    if (!active || active.turn !== turn) return
    active.kind = error instanceof LlmError ? 'model' : 'infrastructure'
    const code = error instanceof LlmError ? error.failure.code : 'UNKNOWN'
    active.code = typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(code) ? code : 'UNKNOWN'
  })
  ctx.on('session/event', (session, event) => {
    const active = turns.get(session)
    if (!active || event.data?.turn !== active.turn) return
    if (event.type === 'tool/call') {
      // Log the name only. Arguments, tool output and provider messages may
      // contain private conversations or credentials.
      active.calls.set(event.data.callId, event.data.name)
      active.finalText = false
    } else if (event.type === 'assistant/message') {
      active.finalText = event.data.message.content.some(block => block.type === 'text' && block.text.trim())
    } else if (event.type === 'tool/result') {
      // Synthetic recovery closers explicitly say the outcome is unknown.
      // An error result can also represent a remote timeout after execution;
      // only a successful receipt certifies a completed operation here.
      if (!event.data.message.isError) active.finished.add(event.data.message.toolCallId)
    } else if (event.type === 'turn/end') {
      turns.delete(session)
      const reason = event.data.reason.kind
      if (reason === 'completed' && !active.finalText) {
        active.kind = 'model'
        active.code = 'EMPTY_RESPONSE'
      } else if (reason === 'max-tokens') {
        active.kind = 'model'
        active.code = 'MAX_TOKENS'
      } else if (reason !== 'error') return
      const source = readTurnSource(root, epoch, session.id)
      if (source.nonce !== active.source.nonce) return
      const names = ids => [...ids].map(id => active.calls.get(id)).filter(Boolean).slice(0, 100)
      writeReceipt(root, source, { version: 1, session_id: session.id, epoch, nonce: source.nonce,
        turn: active.turn, terminal: true, kind: active.kind, code: active.code,
        tools_started: names(active.calls.keys()), tools_finished: names(active.finished),
        unfinished_tools: names([...active.calls.keys()].filter(id => !active.finished.has(id))) })
    }
  })
  ctx.on('agent/disposed', ({ agent }) => { seenTurns.delete(agent); turns.delete(agent.session) })
}
