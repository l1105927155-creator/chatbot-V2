/** Native, per-group application calls with data-only write authority. */
import { TOOL_ABORTED } from '../../node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import { HarnessError } from '../../node_modules/@deepseek-ai/dsh-llm/lib/index.js'

export const name = 'v2-group-app'
export const inject = ['tools', 'shell', 'sandboxPolicy']

const appSlug = '^[a-z0-9][a-z0-9_-]*$'

const parameters = {
  type: 'object', additionalProperties: false, required: ['app', 'operation', 'args'],
  properties: {
    app: { type: 'string', minLength: 1, maxLength: 64, pattern: appSlug },
    operation: { type: 'string', minLength: 1, maxLength: 32, pattern: '^[a-z][a-z0-9_]*$' },
    args: { type: 'array', maxItems: 32, items: { type: 'string', maxLength: 1024 } },
    timeoutMs: { type: 'integer', minimum: 1, maximum: 60000 },
  },
}

export function apply(ctx) {
  ctx.tools.register({
    name: 'group_app',
    description: '运行群工作区 app.json 清单明确开放的一个应用操作。应用只能修改自己的 data 目录；请将参数逐项作为字符串传入。',
    parameters,
    output: {
      schema: { type: 'object', additionalProperties: false, required: ['exitCode', 'stdout', 'stderr'], properties: {
        exitCode: { type: 'integer' }, stdout: { type: 'string' }, stderr: { type: 'string' },
        timedOut: { type: 'boolean' }, timeoutMs: { type: 'integer' },
      } },
      render: (_args, value) => [{ type: 'text', text: [value.stdout, value.stderr,
        `exitCode=${value.exitCode}`, value.timedOut ? `timedOut=${value.timeoutMs}ms` : '']
        .filter(Boolean).join('\n') }],
    },
    async execute(args, exec) {
      if (!args || typeof args.app !== 'string' || !/^[a-z0-9][a-z0-9_-]*$/.test(args.app)) throw new Error('invalid group app slug')
      if (typeof args.operation !== 'string' || !/^[a-z][a-z0-9_]*$/.test(args.operation)) throw new Error('invalid group app operation')
      if (!Array.isArray(args.args) || args.args.length > 32
          || args.args.some(value => typeof value !== 'string' || value.length > 1024
            || value.includes('\0') || !value.isWellFormed())) throw new Error('invalid group app arguments')
      if (Buffer.byteLength(JSON.stringify(args), 'utf8') > 8192) throw new Error('group app request exceeds 8 KiB')
      if (args.timeoutMs !== undefined && (!Number.isSafeInteger(args.timeoutMs)
          || args.timeoutMs < 1 || args.timeoutMs > 60000)) {
        throw new Error('group app timeout must be between 1 and 60000 milliseconds')
      }
      if (exec.signal.aborted) throw exec.signal.reason ?? new HarnessError('tool call aborted', TOOL_ABORTED)
      const request = {
        command: JSON.stringify({ app: args.app, operation: args.operation, args: args.args }),
        signal: exec.signal,
        sandboxPolicy: ctx.sandboxPolicy.resolve({ session: exec.agent.session }),
        ...(args.timeoutMs === undefined ? {} : { timeoutMs: args.timeoutMs }),
      }
      const process = await ctx.shell.execute(ctx.shell.resolve(request))
      const result = await process.result()
      if (!result || result.exitCode === undefined || !result.stdout || !result.stderr) {
        throw new Error('native shell returned an invalid result: ' + JSON.stringify(result))
      }
      if (result.aborted || exec.signal.aborted) {
        const error = new HarnessError('tool call aborted', TOOL_ABORTED)
        error.name = 'AbortError'
        throw error
      }
      return {
        exitCode: result.exitCode,
        stdout: result.stdout.text,
        stderr: result.stderr.text,
        ...(result.timedOut ? { timedOut: true, timeoutMs: result.timeoutMs } : {}),
      }
    },
  })
}

export default { name, inject, apply }
