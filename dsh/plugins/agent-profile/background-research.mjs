import { isBackupRoute } from './model-recovery.mjs'
/** Host-controlled foreground tool for delegated background work. */
import { createUserMessage, LlmError } from '../../node_modules/@deepseek-ai/dsh-llm/lib/index.js'
import * as McpClient from '../../node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js'
import { privateSourceRoot, readTurnSource, validateClaimedPrompt,
  validateOwnerSource, parseConfiguredOwners, sameTurnSource } from './trusted-turn-guard.mjs'
import { bindParentTurn, endParentTurn, currentParentTurn, bindResearchChild,
  createResearchAuthority, researchDelegation, revokeResearchChild, revokeResearchAuthority } from './background-authority.mjs'

export const name = 'v2-background-research'
export const inject = ['tools', 'jobs', 'subagents']

export const MAX_PER_PARENT = 3
export const MAX_RUNTIME = 3
const MAX_PROMPT_CHARS = 12000
const MAX_RESULT_CHARS = 12000
const ACTIVE = new Map()
const TASKS = new Map()
const BY_PARENT = new Map()
const PENDING_CHILD = new Map()
const PARENT_START_LOCKS = new Map()

// Read-only bridge for the host's private control socket. Keep the original
// callback payload so reconciliation cannot change a task's terminal result.
export function terminalEvents(parentSessionId) {
  return [...TASKS.values()]
    .filter(task => task.rootSessionId === parentSessionId && task.terminalEvent)
    .map(task => ({...task.terminalEvent}))
}

const isText = value => typeof value === 'string' && value.length > 0
  && value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)
const output = { schema: { type: 'object', additionalProperties: false,
  properties: { task_id: {type:'string'}, status: {type:'string'}, label: {type:'string'}, detail: {type:'string'} },
  required: ['status'], }, render: (_args, value) => [{type:'text', text:JSON.stringify(value)}] }
const textOf = blocks => blocks.filter(block => block?.type === 'text').map(block => block.text).join('\n').trim()

function callbackConfig(config) {
  const rawUrl = config?.callbackUrl ?? process.env.V2_BACKGROUND_CALLBACK_URL
  const token = config?.callbackToken ?? process.env.V2_BACKGROUND_CALLBACK_TOKEN
  if (!isText(rawUrl) || !isText(token)) return undefined
  const url = new URL(rawUrl)
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) {
    throw new Error('background callback must use HTTPS or loopback HTTP')
  }
  return { url: url.href, token }
}

async function postCallback(callback, payload) {
  const response = await fetch(callback.url, {
    method: 'POST',
    headers: { authorization: `Bearer ${callback.token}`, 'content-type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(5000),
  })
  if (!response.ok) throw new Error(`background callback rejected (${response.status})`)
  return response.json().catch(() => ({}))
}

function requester(source, expectedProfile) {
  if (expectedProfile === 'owner-host' || expectedProfile === 'owner-sandbox') {
    return validateOwnerSource(source, parseConfiguredOwners(process.env.V2_QQ_OWNERS))
  }
  if (expectedProfile === 'group-sandbox') {
    if (source.conversation_key !== 'group:' + process.env.V2_GROUP_ID
      || source.platform_id !== process.env.V2_GROUP_PLATFORM_ID
      || source.bot_id !== process.env.V2_GROUP_BOT_ID) throw new Error('group source identity mismatch')
    return true
  }
  if (expectedProfile === 'safe-chat') {
    if (!source.conversation_key.startsWith('private:') || source.conversation_key !== 'private:' + source.requester_id) {
      throw new Error('private source required')
    }
    return true
  }
  throw new Error('unsupported background execution profile')
}


function mapForParent(parentId) {
  let tasks = BY_PARENT.get(parentId)
  if (!tasks) BY_PARENT.set(parentId, tasks = new Set())
  return tasks
}

async function withParentStartLock(parentId, operation) {
  const previous = PARENT_START_LOCKS.get(parentId) ?? Promise.resolve()
  let release
  const hold = new Promise(resolve => { release = resolve })
  const tail = previous.catch(() => {}).then(() => hold)
  PARENT_START_LOCKS.set(parentId, tail)
  await previous.catch(() => {})
  try {
    return await operation()
  } finally {
    release()
    if (PARENT_START_LOCKS.get(parentId) === tail) PARENT_START_LOCKS.delete(parentId)
  }
}

function safeText(value, max = MAX_RESULT_CHARS) {
  const text = String(value ?? '').trim()
  const marker = '\n[已截断]'
  return text.length > max ? text.slice(0, Math.max(0, max - marker.length)) + marker : text
}

function taskLabel(prompt) {
  const firstLine = String(prompt ?? '').split(/\r?\n/).map(line => line.trim()).find(Boolean)
  return safeText(firstLine || '后台研究', 60)
}

function terminalStatus(status) {
  return status === 'completed' || status === 'failed' || status === 'killed'
}

const RECOVERABLE_MODEL_CODES = new Set([
  'MODEL_TIMEOUT', 'MODEL_TRANSPORT_ERROR', 'RATE_LIMITED',
  'MODEL_UNAVAILABLE', 'EMPTY_RESPONSE', 'MODEL_ERROR',
])
const SAFE_MODEL_ERROR_CODES = new Set([
  ...RECOVERABLE_MODEL_CODES, 'QUOTA_EXCEEDED', 'MISSING_CREDENTIAL',
  'INVALID_CREDENTIAL', 'INVALID_REQUEST', 'CONTEXT_WINDOW_EXCEEDED',
  'MODEL_OUTPUT_LIMIT', 'MAX_TOKENS', 'UNKNOWN',
])

function modelRecovery(ctx) {
  const service = ctx.reflect?.get?.('v2ModelRecovery')
  return service && typeof service.peek === 'function'
    && typeof service.classifyFailure === 'function' ? service : undefined
}

function safeRecoverySummary(value) {
  if (!value || value.attempted !== true || typeof value.recovered !== 'boolean'
      || typeof value.requested_provider !== 'string' || value.requested_provider.length < 1
      || value.requested_provider.length > 128 || typeof value.requested_model !== 'string'
      || value.requested_model.length < 1 || value.requested_model.length > 256
      || !isBackupRoute(value)
      || !RECOVERABLE_MODEL_CODES.has(value.code)) return undefined
  if (value.requested_provider === value.provider && value.requested_model === value.model) return undefined
  const status = Number.isInteger(value.status) && value.status >= 100 && value.status <= 599
    ? value.status : undefined
  return {requested_provider:value.requested_provider, requested_model:value.requested_model,
    provider:value.provider, model:value.model, code:value.code, attempted:true,
    recovered:value.recovered, ...(status === undefined ? {} : {status})}
}

function terminalModelDetails(ctx, task) {
  const result = {}
  if (SAFE_MODEL_ERROR_CODES.has(task.modelError)) result.model_error = task.modelError
  const recovery = modelRecovery(ctx)
  if (recovery && task.child && Number.isInteger(task.turn)) {
    try {
      const summary = safeRecoverySummary(recovery.peek(task.child, task.turn))
      if (summary) result.recovery = summary
    } catch { /* Recovery diagnostics never change task completion. */ }
  }
  return result
}

export function apply(ctx, config = {}) {
  const callback = callbackConfig(config)
  // Keep plain ACP and existing profile tests usable without a host callback.
  if (!callback) {
    ctx.logger?.info?.('background research unavailable: callback endpoint is not configured')
    return
  }
  const sourceRoot = privateSourceRoot(config.sourceRoot ?? process.env.V2_TURN_SOURCE_DIR)
  const epoch = config.epoch ?? process.env.V2_TURN_SOURCE_EPOCH
  const profile = config.executionProfile
  const recovery = modelRecovery(ctx)
  if (!isText(epoch) || !['owner-host', 'owner-sandbox', 'group-sandbox', 'safe-chat'].includes(profile)) {
    throw new Error('background research requires a supported execution profile and source epoch')
  }

  const parentTurns = new WeakMap()
  const detachController = ctx.jobs.attachController('v2-background-research')
  function rememberTerminal(taskId, task) {
    if (!task || task.active || task.jobRemoved) return
    const owner = task.parent.session.id
    if (!task.terminal) {
      try {
        const job = ctx.jobs.get(taskId, owner)
        if (!terminalStatus(job.status)) return
        task.terminal = { status: job.status, label: job.label, detail: job.detail }
      } catch {
        // A disposed owner may already have dropped its native jobs.
        task.jobRemoved = true
        return
      }
    }
    try {
      ctx.jobs.remove(taskId, owner)
      task.jobRemoved = true
    } catch (error) {
      ctx.logger?.warn?.('background terminal job cleanup failed: ' + String(error))
    }
  }
  function pruneTaskHistory() {
    for (const [taskId, task] of TASKS) rememberTerminal(taskId, task)
    if (TASKS.size > 100) {
      for (const [taskId, task] of TASKS) {
        if (!task.active && task.terminal && task.jobRemoved) TASKS.delete(taskId)
        if (TASKS.size <= 80) break
      }
    }
  }
  ctx.on('agent/inbox/claimed', ({agent, turn, message}) => {
    try {
      const source = readTurnSource(sourceRoot, epoch, agent.session.id)
      validateClaimedPrompt(message, source)
      requester(source, profile)
      const binding = { turn, source, signal: undefined }
      parentTurns.set(agent, binding)
      bindParentTurn(agent, turn, source)
    } catch {
      parentTurns.delete(agent)
      endParentTurn(agent, turn)
    }
  })
  ctx.on('agent/pre-step', ({agent, turn, signal}, next) => {
    const binding = parentTurns.get(agent)
    if (binding?.turn === turn) binding.signal = signal
    return next()
  })
  ctx.on('agent/turn-stopping', ({agent, turn}) => {
    const binding = parentTurns.get(agent)
    if (binding?.turn === turn) {
      binding.signal = undefined
      // The source is captured into any delegated task; the new parent turn may bind independently.
      endParentTurn(agent, turn)
    }
  })
  ctx.on('agent/pre-step', ({agent, turn}, next) => {
    const delegated = researchDelegation(agent)
    const task = delegated && TASKS.get(delegated.taskId)
    if (task?.active && task.child === agent) task.turn = turn
    return next()
  })
  ctx.on('agent/error', ({agent, turn, error}) => {
    const delegated = researchDelegation(agent)
    const task = delegated && TASKS.get(delegated.taskId)
    if (!task?.active || task.child !== agent || task.turn !== turn || !(error instanceof LlmError)) return
    try {
      const failure = recovery?.classifyFailure(error.failure)
      if (SAFE_MODEL_ERROR_CODES.has(failure?.code)) task.modelError = failure.code
      else task.modelError = 'MODEL_ERROR'
    } catch { task.modelError = 'MODEL_ERROR' }
  })
  ctx.on('agent/created', async ({agent}) => {
    const parentSessionId = agent.session.header.parentSession
    if (typeof parentSessionId !== 'string') return
    const pending = PENDING_CHILD.get(parentSessionId)
    if (!pending || pending.active !== true) return
    try {
      pending.child = agent
      const linked = bindResearchChild(agent, pending.authority)
      pending.linked = linked
      if (pending.mcp) {
        await agent.ctx.plugin(McpClient, {
          transport: 'streamable-http', serverName: 'astrbot',
          url: pending.mcp.url, headers: {Authorization: `Bearer ${pending.mcp.token}`},
          failOnStartupError: true,
        })
      }
      pending.progress('研究子 Agent 已启动')
    } catch (error) {
      pending.startError = error
      throw error
    }
  })
  ctx.on('agent/disposed', ({agent}) => {
    const delegated = researchDelegation(agent)
    if (delegated) revokeResearchChild(agent, delegated.taskId)
    const owned = [...(BY_PARENT.get(agent.session.id) ?? [])]
    for (const taskId of owned) {
      const task = TASKS.get(taskId)
      if (!task) continue
      task.active = false
      task.controller.abort(new Error('parent agent disposed'))
      revokeResearchAuthority(task.authority)
      if (task.child) revokeResearchChild(task.child, taskId)
      try { ctx.jobs.kill(taskId, agent.session.id, 'parent agent disposed') } catch {}
    }
    BY_PARENT.delete(agent.session.id)
    for (const [taskId, task] of TASKS) if (task.parent === agent) TASKS.delete(taskId)
    parentTurns.delete(agent)
  })
  ctx.on('tools/execute', (exec, next) => {
    const delegated = researchDelegation(exec.agent)
    if (delegated) {
      const task = TASKS.get(delegated.taskId)
      if (!task?.active || task.child !== exec.agent || delegated.parent !== task.parent
        || delegated.source !== task.source || delegated.signal !== task.controller.signal) {
        throw new Error('background research tool authority expired or unavailable')
      }
      task.progress(`正在使用 ${exec.name}`)
    }
    return next()
  })
  ctx.on('tools/result', exec => {
    const delegated = researchDelegation(exec.agent)
    if (!delegated) return
    const task = TASKS.get(delegated.taskId)
    if (task?.active && task.child === exec.agent) task.progress(`已完成 ${exec.name}，继续研究`)
  })

  const parameters = { type:'object', additionalProperties:false, required:['action'], properties:{
    action: {type:'string', enum:['start','status','update','cancel']},
    prompt: {type:'string', minLength:1, maxLength:MAX_PROMPT_CHARS},
    task_id: {type:'string', minLength:1, maxLength:128},
  } }
  ctx.tools.register({
    name: 'background_research',
    description: '启动后台研究、自检或请求者已授权的维护任务，也可查询进度、补充要求或取消。start 必须写明自足的任务范围与授权边界。子 Agent 继承原请求者工具和权限，按任务要求使用工具；返回后前台可继续交流。',
    parameters,
    output,
    execute: async (args, exec) => {
      const parent = exec.agent
      const delegation = researchDelegation(parent)
      let binding
      let parentTaskId
      let depth = 1
      let rootSessionId = parent.session.id
      if (delegation) {
        const parentTask = TASKS.get(delegation.taskId)
        if (!parentTask?.active || parentTask.child !== parent || parentTask.source !== delegation.source
            || delegation.signal?.aborted || delegation.profile !== profile) {
          throw new Error('background parent authority expired')
        }
        binding = {turn: delegation.taskId, source: delegation.source, signal: delegation.signal}
        parentTaskId = delegation.taskId
        depth = delegation.depth + 1
        rootSessionId = delegation.rootSessionId
        if (depth > MAX_RUNTIME) throw new Error('background research capacity is full')
      } else {
        binding = parentTurns.get(parent)
        const trusted = currentParentTurn(parent)
        if (!binding || binding.turn !== trusted.turn || binding.source !== trusted.source
          || binding.signal?.aborted) throw new Error('no trusted current request')
        const currentSource = readTurnSource(sourceRoot, epoch, parent.session.id)
        if (!sameTurnSource(currentSource, binding.source)) throw new Error('current request source changed')
      }
      requester(binding.source, profile)
      const owner = parent.session.id
      const lineage = parentTaskId === undefined ? {} : {parent_task_id: parentTaskId, depth}

      if (args.action === 'start') {
        if (typeof args.prompt !== 'string' || !args.prompt.trim() || args.prompt.length > MAX_PROMPT_CHARS) {
          throw new Error('start requires a research prompt of 1 to 12000 characters')
        }
        pruneTaskHistory()
        if (mapForParent(owner).size >= MAX_PER_PARENT) throw new Error('this conversation already has 3 running research tasks')
        if (ACTIVE.size >= MAX_RUNTIME) throw new Error('background research capacity is full')
        const controller = new AbortController()
        const startGate = {}
        startGate.promise = new Promise(resolve => { startGate.resolve = resolve })
        const record = {
          parent, source: binding.source, profile, controller, child: undefined,
          rootSessionId, parentTaskId, depth,
          taskId: undefined, active: true, progress: line => record.jobHandle?.updateProgress(line),
          jobHandle: undefined, startedAt: Date.now(), label: taskLabel(args.prompt),
        }
        const taskId = ctx.jobs.start({
          kind: 'background-research', label: record.label, owner,
          outputLimitBytes: 32768,
          run(job) {
            record.jobHandle = job
            const done = (async () => {
              let run
              try {
                await startGate.promise
                if (controller.signal.aborted) return {status:'killed', detail:'cancelled before launch'}
                const childPrompt = [
                  '请独立完成以下研究、自检或请求者授权的维护任务。只使用当前执行域已经开放的工具，不扩大请求者权限；可将耗时步骤留在后台运行并更新真实进度。按任务要求使用工具，并遵守原请求者权限。',
                  '原始任务：', args.prompt.trim(),
                ].join('\n\n')
                await withParentStartLock(owner, async () => {
                  if (controller.signal.aborted) throw new Error('cancelled before child launch')
                  const pending = { parent, source: binding.source, taskId, profile,
                    controller, authority: record.authority, progress: record.progress, active: true, mcp: record.mcp }
                  PENDING_CHILD.set(owner, pending)
                  try {
                    run = await ctx.subagents.start('spawn', {
                      parent,
                      prompt: [{type:'text', text:childPrompt}],
                      signal: controller.signal,
                      maxDepth: MAX_RUNTIME,
                      label: `QQ background research: ${record.label}`,
                    })
                    record.child = pending.child ?? run.localAgent
                    record.linked = pending.linked
                    if (!record.child) throw new Error('research child was not created')
                    if (!record.linked) {
                      record.linked = bindResearchChild(record.child, record.authority)
                    }
                    if (pending.startError) throw pending.startError
                    if (controller.signal.aborted) throw new Error('cancelled during child launch')
                  } finally {
                    record.child ??= pending.child
                    record.linked ??= pending.linked
                    if (PENDING_CHILD.get(owner) === pending) PENDING_CHILD.delete(owner)
                  }
                })
                const result = await run.result
                // Keep unfinished descendants alive until they finish; an
                // explicit cancellation instead kills and drains the subtree.
                const descendants = [...(BY_PARENT.get(record.child.session.id) ?? [])]
                  .map(id => TASKS.get(id)).filter(item => item?.active)
                if (controller.signal.aborted) {
                  for (const item of descendants) {
                    try { ctx.jobs.kill(item.taskId, record.child.session.id, 'ancestor task cancelled') } catch {}
                  }
                }
                const nestedResults = await Promise.allSettled(descendants.map(item => item.done))
                const trailing = nestedResults.flatMap((entry, index) => {
                  if (entry.status !== 'fulfilled') return [`后台子任务 ${descendants[index].taskId} 未能完成。`]
                  if (entry.value?.result) return [`后台子任务 ${descendants[index].taskId}：\n${entry.value.result}`]
                  if (entry.value?.status !== 'completed') return [`后台子任务 ${descendants[index].taskId} 未能完成。`]
                  return []
                })
                const resultText = safeText([textOf(result.output), ...trailing].filter(Boolean).join('\n\n'))
                let status = controller.signal.aborted || result.stopReason === 'aborted'
                  ? 'cancelled' : result.stopReason === 'completed' ? 'completed' : 'failed'
                record.terminalEvent = {
                  event:'settled', parent_session_id: rootSessionId, task_id: taskId, ...lineage,
                  requester_id: binding.source.requester_id, status,
                  ...(resultText ? {text: resultText} : {}), label: record.label,
                  ...terminalModelDetails(ctx, record),
                }
                if (status === 'completed' && !resultText) {
                  status = 'failed'
                  record.terminalEvent = {...record.terminalEvent, status, text:'后台研究无法完成。'}
                }
                record.terminal = {status: record.terminalEvent.status,
                  label: record.terminalEvent.label, detail: result.stopReason}
                if (parentTaskId !== undefined && researchDelegation(parent) && !controller.signal.aborted) {
                  try {
                    parent.steer(createUserMessage({
                      content: [{type:'text', text:`后台子任务 ${taskId}（${status}）：\n${resultText || '未返回结果。'}`}],
                      source: {kind:'agent-message', form:'relay', senderSessionId:record.child.session.id},
                    }))
                  } catch { /* A finished parent collects this result while draining descendants. */ }
                }
                try { await postCallback(callback, record.terminalEvent) }
                catch (notifyError) { ctx.logger?.warn?.('background callback settlement failed: ' + String(notifyError)) }
                return {status: status === 'cancelled' ? 'killed' : status === 'completed' ? 'completed' : 'failed',
                  detail: result.stopReason, result: resultText}
              } catch (error) {
                const status = controller.signal.aborted ? 'cancelled' : 'failed'
                record.terminalEvent ??= {event:'settled', parent_session_id: rootSessionId, ...lineage,
                  task_id: taskId, requester_id: binding.source.requester_id, status,
                  text: status === 'cancelled' ? '后台研究已停止。' : '后台研究无法完成。', label: record.label,
                  ...terminalModelDetails(ctx, record)}
                record.terminal = {status: record.terminalEvent.status,
                  label: record.terminalEvent.label, detail: 'research failed'}
                if (parentTaskId !== undefined && researchDelegation(parent) && !controller.signal.aborted) {
                  try {
                    parent.steer(createUserMessage({
                      content: [{type:'text', text:`后台子任务 ${taskId}：后台研究无法完成。`}],
                      source: {kind:'agent-message', form:'relay', senderSessionId:record.child?.session.id ?? owner},
                    }))
                  } catch { /* The parent may have finished while the child failed. */ }
                }
                try {
                  await postCallback(callback, record.terminalEvent)
                } catch (notifyError) { ctx.logger?.warn?.('background callback settlement failed: ' + String(notifyError)) }
                return {status: status === 'cancelled' ? 'killed' : 'failed',
                  detail: controller.signal.aborted ? 'cancelled' : 'research failed'}
              } finally {
                if (run) {
                  if (record.child) revokeResearchChild(record.child, taskId)
                  try { await run.dispose() } catch (error) { ctx.logger?.warn?.('background child disposal failed: ' + String(error)) }
                } else if (record.child) {
                  revokeResearchChild(record.child, taskId)
                  try { await record.child.cancel?.({kind:'parent'}) } catch {}
                }
                revokeResearchAuthority(record.authority)
                record.active = false
                ACTIVE.delete(taskId)
                mapForParent(owner).delete(taskId)
                pruneTaskHistory()
              }
            })()
            record.done = done
            return {cancel(reason) {
              record.active = false
              for (const id of [...(BY_PARENT.get(record.child?.session?.id) ?? [])]) {
                try { ctx.jobs.kill(id, record.child.session.id, 'ancestor task cancelled') } catch {}
              }
              controller.abort(new Error(reason || 'cancelled'))
              revokeResearchAuthority(record.authority)
              startGate.resolve()
              if (record.child) revokeResearchChild(record.child, taskId)
              void record.child?.cancel?.({kind:'parent'})
            }, done}
          },
        })
        record.taskId = taskId
        TASKS.set(taskId, record)
        mapForParent(owner).add(taskId)
        ACTIVE.set(taskId, record)
        try {
          record.startCallbackAttempted = true
          const ack = await postCallback(callback, {
            event:'started', parent_session_id:rootSessionId, task_id:taskId, ...lineage,
            requester_id:binding.source.requester_id, status:'running', label:record.label,
          })
          if (ack?.accepted === false) throw new Error('background callback did not accept the task')
          if (!ack || !Object.hasOwn(ack, 'mcp')) throw new Error('background MCP delegation was not provided')
          if (ack.mcp !== null) {
            const url = new URL(ack.mcp?.url)
            if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1'
                || typeof ack.mcp?.token !== 'string' || ack.mcp.token.length < 32) {
              throw new Error('invalid background MCP delegation')
            }
          }
          record.mcp = ack.mcp
          if (!record.active || controller.signal.aborted) throw new Error('background task was cancelled before launch')
          record.authority = createResearchAuthority(parent, binding.turn, binding.source,
            taskId, profile, controller.signal, {rootSessionId, parentTaskId, depth})
          startGate.resolve()
          record.progress('等待研究子 Agent 启动')
          return {task_id:taskId, status:'running', label:record.label}
        } catch (error) {
          record.active = false
          controller.abort(new Error('background task registration failed'))
          revokeResearchAuthority(record.authority)
          try { ctx.jobs.kill(taskId, owner, 'start callback failed') } catch {}
          startGate.resolve()
          if (record.startCallbackAttempted) {
            try { await postCallback(callback, {event:'settled', parent_session_id:rootSessionId, ...lineage,
              task_id:taskId, requester_id:binding.source.requester_id, status:'failed',
              text:'后台研究无法启动。', label:record.label})
            } catch (notifyError) { ctx.logger?.warn?.('background callback settlement failed: ' + String(notifyError)) }
          }
          throw new Error('任务状态登记失败，后台研究未启动')
        }
      }

      const taskId = args.task_id
      if (typeof taskId !== 'string' || !taskId) throw new Error(`${args.action} requires task_id`)
      const record = TASKS.get(taskId)
      if (!record || record.parent !== parent || record.source.requester_id !== binding.source.requester_id) {
        throw new Error('research task unavailable in this conversation')
      }
      if (args.action === 'status') {
        if (record.terminalEvent) {
          try { await postCallback(callback, record.terminalEvent) }
          catch (notifyError) { ctx.logger?.warn?.('background callback reconciliation failed: ' + String(notifyError)) }
        }
        if (record.terminal) return {task_id:taskId, status:record.terminal.status,
          label:record.terminal.label ?? record.label,
          ...(record.terminal.detail ? {detail:record.terminal.detail} : {})}
        const job = ctx.jobs.get(taskId, owner)
        const result = {task_id:taskId, status:job.status, label:job.label,
          ...(job.progress ? {detail:job.progress} : {}),
          ...(job.detail ? {detail:job.detail} : {})}
        if (terminalStatus(job.status)) {
          record.terminal = {status:job.status, label:job.label, detail:job.detail}
          try {
            ctx.jobs.remove(taskId, owner)
            record.jobRemoved = true
          } catch (error) { ctx.logger?.warn?.('background terminal job cleanup failed: ' + String(error)) }
          pruneTaskHistory()
        }
        return result
      }
      if (args.action === 'cancel') {
        if (record.terminal) return {task_id:taskId, status:'already-finished'}
        const cancelled = ctx.jobs.kill(taskId, owner, 'requested by requester')
        return {task_id:taskId, status:cancelled === 'requested' ? 'stopping' : 'already-finished'}
      }
      if (args.action === 'update') {
        if (typeof args.prompt !== 'string' || !args.prompt.trim() || args.prompt.length > 4000) {
          throw new Error('update requires an instruction of 1 to 4000 characters')
        }
        const child = record.child
        const delegated = child && researchDelegation(child)
        if (!child || !delegated || !record.active) throw new Error('research child is not ready for an update')
        const message = createUserMessage({
          content: [{type:'text', text:`研究目标补充：\n${args.prompt.trim()}`}],
          source: {kind:'agent-message', form:'relay', senderSessionId:parent.session.id},
        })
        child.steer(message)
        record.progress('已接收研究要求，等待子 Agent 执行')
        return {task_id:taskId, status:'running', detail:'补充要求已交给研究任务'}
      }
      throw new Error('unsupported background research action')
    },
  })
  ctx.effect(() => () => { detachController() }, 'v2 background research controller')
}

export default apply
