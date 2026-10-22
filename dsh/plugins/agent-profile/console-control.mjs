/** Private host control adapter for compaction and read-only task receipts. */
import { createServer } from 'node:net'
import { chmodSync, lstatSync, unlinkSync } from 'node:fs'
import { dirname, isAbsolute } from 'node:path'
import { timingSafeEqual } from 'node:crypto'
import { terminalEvents } from './background-research.mjs'

export const name = 'v2-console-control'
export const inject = ['agents', 'compaction']

export async function apply(ctx, config = {}) {
  const path = config.socketPath ?? process.env.V2_CONSOLE_CONTROL_SOCKET
  const token = config.token ?? process.env.V2_CONSOLE_CONTROL_TOKEN
  if (!path && !token) return
  if (!isAbsolute(path) || typeof token !== 'string' || token.length < 32) {
    throw new Error('private console control configuration required')
  }
  const directory = lstatSync(dirname(path))
  if (!directory.isDirectory() || directory.isSymbolicLink()
      || directory.uid !== process.getuid() || (directory.mode & 0o077) !== 0) {
    throw new Error('private console control directory required')
  }
  const sockets = new Set()
  const server = createServer(socket => {
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    socket.on('error', () => {})
    let input = ''
    let dispatched = false
    socket.setTimeout(300000, () => socket.destroy())
    socket.on('data', async chunk => {
      if (dispatched) return
      input += chunk.toString('utf8')
      if (Buffer.byteLength(input) > 4096) { socket.destroy(); return }
      if (!input.includes('\n')) return
      dispatched = true
      try {
        const request = JSON.parse(input.slice(0, input.indexOf('\n')))
        const provided = Buffer.from(typeof request.token === 'string' ? request.token : '')
        const expected = Buffer.from(token)
        if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
          socket.end(JSON.stringify({ok: false, code: 'UNAUTHORIZED'}) + '\n'); return
        }
        if (request.action === 'background_terminal'
            && typeof request.session_id === 'string' && typeof request.task_id === 'string') {
          const event = terminalEvents(request.session_id)
            .find(item => item.task_id === request.task_id)
          socket.end(JSON.stringify({ok: true, event: event ?? null}) + '\n'); return
        }
        if (request.action !== 'compact' || typeof request.session_id !== 'string') {
          socket.end(JSON.stringify({ok: false, code: 'INVALID_REQUEST'}) + '\n'); return
        }
        const agent = ctx.agents.get(request.session_id)
        if (!agent) { socket.end(JSON.stringify({ok: false, code: 'SESSION_NOT_LOADED'}) + '\n'); return }
        // The native API owns idle/busy checks, maintenance serialization,
        // summary generation and persistence. Do not run a chat prompt here.
        const result = await ctx.compaction.compactNow(agent, new AbortController().signal)
        socket.end(JSON.stringify({ok: true, compacted: result !== null}) + '\n')
      } catch {
        // Error messages and summary text may contain provider/private data.
        socket.end(JSON.stringify({ok: false, code: 'COMPACTION_FAILED'}) + '\n')
      }
    })
  })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(path, () => { server.removeListener('error', reject); resolve() })
  })
  chmodSync(path, 0o600)
  ctx.on('dispose', () => {
    for (const socket of sockets) socket.destroy()
    server.close()
    try { unlinkSync(path) } catch (error) { if (error.code !== 'ENOENT') throw error }
  })
}
