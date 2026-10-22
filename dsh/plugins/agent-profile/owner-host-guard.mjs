// Per-operation gate for the Owner's native host tools. DSH's global
// danger-full-access policy is only loaded by the Owner Profile; this guard
// still requires a fresh, exact private-Owner source for every tool call.
import {
  parseConfiguredOwners, privateSourceRoot,
  readTurnSource, validateClaimedPrompt, validateOwnerSource, sameTurnSource,
  verifyTurnImageProofs, validateTurnImages,
} from './trusted-turn-guard.mjs'
import { researchDelegation } from './background-authority.mjs'
import { withOwnerWorkspaceLock } from './host-workspace-lock.mjs'

export const name = 'v2-owner-host-guard'

const allowedTools = new Set([
  'bash', 'read', 'read_image', 'write', 'edit', 'glob', 'grep',
  'skill', 'web_search', 'web_fetch',
  'mcp__astrbot__history_recent', 'mcp__astrbot__history_before',
  'mcp__astrbot__history_search', 'mcp__astrbot__history_read_image', 'mcp__astrbot__current_group_info',
  'mcp__astrbot__current_group_members', 'mcp__astrbot__qq_prepare_reply', 'mcp__astrbot__qq_send_origin', 'mcp__astrbot__qq_file_receive',
  'mcp__astrbot__qq_file_send', 'mcp__astrbot__meme_categories', 'mcp__astrbot__meme_list', 'mcp__astrbot__meme_send', 'mcp__astrbot__plugin_status', 'mcp__astrbot__bili_dynamics', 'mcp__astrbot__bili_subscribe', 'mcp__astrbot__bili_subscriptions', 'mcp__astrbot__bili_unsubscribe', 'mcp__astrbot__twitter_subscribe', 'mcp__astrbot__twitter_subscriptions', 'mcp__astrbot__twitter_unsubscribe',
  'background_research',
])

export class OwnerHostGuard {
  static inject = ['tools']

  constructor(ctx, config = {}) {
    this.root = privateSourceRoot(config.sourceRoot ?? process.env.V2_TURN_SOURCE_DIR)
    this.epoch = config.epoch ?? process.env.V2_TURN_SOURCE_EPOCH
    if (typeof this.epoch !== 'string' || !this.epoch) throw new Error('private source epoch required')
    this.owners = parseConfiguredOwners(config.owners ?? process.env.V2_QQ_OWNERS)
    this.bound = new WeakMap()
    this.rejected = new WeakMap()
    this.captured = new Map()
    ctx.on('agent/inbox/claimed', ({ agent, turn, message }) => this.bind(agent, turn, message))
    ctx.on('agent/pre-step', async ({ agent, turn, signal }, next) => {
      const binding = this.bound.get(agent)
      if (binding?.turn === turn) {
        binding.signal = signal
        if (binding.source.image_manifest !== undefined && !binding.imagesVerified) {
          if (signal?.aborted) signal.throwIfAborted()
          this.current(agent, false)
          await validateTurnImages(ctx, this.root, binding.source, binding.message)
          if (signal?.aborted || binding.signal?.aborted) throw new Error('inactive turn during image validation')
          this.current(agent, false)
          if (this.bound.get(agent) !== binding || binding.turn !== turn) throw new Error('turn changed during image validation')
          binding.imagesVerified = true
        }
      }
      return next()
    })
    ctx.on('agent/turn-stopping', ({ agent, turn }) => {
      if (this.bound.get(agent)?.turn === turn) this.bound.delete(agent)
    })
    ctx.on('agent/disposed', ({ agent }) => this.bound.delete(agent))
    ctx.tools.guard(exec => {
      try {
        const binding = this.current(exec.agent)
        if (exec.signal?.aborted || binding.signal?.aborted) throw new Error('inactive turn')
        if (!allowedTools.has(exec.name)) throw new Error('tool is unavailable in Owner host profile')
        if (exec.name === 'bash') {
          const args = exec.arguments ?? {}
          if (args.run_in_background) throw new Error('background execution is disabled')
          if (args.sandbox_permissions !== undefined) {
            throw new Error('Owner host commands do not accept sandbox escalation arguments')
          }
          if (args.timeoutMs !== undefined
            && (!Number.isFinite(args.timeoutMs) || args.timeoutMs <= 0 || args.timeoutMs > 600_000)) {
            throw new Error('Owner command timeout must be between 1 and 600000 milliseconds')
          }
        }
        this.captured.set(exec.token, {
          agent: exec.agent, turn: binding.turn, nonce: binding.source.nonce, name: exec.name,
        })
      } catch (error) {
        this.captured.delete(exec.token)
        return 'V2 Owner host authorization denied: ' + error.message
      }
    })
    ctx.on('tools/execute', async (exec, next) => {
      const capture = this.captured.get(exec.token)
      this.captured.delete(exec.token)
      if (!capture || capture.agent !== exec.agent || capture.name !== exec.name) {
        throw new Error('V2 Owner host authorization denied: missing tool guard capture')
      }
      const binding = this.current(exec.agent)
      if (binding.turn !== capture.turn || binding.source.nonce !== capture.nonce
        || exec.signal?.aborted || binding.signal?.aborted) {
        throw new Error('V2 Owner host authorization denied: current turn expired before execution')
      }
      return withOwnerWorkspaceLock(exec, next)
    })
    ctx.on('tools/result', exec => this.captured.delete(exec.token))
  }

  bind(agent, turn, message) {
    this.bound.delete(agent)
    if (this.rejected.get(agent) === turn) return
    try {
      const source = readTurnSource(this.root, this.epoch, agent.session.id)
      validateOwnerSource(source, this.owners)
      validateClaimedPrompt(message, source)
      this.bound.set(agent, { source, turn, message, imagesVerified: source.image_manifest === undefined })
    } catch {
      this.rejected.set(agent, turn)
    }
  }

  current(agent, requireVerifiedImages = true) {
    const delegated = researchDelegation(agent)
    if (delegated) {
      if (delegated.profile !== 'owner-host' || delegated.signal?.aborted) throw new Error('background host authority expired')
      return { source: delegated.source, turn: delegated.taskId, signal: delegated.signal,
        imagesVerified: true, delegated }
    }
    const binding = this.bound.get(agent)
    if (!binding || binding.signal?.aborted) throw new Error('no trusted current turn')
    const latest = readTurnSource(this.root, this.epoch, agent.session.id)
    if (!sameTurnSource(latest, binding.source)) {
      throw new Error('current turn source changed')
    }
    verifyTurnImageProofs(this.root, latest)
    if (requireVerifiedImages && !binding.imagesVerified) throw new Error('trusted image sequence has not been verified')
    validateOwnerSource(latest, this.owners)
    return binding
  }
}

export default OwnerHostGuard
