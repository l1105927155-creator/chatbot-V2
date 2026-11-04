// Test-only real ACP adapter and controllably blocked native Web provider.
import { appendFileSync, existsSync, readFileSync } from 'node:fs'
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
  parentToolSchemas = []
  schemaOf(tools) {
    return (tools ?? []).map(tool => Object.fromEntries(
      ['name','description','parameters','inputSchema','schema']
        .filter(key => Object.hasOwn(tool, key)).map(key => [key, tool[key]])))
  }
  async resolveModel(provider, model) { return { provider, id: model, name: model } }
  async *stream(options) {
    const users = options.messages.filter(m => m.role === 'user')
    const child = users.some(m => m.content?.some(b => b.type === 'text'
      && b.text.startsWith('请独立完成以下研究、自检')))
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
    if (!child) this.parentToolSchemas = this.schemaOf(options.tools)
    log({ kind: 'model', session: options.sessionId, source,
          tools: (options.tools ?? []).map(t => t.name) })
    if (child) {
      const stage = this.children.get(options.sessionId) ?? 0
      this.children.set(options.sessionId, stage + 1)
      const tools = (options.tools ?? []).map(t => t.name)
      const profile = process.env.V2_EXECUTION_PROFILE
      log({kind:'child-tools', profile, tools})
      const childSchemas = this.schemaOf(options.tools)
      const parentByName = new Map(this.parentToolSchemas.map(tool => [tool.name, tool]))
      const childByName = new Map(childSchemas.map(tool => [tool.name, tool]))
      const sameNameMcp = [...parentByName.keys()].filter(name => name.startsWith('mcp__'))
      const differences = {
        parent_only: [...parentByName.keys()].filter(name => !childByName.has(name)).sort(),
        child_only: [...childByName.keys()].filter(name => !parentByName.has(name)).sort(),
        schema_changed: [...parentByName.keys()].filter(name => childByName.has(name)
          && JSON.stringify(parentByName.get(name)) !== JSON.stringify(childByName.get(name))).sort(),
      }
      const allParentMcpVisible = sameNameMcp.every(name => childByName.has(name))
      const backgroundResearch = {
        parent: parentByName.get('background_research') ?? null,
        child: childByName.get('background_research') ?? null,
      }
      log({kind:'tool-schema-comparison', profile, all_parent_mcp_visible:allParentMcpVisible,
           parent_mcp_names:sameNameMcp.sort(), differences, background_research:backgroundResearch})
      if (!allParentMcpVisible) throw new Error('one or more parent MCP tools are missing in the child')
      const childText = users.flatMap(m => m.content ?? [])
        .filter(b => b.type === 'text').map(b => b.text).join('\n')
      if (childText.includes('nested-background-probe')) {
        if (childText.includes('PARENT_FINISHES_EARLY') && stage === 0) {
          yield* call('web_fetch', {url:'https://background.fixture/research?grandchild=1'}, 'grandchild-gate-' + options.sessionId)
          return
        }
        log({kind:'nested-child-completed'})
        yield* text('NESTED_RESEARCH_COMPLETE')
        return
      }
      if (childText.includes('BG_NESTED_PROBE')) {
        if (stage === 0) {
          yield* call('background_research', {action:'start', prompt:'nested-background-probe: verify nested research.'},
            'generic-nested-start-' + options.sessionId)
        } else if (stage === 1) {
          const result = JSON.stringify(options.messages.at(-1))
          if (!result.includes('running')) throw new Error('generic nested start failed: ' + result)
          yield* call('web_fetch', {url:'https://background.fixture/research?wait_nested=1'},
            'generic-nested-gate-' + options.sessionId)
        } else {
          const relayed = JSON.stringify(options.messages).includes('NESTED_RESEARCH_COMPLETE')
          log({kind:'generic-nested-result', profile, relayed})
          if (!relayed) throw new Error('nested result not relayed')
          yield* text('GENERIC_NESTED_PARENT_DONE')
        }
        return
      }
      const historyProbe = childText.includes('BG_HISTORY_PROBE')
      const parallel = childText.match(/BG_CHILD_([A-D])/)
      if (profile !== 'owner-host' && tools.some(t => t === 'write' || t === 'edit')) {
        throw new Error('unexpected child write authority')
      }
      if (profile === 'owner-host'
        && !['bash', 'read', 'read_image', 'write', 'edit', 'glob', 'grep', 'web_search', 'web_fetch', 'skill']
          .every(name => tools.includes(name))) throw new Error('Owner-host self-check tools are unavailable')
      if (profile === 'group-sandbox' && !tools.includes('group_app')) {
        throw new Error('group application tool is unavailable to the sandbox child')
      }
      const fileTool = tools.includes('read') || tools.includes('bash')
      if (historyProbe && !tools.includes('mcp__astrbot__history_recent')) {
        throw new Error('history_recent is unavailable to the background child')
      }
      if (historyProbe && stage === 0) {
        yield* call('web_fetch', { url: 'https://background.fixture/research?history=1' },
          'history-gate-' + options.sessionId)
      } else if (historyProbe && stage === 1) {
        yield* call('mcp__astrbot__history_recent', { limit: 100 },
          'history-' + options.sessionId)
      } else if (historyProbe && stage === 2) {
        const previous = JSON.stringify(options.messages)
        const localSeen = previous.includes('LOCAL_HISTORY_SENTINEL')
        const foreignSeen = previous.includes('FOREIGN_GROUP_SENTINEL')
        log({kind:'history-verified', profile, localSeen, foreignSeen, tools})
        if (!localSeen || foreignSeen) {
          throw new Error(`history scope mismatch: local=${localSeen} foreign=${foreignSeen}`)
        }
        yield* call('mcp__astrbot__qq_send_origin', {text:'BACKGROUND_CHILD_TEXT_SEND'},
          'history-text-send-' + options.sessionId)
      } else if (historyProbe && stage === 3) {
        yield* call('mcp__astrbot__qq_file_send',
          {content:'BACKGROUND_CHILD_FILE_SEND', name:'child-report.md'},
          'history-file-send-' + options.sessionId)
      } else if (historyProbe && stage === 4) {
        log({kind:'child-send-tools-verified', profile,
             results: JSON.stringify(options.messages).includes('BACKGROUND_CHILD_TEXT_SEND')})
        yield* call('background_research', {
          action:'start', prompt:'nested-background-probe: bounded test-only child capability check; do not send QQ messages or modify files.' +
            (childText.includes('PARENT_FINISHES_EARLY') ? ' PARENT_FINISHES_EARLY' : '')
        }, 'nested-background-start-' + options.sessionId)
      } else if (historyProbe && stage === 5) {
        const nestedResult = options.messages
          .filter(m => m.role === 'tool'
            && m.source?.callId?.startsWith('nested-background-start-'))
          .flatMap(m => m.content ?? []).filter(b => b.type === 'text')
          .map(b => b.text).join('\n')
        log({kind:'background-research-recursion-probe', profile,
             toolsAvailable:tools.includes('background_research'),
             nestedResult})
        if (!nestedResult.includes('running')) throw new Error('nested start failed: ' + nestedResult)
        if (childText.includes('PARENT_FINISHES_EARLY')) {
          yield* text('BACKGROUND_PARENT_FINISHED_EARLY')
          return
        }
        yield* call('web_fetch', {url:'https://background.fixture/research?wait_nested=1'},
          'nested-gate-' + options.sessionId)
      } else if (historyProbe && stage === 6) {
        log({kind:'nested-parent-result', session: options.sessionId,
             messages: JSON.stringify(options.messages).includes('NESTED_RESEARCH_COMPLETE')})
        yield* text('BACKGROUND_HISTORY_READ_OK')
      } else if (stage === 0 && fileTool) {
        yield* call(profile === 'owner-host' || !tools.includes('read') ? 'bash' : 'read',
          profile === 'owner-host'
            ? { command: 'printf OWNER_HOST_SELF_CHECK_OK', description: 'Check the owner host runtime' }
            : tools.includes('read') ? { file_path: process.env.V2_BG_PROBE_FILE }
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
          if (profile === 'owner-host'
            && !JSON.stringify(result).includes('OWNER_HOST_SELF_CHECK_OK')) {
            throw new Error('Owner-host child bash self-check did not complete')
          }
          log({kind:'file-verified', tools})
        }
        yield* call('web_fetch', { url: 'https://background.fixture/research' }, 'fetch-' + options.sessionId)
      } else if (childText.includes('LONG_RESULT')) {
        yield* text('中文结论'.repeat(2000))
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
    const match = source.includes('BG_HISTORY_START') || source.includes('BG_NESTED_START')
      ? ['BG_START', undefined, 'START', undefined]
      : source.match(/BG_(PAR_)?(START|STATUS|UPDATE|CANCEL)(?:_([A-D]))?/)
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
          ? label : 'BG_CHILD: verify the implementation using web tools.' +
            (source.includes('BG_HISTORY_START') ? ' BG_HISTORY_PROBE' : '') +
            (source.includes('BG_NESTED_START') ? ' BG_NESTED_PROBE' : '') +
            (source.includes('PARENT_FINISHES_EARLY') ? ' PARENT_FINISHES_EARLY' : '') +
            (source.includes('BG_START_LONG_RESULT') ? ' LONG_RESULT' : '') }
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
      const parsed = new URL(request.url)
      const child = parsed.searchParams.get('child')
      if (parsed.searchParams.has('wait_nested')) {
        while (!existsSync(process.env.V2_BG_PROBE_LOG) || !readFileSync(process.env.V2_BG_PROBE_LOG, 'utf8').includes('nested-child-completed')) {
          signal?.throwIfAborted()
          await new Promise(resolve => setTimeout(resolve, 20))
        }
        await new Promise(resolve => setTimeout(resolve, 100))
      }
      log({ kind: 'fetch-start', url: request.url, ...(child ? {child} : {}) })
      const release = parsed.searchParams.has('grandchild')
        ? process.env.V2_BG_PROBE_NESTED_RELEASE
        : child ? process.env[`V2_BG_PROBE_RELEASE_${child}`]
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
