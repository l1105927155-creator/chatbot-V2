/** Owner-only, one-turn Bash confinement for the pinned DSH sandbox seam. */
import { AsyncLocalStorage } from 'node:async_hooks'
import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, realpathSync, readdirSync, openSync, fstatSync, closeSync, constants } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { isAbsolute, join, relative, resolve, sep, dirname } from 'node:path'
import { SandboxProvider } from '../../node_modules/@deepseek-ai/dsh-sandbox/lib/index.js'

export const name = 'v2-owner-sandbox'

const fields = ['platform_id', 'bot_id', 'conversation_key', 'requester_id',
  'inbound_message_id', 'epoch', 'session_id', 'nonce', 'prompt_sha256']
const safeMcp = new Set(['mcp__astrbot__history_recent', 'mcp__astrbot__history_before',
  'mcp__astrbot__history_search', 'mcp__astrbot__current_group_info', 'mcp__astrbot__qq_send_origin'])
const allowedTools = new Set(['bash', 'skill', 'web_search', 'web_fetch', ...safeMcp])
const text = value => typeof value === 'string' && value.length > 0
  && value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)
const within = (base, path) => path === base || path.startsWith(base + sep)

function scanWorkspace(root) {
  const meta = lstatSync(root)
  if (!meta.isDirectory() || meta.isSymbolicLink() || (meta.mode & 0o077) !== 0
    || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
    throw new Error('owner workspace must be private and owned by runtime user')
  }
  const pending = [root]
  let count = 0
  while (pending.length) {
    const directory = pending.pop()
    for (const entry of readdirSync(directory)) {
      if (++count > 100000) throw new Error('owner workspace preflight limit exceeded')
      const path = join(directory, entry)
      const item = lstatSync(path)
      if (item.isSocket() || item.isFIFO() || item.isBlockDevice() || item.isCharacterDevice()
        || (item.isFile() && item.nlink > 1)) throw new Error('unsafe owner workspace entry')
      if (item.isDirectory()) pending.push(path)
    }
  }
}

function privateDir(path) {
  if (typeof path !== 'string' || !isAbsolute(path)) throw new Error('absolute private source root required')
  const meta = lstatSync(path)
  if (!meta.isDirectory() || meta.isSymbolicLink() || (meta.mode & 0o077) !== 0
    || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
    throw new Error('owner source root must be private and owned by runtime user')
  }
  return realpathSync(path)
}

export function parseOwners(raw) {
  const owners = JSON.parse(raw)
  if (!Array.isArray(owners) || owners.length === 0) throw new Error('non-empty V2_QQ_OWNERS required')
  for (const item of owners) {
    if (!item || Object.keys(item).sort().join(',') !== 'bot_id,platform_id,user_id'
      || !Object.values(item).every(text)) throw new Error('invalid V2_QQ_OWNERS')
  }
  return owners
}

export class OwnerSandbox extends SandboxProvider {
  static inject = ['tools']

  constructor(ctx, config = {}) {
    super(ctx)
    if (process.platform !== 'linux') throw new Error('owner sandbox requires Linux')
    this.root = privateDir(config.sourceRoot ?? process.env.V2_TURN_SOURCE_DIR)
    this.epoch = config.epoch ?? process.env.V2_TURN_SOURCE_EPOCH
    if (!text(this.epoch)) throw new Error('private source epoch required')
    this.owners = parseOwners(config.owners ?? process.env.V2_QQ_OWNERS)
    const workspace = config.workspaceRoot ?? process.cwd()
    if (!isAbsolute(workspace) || lstatSync(workspace).isSymbolicLink()) {
      throw new Error('absolute physical owner workspace required')
    }
    this.workspace = realpathSync(workspace)
    scanWorkspace(this.workspace)
    if (within(this.workspace, this.root) || within(this.root, this.workspace)) {
      throw new Error('owner workspace and source root must be separate')
    }
    this.runner = config.runner ?? '/usr/bin/bwrap'
    if (this.runner !== '/usr/bin/bwrap') throw new Error('owner sandbox runner must be /usr/bin/bwrap')
    this.limiter = '/usr/bin/python3'
    this.scopeRunner = join(dirname(fileURLToPath(import.meta.url)), 'owner-scope-runner.py')
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
          if (args.workdir !== undefined) {
            if (typeof args.workdir !== 'string' || !args.workdir) throw new Error('invalid workdir')
            const candidate = resolve(this.workspace, args.workdir)
            if (!within(this.workspace, candidate) || !within(this.workspace, realpathSync(candidate))) {
              throw new Error('workdir outside owner workspace')
            }
            if (!lstatSync(candidate).isDirectory()) throw new Error('workdir must be a directory')
          }
        }
        this.captured.set(exec.token, { agent: exec.agent, turn: binding.turn, nonce: binding.source.nonce })
      } catch (error) { return 'V2 owner authorization denied: ' + error.message }
    })
    ctx.on('tools/execute', async (exec, next) => {
      const capture = this.captured.get(exec.token)
      this.captured.delete(exec.token)
      if (!capture) throw new Error('missing owner execution source')
      return this.local.run({ ...capture, execution: exec }, next)
    })
    ctx.on('tools/result', exec => this.captured.delete(exec.token))
  }

  load(sessionId) {
    if (!text(sessionId)) throw new Error('invalid session ID')
    const file = join(this.root, createHash('sha256').update(sessionId).digest('hex') + '.json')
    const descriptor = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW)
    let source
    try {
      const meta = fstatSync(descriptor)
      if (!meta.isFile() || (meta.mode & 0o077) !== 0
        || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
        throw new Error('source file must be private')
      }
      source = JSON.parse(readFileSync(descriptor, 'utf8'))
    } finally {
      closeSync(descriptor)
    }
    if (!source || Object.keys(source).sort().join(',') !== [...fields].sort().join(',')
      || !Object.values(source).every(text)
      || source.epoch !== this.epoch || source.session_id !== sessionId) {
      throw new Error('invalid current source')
    }
    return Object.freeze(source)
  }

  assertOwner(source) {
    if (source.conversation_key !== 'private:' + source.requester_id
      || !this.owners.some(owner => owner.platform_id === source.platform_id
        && owner.bot_id === source.bot_id && owner.user_id === source.requester_id)) {
      throw new Error('owner private chat required')
    }
  }

  bind(agent, turn, message) {
    this.bound.delete(agent)
    if (this.rejected.get(agent) === turn) return
    try {
      const source = this.load(agent.session.id)
      this.assertOwner(source)
      const content = message?.content
      if (!Array.isArray(content) || content.length !== 1 || content[0].type !== 'text'
        || typeof content[0].text !== 'string'
        || createHash('sha256').update(content[0].text).digest('hex') !== source.prompt_sha256) {
        throw new Error('claimed prompt mismatch')
      }
      this.bound.set(agent, {source, turn})
    } catch {
      this.rejected.set(agent, turn)
    }
  }

  binding(agent) {
    const binding = this.bound.get(agent)
    if (!binding || binding.signal?.aborted) throw new Error('no trusted current turn')
    const latest = this.load(agent.session.id)
    if (fields.some(field => latest[field] !== binding.source[field])) throw new Error('source expired')
    this.assertOwner(latest)
    return binding
  }

  async confine(argv, policy, signal) {
    if (signal?.aborted) signal.throwIfAborted()
    const active = this.local.getStore()
    if (!active || active.execution.signal.aborted || active.execution.name !== 'bash') {
      throw new Error('no authorized bash execution')
    }
    const binding = this.binding(active.agent)
    if (binding.turn !== active.turn || binding.source.nonce !== active.nonce) {
      throw new Error('expired owner execution')
    }
    if (policy.mode !== 'workspace-write' || realpathSync(policy.workspaceRoot) !== this.workspace
      || policy.sessionId !== active.agent.session.id) {
      throw new Error('owner workspace-write policy required')
    }
    if (!Array.isArray(argv) || argv.length !== 3 || argv[0] !== 'bash' || argv[1] !== '-c'
      || typeof argv[2] !== 'string') throw new Error('unexpected shell argv')
    scanWorkspace(this.workspace)
    const modelWorkdir = active.execution.arguments?.workdir
    const physicalWorkdir = modelWorkdir === undefined ? this.workspace : realpathSync(resolve(this.workspace, modelWorkdir))
    if (!within(this.workspace, physicalWorkdir)) throw new Error('workdir outside owner workspace')
    const sandboxWorkdir = join('/workspace', relative(this.workspace, physicalWorkdir))
    // A deliberately small, explicit root. No host /home, /etc, /proc, sockets,
    // credentials, or inherited environment are available to the child.
    const wrapped = [
      this.runner, '--unshare-net', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
      '--die-with-parent', '--new-session', '--clearenv',
      '--ro-bind', '/usr', '/usr',
      '--symlink', 'usr/bin', '/bin',
      '--symlink', 'usr/lib', '/lib',
      '--symlink', 'usr/lib64', '/lib64',
      '--dir', '/dev', '--dev-bind', '/dev/null', '/dev/null',
      '--proc', '/proc', '--tmpfs', '/tmp',
      '--bind', this.workspace, '/workspace',
      '--chmod', '0555', '/', '--cap-drop', 'ALL',
      '--chdir', sandboxWorkdir,
      '--setenv', 'PATH', '/usr/bin:/bin',
      '--setenv', 'HOME', '/workspace',
      '--setenv', 'TMPDIR', '/tmp',
      '--', '/usr/bin/bash', '-c', argv[2],
    ]
    if (signal?.aborted) signal.throwIfAborted()
    return {
      // Set limits on DSH's owned scope. A second systemd-run scope would
      // move descendants out of the native cancellation/teardown range.
      argv: [this.limiter, this.scopeRunner, '--workspace', this.workspace, ...wrapped],
      enforcement: 'full',
      denialSignatures: ['permission denied', 'read-only file system', 'no such file or directory'],
      runnerFailureRules: [{fatalSignatures: ['bwrap: ', 'V2 owner containment unavailable']}],
    }
  }
}

export default OwnerSandbox
