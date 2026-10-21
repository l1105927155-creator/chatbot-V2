/** Trusted active-turn facts and policy, attached to the pinned native tool runtime. */
import { Service } from '@deepseek-ai/cordis'
import { AsyncLocalStorage } from 'node:async_hooks'
import { readFileSync, realpathSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname, isAbsolute, resolve, sep } from 'node:path'
import { ScopeStore } from './scopes.mjs'
import { researchDelegation } from '../agent-profile/background-authority.mjs'

const fields = ['platform_id', 'bot_id', 'conversation_key', 'requester_id', 'inbound_message_id', 'epoch', 'session_id', 'nonce', 'prompt_sha256']
const globals = ['read', 'write', 'edit', 'bash', 'conversation_scope_get', 'conversation_scope_update',
  'list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource',
  'web_search', 'web_fetch', 'skill', 'background_research']
const safeMcp = new Set(['mcp__astrbot__history_recent', 'mcp__astrbot__history_before',
  'mcp__astrbot__history_search', 'mcp__astrbot__history_read_image', 'mcp__astrbot__current_group_info', 'mcp__astrbot__qq_send_origin'])
const text = value => typeof value === 'string' && value.length > 0 && value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)
const nested = (base, path) => path === base || path.startsWith(base + sep)

export function parseOwners(raw = '[]') {
  const entries = JSON.parse(raw)
  if (!Array.isArray(entries)) throw new Error('V2_QQ_OWNERS must be an array')
  for (const item of entries) {
    if (!item || Object.keys(item).sort().join(',') !== 'bot_id,platform_id,user_id'
        || !Object.values(item).every(text)) throw new Error('invalid V2_QQ_OWNERS entry')
  }
  return entries.map(item => Object.freeze({...item}))
}

export const identity = source => Object.fromEntries(['platform_id', 'bot_id', 'conversation_key'].map(field => [field, source[field]]))

export class Authority extends Service {
  static inject = ['tools']
  constructor(ctx, config = {}) {
    super(ctx, 'v2Authority')
    this.sourceRoot = resolve(config.sourceRoot ?? process.env.V2_TURN_SOURCE_DIR)
    this.epoch = config.epoch ?? process.env.V2_TURN_SOURCE_EPOCH
    if (!text(this.epoch)) throw new Error('private source epoch is required')
    this.store = new ScopeStore(config.scopeRoot ?? process.env.V2_SCOPE_ROOT)
    this.owners = parseOwners(config.owners ?? process.env.V2_QQ_OWNERS ?? '[]')
    this.bound = new WeakMap()
    this.rejected = new WeakMap()
    this.captured = new Map()
    this.local = new AsyncLocalStorage()
    ctx.on('agent/inbox/claimed', ({agent, turn, message}) => this.bind(agent, turn, message))
    ctx.on('agent/pre-step', async ({agent, turn, signal}, next) => {
      const binding = this.bound.get(agent)
      if (binding?.turn === turn) binding.signal = signal
      return next()
    })
    ctx.on('agent/turn-stopping', ({agent, turn}) => this.unbind(agent, turn))
    ctx.on('agent/disposed', ({agent}) => this.bound.delete(agent))
    ctx.on('agent/created', ({agent}) => {
      // Mask unselected ambient tools; scope-local tools still face the final guard.
      const available = new Set(ctx.tools.schemas().map(tool => tool.name))
      agent.ctx.tools.restrict({allow: globals.filter(name => available.has(name))})
    })
    ctx.tools.guard(exec => {
      try {
        const binding = this.binding(exec.agent)
        if (exec.signal.aborted || binding.signal?.aborted) throw new Error('inactive turn')
        const name = exec.name
        if (binding.delegated && !['web_search', 'web_fetch', 'skill'].includes(name)) {
          throw new Error('tool unavailable to background safe-chat research')
        }
        if (!safeMcp.has(name) && !globals.includes(name)) throw new Error('unavailable tool')
        if (exec.arguments?.sandbox_permissions && exec.arguments.sandbox_permissions !== 'workspace-write') {
          throw new Error('sandbox escalation is unavailable')
        }
        if (name === 'bash' && !this.owner(binding.source)) throw new Error('ordinary shell execution is denied')
        if (name === 'conversation_scope_update' && !this.owner(binding.source)) throw new Error('only owner may edit this conversation scope')
        this.captured.set(exec.token, {agent: exec.agent, source: binding.source, turn: binding.turn})
      } catch (error) { return `V2 authorization denied: ${error.message}` }
    })
    ctx.on('tools/execute', async (exec, next) => {
      const capture = this.captured.get(exec.token)
      this.captured.delete(exec.token)
      if (!capture) throw new Error('missing trusted execution source')
      return this.local.run({...capture, execution: exec}, next)
    })
    ctx.on('tools/result', exec => this.captured.delete(exec.token))
  }

  load(sessionId) {
    const file = createHash('sha256').update(sessionId).digest('hex') + '.json'
    const value = JSON.parse(readFileSync(resolve(this.sourceRoot, file), 'utf8'))
    if (!value || Object.keys(value).sort().join(',') !== [...fields].sort().join(',')
        || !Object.values(value).every(text) || value.epoch !== this.epoch || value.session_id !== sessionId) {
      throw new Error('invalid active source')
    }
    return Object.freeze(value)
  }

  bind(agent, turn, message) {
    this.bound.delete(agent)
    if (this.rejected.get(agent) === turn) return
    try {
      const source = this.load(agent.session.id)
      const content = message?.content
      if (!Array.isArray(content) || content.length !== 1 || content[0].type !== 'text'
          || typeof content[0].text !== 'string'
          || createHash('sha256').update(content[0].text).digest('hex') !== source.prompt_sha256) {
        throw new Error('claimed input does not match the admitted current prompt')
      }
      this.bound.set(agent, {source, turn})
    } catch {
      // An old queued input poisons the entire turn, even if a matching prompt
      // is claimed later in the same batch. Only a new turn may bind again.
      this.rejected.set(agent, turn)
    }
  }

  unbind(agent, turn) {
    if (this.bound.get(agent)?.turn === turn) this.bound.delete(agent)
  }

  binding(agent) {
    const delegated = researchDelegation(agent)
    if (delegated) {
      if (delegated.profile !== 'safe-chat' || delegated.signal?.aborted) {
        throw new Error('background safe-chat authority expired or unavailable')
      }
      return {source:delegated.source, turn:delegated.taskId, signal:delegated.signal, delegated}
    }
    const binding = this.bound.get(agent)
    if (!binding || binding.signal?.aborted) throw new Error('no trusted current turn')
    const latest = this.load(agent.session.id)
    if (latest.nonce !== binding.source.nonce
        || fields.some(field => latest[field] !== binding.source[field])) throw new Error('source changed or expired')
    return binding
  }

  current() {
    const current = this.local.getStore()
    if (!current || current.execution.signal.aborted) throw new Error('no trusted execution')
    const binding = this.binding(current.agent)
    if (binding.delegated && !['web_search', 'web_fetch', 'skill'].includes(current.execution.name)) {
      throw new Error('tool unavailable to background safe-chat research')
    }
    if (binding.turn !== current.turn || binding.source.nonce !== current.source.nonce) throw new Error('expired execution')
    return current.source
  }

  owner(source) {
    return this.owners.some(owner => owner.platform_id === source.platform_id
      && owner.bot_id === source.bot_id && owner.user_id === source.requester_id)
  }

  isOwner(source) {
    if (this.current().nonce !== source.nonce) throw new Error('source mismatch')
    return this.owner(source)
  }

  reserved(path) {
    const candidate = resolve(path)
    for (const base of [dirname(this.sourceRoot), this.store.root]) {
      // Include physical aliases of private roots, not only their configured spelling.
      let physical = base
      try { physical = realpathSync(base) } catch { /* a missing root still reserves its spelling */ }
      if ([base, physical].some(root => nested(root, candidate) || nested(candidate, root))) return true
    }
    return false
  }

  allowFile(source, operation, path) {
    if (this.current().nonce !== source.nonce || !isAbsolute(path)) throw new Error('source or path mismatch')
    if (this.isOwner(source)) return true
    if (this.reserved(path)) return false
    const scope = this.store.get(identity(source)) // No turn/session permission cache.
    return scope.ordinary[operation]?.includes(path) ?? false
  }
}

export const name = 'v2-qq-authority'
export default Authority
