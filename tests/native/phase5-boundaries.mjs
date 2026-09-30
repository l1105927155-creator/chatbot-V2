import assert from 'node:assert/strict'

if (process.argv[2] === 'server') {
  const label = process.argv[3]
  let line = ''
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', chunk => {
    line += chunk
    for (;;) {
      const end = line.indexOf('\n')
      if (end < 0) break
      const raw = line.slice(0, end)
      line = line.slice(end + 1)
      let message
      try { message = JSON.parse(raw) } catch { continue }
      if (message.id === undefined) continue
      let result
      if (message.method === 'initialize') {
        result = { protocolVersion: '2025-11-25', capabilities: { tools: {} }, serverInfo: { name: 'local-fixture', version: '1.0.0' } }
      } else if (message.method === 'tools/list') {
        result = { tools: [{ name: 'probe', description: 'Return this fixture identity', inputSchema: { type: 'object', properties: {}, additionalProperties: false } }] }
      } else if (message.method === 'tools/call') {
        result = { content: [{ type: 'text', text: label }], isError: false }
      } else {
        process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'Method not found' } })}\n`)
        continue
      }
      process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, result })}\n`)
    }
  })
  process.stdin.resume()
} else {
  const { Context } = await import('@deepseek-ai/cordis')
  const SystemPrompt = (await import('@deepseek-ai/dsh-system-prompt')).default
  const ToolRuntime = (await import('@deepseek-ai/dsh-tools')).default
  const McpClient = await import('@deepseek-ai/dsh-mcp-client')
  const { createScope } = await import('@deepseek-ai/dsh-scope')
  const { ToolCallId } = await import('@deepseek-ai/dsh-llm')
  const signal = new AbortController().signal
  let callNumber = 0

  const tool = (name, execute) => ({
    name,
    description: name,
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute,
  })

  async function setup() {
    const ctx = new Context()
    await ctx.plugin(SystemPrompt, {})
    await ctx.plugin(ToolRuntime)
    return ctx
  }

  async function mint(ctx, id) {
    const key = { id }
    let scope
    await ctx.plugin(Object.assign(inner => { scope = createScope(inner, key) }, { inject: ['tools', 'systemPrompt'] }))
    return { scope, key }
  }

  async function attachMcp(scope, name, label) {
    await scope.ctx.plugin(McpClient, {
      transport: 'stdio',
      serverName: name,
      command: process.execPath,
      args: [process.env.V2_DSH_NATIVE_HELPER, 'server', label],
      env: {},
      cwd: process.cwd(),
      failOnStartupError: true,
      toolCallTimeoutMs: 5000,
    })
  }

  async function run(ctx, agent, name, args = {}) {
    const result = await ctx.tools.execute({ signal, callId: ToolCallId(`call-${++callNumber}`), name, arguments: args, ...(agent ? { agent } : {}) })
    const block = result.content[0]
    return block?.type === 'text' ? block.text : JSON.stringify(result.content)
  }

  const evidence = {}

  // Global restriction is applied to the global registry before scope-local
  // definitions are composed; ACP mounts MCP tools through this same scope.
  {
    const ctx = await setup()
    ctx.tools.register(tool('global_keep', () => Promise.resolve('kept')))
    ctx.tools.register(tool('global_hidden', () => Promise.resolve('hidden')))
    const { scope, key } = await mint(ctx, 'scope')
    scope.ctx.tools.register(tool('agent_local', () => Promise.resolve('ran:agent_local')))
    scope.ctx.tools.restrict({ allow: ['global_keep'] })
    await attachMcp(scope, 'fixture', 'fixture-result')
    scope.ctx.tools.guard(exec => exec.name === 'mcp__fixture__probe' ? 'agent guard denied MCP call' : undefined)
    const visible = ctx.tools.schemas(key).map(item => item.name).sort()
    assert.deepEqual(visible, ['agent_local', 'global_keep', 'mcp__fixture__probe'])
    evidence.global_restrict = {
      visible,
      global_hidden_error: await run(ctx, key, 'global_hidden'),
      agent_local_result: await run(ctx, key, 'agent_local'),
      mcp_result: await run(ctx, key, 'mcp__fixture__probe'),
    }
    await scope.dispose()
  }

  // A waterfall listener must call next() to continue; a later listener's
  // allow cannot undo a scope's monotonic guard, which is checked after it.
  {
    const ctx = await setup()
    const { scope, key } = await mint(ctx, 'gate')
    let bodyCalls = 0
    scope.ctx.tools.register(tool('guarded', () => { bodyCalls++; return Promise.resolve('ran') }))
    const waterfallOrder = []
    scope.ctx.on('tools/pre-execute', async (_exec, next) => {
      waterfallOrder.push('pass-through')
      return next()
    })
    scope.ctx.on('tools/pre-execute', async (_exec, next) => {
      waterfallOrder.push('deny-listener')
      await next()
      return { kind: 'allow' }
    })
    scope.ctx.tools.guard(exec => exec.name === 'guarded' ? 'latest rule denied "guarded"' : undefined)
    evidence.pre_execute_and_guard = {
      waterfall_order: waterfallOrder,
      denied: await run(ctx, key, 'guarded'),
      body_calls: bodyCalls,
    }
    await scope.dispose()
  }

  // Two concurrent Agent scopes own distinct real MCP client subprocesses;
  // the scope key determines both tool visibility and the call destination.
  {
    const ctx = await setup()
    const left = await mint(ctx, 'left')
    const right = await mint(ctx, 'right')
    const agentIds = []
    left.scope.ctx.on('tools/pre-execute', exec => { agentIds.push(exec.agent?.id); return { kind: 'allow' } })
    right.scope.ctx.on('tools/pre-execute', exec => { agentIds.push(exec.agent?.id); return { kind: 'allow' } })
    await Promise.all([
      attachMcp(left.scope, 'fixture', 'left-result'),
      attachMcp(right.scope, 'fixture', 'right-result'),
    ])
    const results = await Promise.all([
      run(ctx, left.key, 'mcp__fixture__probe'),
      run(ctx, right.key, 'mcp__fixture__probe'),
    ])
    evidence.concurrent_agents = { results, agent_ids: agentIds.sort() }
    await Promise.all([left.scope.dispose(), right.scope.dispose()])
  }

  // A guard reads current state at each execution. The first operation
  // revokes the rule; the next operation on the same Agent is rejected.
  {
    const ctx = await setup()
    const { scope, key } = await mint(ctx, 'dynamic')
    let allowed = true
    let bodyCalls = 0
    scope.ctx.tools.guard(exec => exec.name === 'operation' && !allowed ? 'latest rule denied "operation"' : undefined)
    scope.ctx.tools.register(tool('operation', () => {
      bodyCalls += 1
      allowed = false
      return Promise.resolve(`operation-${bodyCalls}`)
    }))
    evidence.dynamic_rule = {
      results: [await run(ctx, key, 'operation'), await run(ctx, key, 'operation')],
      body_calls: bodyCalls,
    }
    await scope.dispose()
  }

  process.stdout.write(`${JSON.stringify(evidence)}\n`)
}
