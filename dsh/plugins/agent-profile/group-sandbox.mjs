/** One-turn, group-only execution inside a verified per-group volume and namespace. */
import { AsyncLocalStorage } from 'node:async_hooks'
import { createHash } from 'node:crypto'
import { constants, fstatSync, openSync, closeSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { SandboxProvider } from '../../node_modules/@deepseek-ai/dsh-sandbox/lib/index.js'
import { privateSourceRoot, readTurnSource,
  validateClaimedPrompt, sameTurnSource, verifyTurnImageProofs, validateTurnImages } from './trusted-turn-guard.mjs'
import { researchDelegation } from './background-authority.mjs'
import { canWriteGroupWorkspace } from './group-write-policy.mjs'

export const name = 'v2-group-sandbox'

const safeMcp = new Set(['mcp__astrbot__history_recent', 'mcp__astrbot__history_before',
  'mcp__astrbot__history_search', 'mcp__astrbot__history_read_image', 'mcp__astrbot__current_group_info', 'mcp__astrbot__current_group_members', 'mcp__astrbot__qq_prepare_reply', 'mcp__astrbot__qq_send_origin',
  'mcp__astrbot__qq_file_receive', 'mcp__astrbot__qq_file_send', 'mcp__astrbot__meme_categories', 'mcp__astrbot__meme_list', 'mcp__astrbot__meme_send', 'mcp__astrbot__plugin_status', 'mcp__astrbot__bili_dynamics', 'mcp__astrbot__bili_subscribe', 'mcp__astrbot__bili_subscriptions', 'mcp__astrbot__bili_unsubscribe', 'mcp__astrbot__twitter_subscribe', 'mcp__astrbot__twitter_subscriptions', 'mcp__astrbot__twitter_unsubscribe'])
const allowedTools = new Set(['bash', 'skill', 'web_search', 'web_fetch', 'background_research', ...safeMcp])
const text = value => typeof value === 'string' && value.length > 0
  && value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)
const within = (base, path) => path === base || path.startsWith(base + sep)

export class SerialGate {
  constructor() { this.tail = Promise.resolve() }
  async run(signal, callback) {
    let release
    const previous = this.tail
    this.tail = new Promise(resolveGate => { release = resolveGate })
    try {
      // Even an aborted waiter must preserve queue order until its predecessor
      // releases; otherwise later calls could leapfrog the still-running one.
      await previous
      if (signal?.aborted) signal.throwIfAborted()
      return await callback()
    } finally { release() }
  }
}

export class GroupSandbox extends SandboxProvider {
  static inject = ['tools']

  constructor(ctx, config = {}) {
    super(ctx)
    if (process.platform !== 'linux') throw new Error('group sandbox requires Linux')
    this.root = privateSourceRoot(config.sourceRoot ?? process.env.V2_TURN_SOURCE_DIR)
    this.epoch = config.epoch ?? process.env.V2_TURN_SOURCE_EPOCH
    if (!text(this.epoch)) throw new Error('private source epoch required')
    this.platformId = config.platformId ?? process.env.V2_GROUP_PLATFORM_ID
    this.botId = config.botId ?? process.env.V2_GROUP_BOT_ID
    this.groupId = config.groupId ?? process.env.V2_GROUP_ID
    if (![this.platformId, this.botId, this.groupId].every(text)) throw new Error('group identity required')
    const workspace = config.workspaceRoot ?? process.env.V2_GROUP_WORKSPACE_ROOT
    if (!isAbsolute(workspace)) throw new Error('absolute group workspace required')
    const descriptor = openSync(workspace, constants.O_PATH | constants.O_DIRECTORY | constants.O_NOFOLLOW)
    try {
      const meta = fstatSync(descriptor)
      if (meta.uid !== process.getuid() || (meta.mode & 0o077) !== 0) {
        throw new Error('group workspace must be private and owned')
      }
    } finally { closeSync(descriptor) }
    this.workspace = workspace
    this.runner = '/usr/bin/bwrap'
    this.scopeRunner = join(dirname(fileURLToPath(import.meta.url)), 'group-scope-runner.py')
    this.bound = new WeakMap()
    this.captured = new Map()
    this.local = new AsyncLocalStorage()
    this.gate = new SerialGate()
    ctx.on('agent/inbox/claimed', ({agent, turn, message}) => this.bind(agent, turn, message))
    ctx.on('agent/pre-step', async ({agent, turn, signal}, next) => {
      const binding = this.bound.get(agent)
      if (binding?.turn === turn) {
        binding.signal = signal
        if (binding.source.image_manifest !== undefined && !binding.imagesVerified) {
          if (signal?.aborted) signal.throwIfAborted()
          this.binding(agent, false)
          await validateTurnImages(ctx, this.root, binding.source, binding.message)
          if (signal?.aborted || binding.signal?.aborted) throw new Error('inactive turn during image validation')
          this.binding(agent, false)
          if (this.bound.get(agent) !== binding || binding.turn !== turn) throw new Error('turn changed during image validation')
          binding.imagesVerified = true
        }
      }
      return next()
    })
    ctx.on('agent/turn-stopping', ({agent, turn}) => {
      if (this.bound.get(agent)?.turn === turn) this.bound.delete(agent)
    })
    ctx.on('agent/disposed', ({agent}) => this.bound.delete(agent))
    ctx.tools.guard(exec => {
      try {
        const binding = this.binding(exec.agent)
        if (exec.signal.aborted || binding.signal?.aborted) throw new Error('inactive turn')
        if (!allowedTools.has(exec.name)) throw new Error('unavailable tool')
        if (exec.name === 'bash') {
          const args = exec.arguments ?? {}
          if (args.sandbox_permissions && args.sandbox_permissions !== 'workspace-write') {
            throw new Error('sandbox escalation denied')
          }
          if (args.run_in_background) throw new Error('background execution denied')
          if (args.timeoutMs !== undefined && (!Number.isFinite(args.timeoutMs)
              || args.timeoutMs <= 0 || args.timeoutMs > 60000)) {
            throw new Error('group command timeout must be between 1 and 60000 milliseconds')
          }
          if (args.workdir !== undefined) {
            if (typeof args.workdir !== 'string' || !args.workdir) throw new Error('invalid workdir')
            const candidate = resolve(this.workspace, args.workdir)
            if (!within(this.workspace, candidate) || !within(this.workspace, resolve(candidate))) {
              throw new Error('workdir outside group workspace')
            }
          }
        }
        this.captured.set(exec.token, {agent: exec.agent, turn: binding.turn,
          nonce: binding.source.nonce, source: binding.source})
      } catch (error) { return 'V2 group authorization denied: ' + error.message }
    })
    ctx.on('tools/execute', async (exec, next) => {
      const capture = this.captured.get(exec.token)
      this.captured.delete(exec.token)
      if (!capture) throw new Error('missing group execution source')
      const invoke = () => this.local.run({...capture, execution: exec}, next)
      return exec.name === 'bash' ? this.gate.run(exec.signal, invoke) : invoke()
    })
    ctx.on('tools/result', exec => this.captured.delete(exec.token))
  }

  bind(agent, turn, message) {
    this.bound.delete(agent)
    try {
      const source = readTurnSource(this.root, this.epoch, agent.session.id)
      validateClaimedPrompt(message, source)
      if (source.platform_id !== this.platformId || source.bot_id !== this.botId
          || source.conversation_key !== 'group:' + this.groupId) {
        throw new Error('group source identity mismatch')
      }
      this.bound.set(agent, {source, turn, message, imagesVerified: source.image_manifest === undefined})
    } catch { /* absence or mismatch means every tool is denied */ }
  }

  binding(agent, requireVerifiedImages = true) {
    const delegated = researchDelegation(agent)
    if (delegated) {
      const source = delegated.source
      if (delegated.profile !== 'group-sandbox' || delegated.signal?.aborted
          || source.platform_id !== this.platformId || source.bot_id !== this.botId
          || source.conversation_key !== 'group:' + this.groupId) {
        throw new Error('background group authority expired or mismatched')
      }
      return {source, turn: delegated.taskId, signal: delegated.signal,
        imagesVerified: true, delegated}
    }
    const binding = this.bound.get(agent)
    if (!binding || binding.signal?.aborted) throw new Error('no trusted current turn')
    const latest = readTurnSource(this.root, this.epoch, agent.session.id)
    if (!sameTurnSource(latest, binding.source)) {
      throw new Error('source expired')
    }
    verifyTurnImageProofs(this.root, latest)
    if (requireVerifiedImages && !binding.imagesVerified) throw new Error('trusted image sequence has not been verified')
    if (latest.platform_id !== this.platformId || latest.bot_id !== this.botId
        || latest.conversation_key !== 'group:' + this.groupId) throw new Error('group source identity mismatch')
    return binding
  }

  async confine(argv, policy, signal) {
    if (signal?.aborted) signal.throwIfAborted()
    const active = this.local.getStore()
    if (!active || active.execution.signal.aborted || active.execution.name !== 'bash') {
      throw new Error('no authorized group bash execution')
    }
    const binding = this.binding(active.agent)
    if (binding.turn !== active.turn || binding.source.nonce !== active.nonce) {
      throw new Error('expired group execution')
    }
    if (policy.mode !== 'workspace-write' || resolve(policy.workspaceRoot) !== this.workspace
        || policy.sessionId !== active.agent.session.id) {
      throw new Error('group workspace-write policy required')
    }
    if (!Array.isArray(argv) || argv.length !== 3 || argv[0] !== 'bash' || argv[1] !== '-c'
        || typeof argv[2] !== 'string') throw new Error('unexpected shell argv')
    const workdirArg = active.execution.arguments?.workdir
    const physical = workdirArg === undefined ? this.workspace : resolve(this.workspace, workdirArg)
    if (!within(this.workspace, physical)) throw new Error('workdir outside group workspace')
    const workdir = relative(this.workspace, physical)
    if (signal?.aborted) signal.throwIfAborted()
    // Re-read the trusted console file at the final host-side execution seam.
    // The result controls the actual mount; command text is never inspected.
    const mode = canWriteGroupWorkspace(binding.source) ? 'workspace-write' : 'read-only'
    return {argv: ['/usr/bin/python3', '-I', '-S', this.scopeRunner, '--workspace', this.workspace,
        '--workdir', workdir || '.', '--workspace-mode', mode, '--', argv[2]],
      enforcement: 'full',
      denialSignatures: ['permission denied', 'read-only file system', 'no such file or directory'],
      runnerFailureRules: [{fatalSignatures: ['bwrap: ', 'V2 group containment unavailable']}]}
  }
}

export default GroupSandbox
