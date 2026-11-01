// Isolated reply-intent trial. This request seam must never load in production.
import { readFileSync, appendFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { projectRequest } from './reply-priority-experiment.mjs'

export const name = 'v2-sandbox-reply-intent-trial'
export const inject = ['llm']
export const RULE = '把本轮已通过工具发出的消息当作对方已经读到的前文。生成下一条前，确认前文已完成哪些回应意图，例如接受纠正、认错、确认安排或交代结果；接下来从尚未表达的信息继续。工具消息和最终正文共同构成回复，最终正文承接最后一个尚未发送的完整意思。需要分条时，每条承担一个完整的回应作用，保留理解所需的事实和条件。'
export const EXAMPLES = `以下是虚构的收尾示例，示例事实仅用于演示表达，不是当前对话的事实。

示例一：接受纠正后补充不同的信息。
已核实事实：店铺周六营业，周六下午三点关门。先前误说周六不开门。
用户：周六也开门啊。
assistant 调用 qq_send_origin({"text":"对，周六也开门，这点我说错了。"})
tool 返回：{"submitted":true,"platform_confirmed":false}
assistant 最终正文：周六只开到下午三点，去之前看下时间。

示例二：结果、未知状态和下一步分别承接。
用户：替我建一个周五交周报的提醒。
已知工具结果：本次创建被明确拒绝，未完成登记；查询失败，旧任务状态未知。后续尝试需要权限状态得到确认。
assistant 调用 qq_send_origin({"text":"这次没建成，创建被拒了。"})
tool 返回：{"submitted":true,"platform_confirmed":false}
assistant 调用 qq_send_origin({"text":"清单也没查出来，旧任务在不在还不能确定。"})
tool 返回：{"submitted":true,"platform_confirmed":false}
assistant 最终正文：等权限确认好了，你再叫我试。`
const MARKER = '回复衔接职责：'
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')

export function projectIntent(options, seed) {
  const arm = seed.diagnostics.id.split('--').at(-1)
  if (!['R0', 'R1', 'R2'].includes(arm)) throw new Error(`unknown intent arm ${arm}`)
  const projected = projectRequest(options, seed, {})
  const before = structuredClone(projected.messages[0])
  if (JSON.stringify(before).includes(MARKER)) throw new Error('duplicate intent rule')
  const reminder = arm === 'R0' ? '' : `${MARKER}\n${RULE}${arm === 'R2' ? '\n\n' + EXAMPLES : ''}`
  if (reminder) projected.messages[0].content.push({ type: 'text', text: reminder })
  const expected = structuredClone(before)
  if (reminder) expected.content.push({ type: 'text', text: reminder })
  projected.checks.system_changed_exactly_once = hash(projected.messages[0]) === hash(expected)
  projected.checks.intent_arm = arm
  projected.checks.intent_added_blocks = reminder ? 1 : 0
  projected.hashes.projectedSystem = hash(projected.messages[0])
  projected.changes.system = reminder ? [{ field: 'system.appended-intent-rule', arm, sha256: hash(reminder) }] : []
  return projected
}

export function apply(ctx, config) {
  if (!process.env.SANDBOX_CONTROL_URL) throw new Error('intent experiment is sandbox-only')
  const raw = JSON.parse(readFileSync(config.seedPath, 'utf8'))
  const remap = value => {
    if (typeof value === 'string') {
      for (const [from, to] of [[raw.sourceWorkspace, process.env.V2_GROUP_WORKSPACE_ROOT],
        ['v2-phase1-napcat', 'v2-sandbox'], ['697823265', '91000'], ['2869323076', '99000']]) {
        if (from && to) value = value.split(from).join(to)
      }
      return value
    }
    if (Array.isArray(value)) return value.map(remap)
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,remap(v)]))
    return value
  }
  const seed = remap(raw), owned = new WeakSet()
  ctx.on('llm/stream', (options, next) => (async function* () {
    if (owned.has(options) || !options.sessionId || options.purpose) { yield* next(); return }
    const projected = projectIntent(options, seed)
    const output = { ...options, messages: projected.messages, tools: projected.tools }
    owned.add(output)
    if (config.logFile) appendFileSync(config.logFile, JSON.stringify({
      provider: options.provider, model: options.model, condition: seed.diagnostics.id,
      sourceId: projected.sourceId, changes: projected.changes, checks: projected.checks,
      hashes: projected.hashes, messages: projected.messages, tools: projected.tools,
    }) + '\n', { mode: 0o600 })
    yield* ctx.llm.stream(output)
  })(), { prepend: true })
}
