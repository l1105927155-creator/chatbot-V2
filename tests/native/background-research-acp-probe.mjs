// Test-only real ACP adapter and controllably blocked native Web provider.
import { appendFileSync, existsSync } from 'node:fs'
import { LlmAdapter } from '../../dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
export const name = 'background-research-acp-probe'
export const inject = ['llm', 'web']
const log = row => appendFileSync(process.env.V2_BG_PROBE_LOG, JSON.stringify(row) + '\n')
function* text(value) {
  yield { type: 'block-start', index: 0, blockType: 'text' }
  yield { type: 'text-delta', index: 0, text: value }
  yield { type: 'block-end', index: 0, block: { type: 'text', text: value } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}
function* call(name, args, id) {
  yield { type: 'block-start', index: 0, blockType: 'tool-call' }
  yield { type: 'block-end', index: 0, block: {
    type: 'tool-call', id, name, arguments: JSON.stringify(args),
  } }
  yield { type: 'finish', reason: { kind: 'tool-calls' } }
}
function findTaskId(value, label, seen = new Set()) {
  if (typeof value === 'string') {
    try { return findTaskId(JSON.parse(value), label, seen) } catch { return undefined }
  }
  if (!value || typeof value !== 'object' || seen.has(value)) return undefined
  seen.add(value)
  if (value.label === label && typeof value.task_id === 'string') return value.task_id
  for (const child of Object.values(value)) {
    const found = findTaskId(child, label, seen)
    if (found) return found
  }
  return undefined
}
class Adapter extends LlmAdapter {
  seen = new Set()
  children = new Map()
  async resolveModel(provider, model) { return { provider, id: model, name: model } }
  async *stream(options) {
    const users = options.messages.filter(m => m.role === 'user')
    const child = users.some(m => m.content?.some(b => b.type === 'text'
      && b.text.startsWith('请独立完成以下研究任务')))
    const last = users.findLast(m => m.content?.some(b => b.type === 'text'
      && b.text.includes('"latest_user_journal_id"'))) ?? users.at(-1)
    const raw = last?.content?.find(b => b.type === 'text')?.text ?? ''
    const lines = raw.split('\n')
    let source = raw
    try {
      const header = JSON.parse(lines[0])
      source = lines.slice(1).find(line => JSON.parse(line).journal_id === header.latest_user_journal_id) ?? raw
    } catch {}
    const key = options.sessionId + ':' + (last?.id ?? source)
    log({ kind: 'model', session: options.sessionId, source,
          tools: (options.tools ?? []).map(t => t.name) })
    if (child) {
      const stage = this.children.get(options.sessionId) ?? 0
      this.children.set(options.sessionId, stage + 1)
      const tools = (options.tools ?? []).map(t => t.name)
      const childText = users.flatMap(m => m.content ?? [])
        .filter(b => b.type === 'text').map(b => b.text).join('\n')
      const parallel = childText.match(/BG_CHILD_([A-D])/)
      if (tools.some(t => t.startsWith('mcp__') || t === 'background_research'
        || t === 'write' || t === 'edit')) throw new Error('unexpected child authority')
      const fileTool = tools.includes('read') || tools.includes('bash')
      if (stage === 0 && fileTool) {
        yield* call(tools.includes('read') ? 'read' : 'bash', tools.includes('read')
          ? { file_path: process.env.V2_BG_PROBE_FILE }
          : { command: "printf GROUP_READ_OK; if [ -e /home/abasun/chatbot-V2/PLAN.md ]; then exit 9; fi",
              description: 'Read the isolated group workspace' },
        'file-' + options.sessionId)
      } else if (parallel && stage === (fileTool ? 1 : 0)) {
        yield* call('web_fetch', { url: `https://background.fixture/research?child=${parallel[1]}` },
          'fetch-' + parallel[1] + '-' + options.sessionId)
      } else if (stage === 0 || (stage === 1 && fileTool)) {
        if (stage === 1) {
          const result = options.messages.at(-1)
          if (result?.isError) throw new Error('delegated file access failed: ' + JSON.stringify(result))
          log({kind:'file-verified', tools})
        }
        yield* call('web_fetch', { url: 'https://background.fixture/research' }, 'fetch-' + options.sessionId)
      } else if (parallel) {
        yield* text(`BACKGROUND_COMPLETE_${parallel[1]}`)
      } else {
        yield* text('BACKGROUND_COMPLETE ' + (JSON.stringify(users).includes('ADDITIONAL_REQUIREMENT')
          ? 'ADDITIONAL_REQUIREMENT_APPLIED' : 'ORIGINAL_REQUIREMENT'))
      }
      return
    }
    const foregroundBlock = source.includes('FG_BLOCK')
    if (foregroundBlock) {
      if (!this.seen.has(key)) {
        this.seen.add(key)
        yield* call('web_fetch', { url: 'https://background.fixture/research?foreground=1' },
          'foreground-block-' + options.sessionId)
      } else {
        yield* text('FOREGROUND_BLOCK_COMPLETED')
      }
      return
    }
    const match = source.match(/BG_(PAR_)?(START|STATUS|UPDATE|CANCEL)(?:_([A-D]))?/)
    if (match && !this.seen.has(key)) {
      this.seen.add(key)
      const parallel = match[1] === 'PAR_'
      const action = match[2].toLowerCase()
      const suffix = match[3]
      const label = parallel ? `BG_CHILD_${suffix}` : undefined
      const ids = [...JSON.stringify(options.messages).matchAll(/background-research-\d+/g)]
      const taskId = parallel ? findTaskId(options.messages, label) : ids.at(-1)?.[0]
      yield* call('background_research', {
        action, ...(action === 'start' ? { prompt: parallel
          ? label : 'BG_CHILD: verify the implementation using web tools.' }
          : { task_id: taskId }),
        ...(action === 'update' ? { prompt: 'ADDITIONAL_REQUIREMENT: focus on group chat.' } : {}),
      }, 'background-' + options.sessionId + '-' + this.seen.size)
      return
    }
    yield* text(match ? 'FRONTEND_ACK ' + JSON.stringify(options.messages.at(-1)) : 'FOREGROUND_RESPONSIVE')
  }
}
export function apply(ctx) {
  ctx.llm.registerAdapter(['background-native-fixture'], new Adapter())
  ctx.web.registerFetchProvider({
    id: 'background-fixture', available: () => true,
    async fetch(request, signal) {
      const child = new URL(request.url).searchParams.get('child')
      log({ kind: 'fetch-start', url: request.url, ...(child ? {child} : {}) })
      const release = child
        ? process.env[`V2_BG_PROBE_RELEASE_${child}`]
        : process.env.V2_BG_PROBE_RELEASE
      while (!existsSync(release)) {
        signal?.throwIfAborted()
        await new Promise(resolve => setTimeout(resolve, 20))
      }
      log({ kind: 'fetch-end', ...(child ? {child} : {}) })
      return { url: request.url, statusCode: 200,
        body: { kind: 'text', content: 'Verified native research fixture.' }, truncated: false }
    },
  })
}
