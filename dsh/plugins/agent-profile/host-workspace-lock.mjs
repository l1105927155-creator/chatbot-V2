/** Hold the Owner's private workspace flock across one native tool operation. */
import { spawn as nodeSpawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const helper = fileURLToPath(new URL('./workspace_lock.py', import.meta.url))
const python = '/usr/bin/python3'
const readyTimeoutMs = 10_000

export function ownerHostLockMode(exec) {
  if (exec?.name === 'bash' || exec?.name === 'write' || exec?.name === 'edit') return 'exclusive'
  if (['read', 'read_image', 'glob', 'grep', 'skill'].includes(exec?.name)) return 'shared'
  return null
}

function lockFileFromEnv(env = process.env) {
  const path = env.V2_OWNER_WORKSPACE_LOCK_FILE
  if (path === undefined || path === '') return null
  if (typeof path !== 'string' || !path.startsWith('/') || /[\x00-\x1f\x7f]/.test(path)) {
    throw new Error('invalid trusted Owner workspace lock path')
  }
  return path
}

async function terminate(child) {
  if (child.exitCode !== null || child.signalCode !== null) return
  await new Promise(resolve => {
    let grace
    let forced
    const done = () => {
      clearTimeout(grace)
      clearTimeout(forced)
      resolve()
    }
    child.once('close', done)
    child.kill('SIGTERM')
    grace = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
      forced = setTimeout(done, 500)
      forced.unref?.()
    }, 500)
    grace.unref?.()
  })
}

async function acquire(lockPath, shared, signal, spawn) {
  if (signal?.aborted) signal.throwIfAborted()
  const args = ['-I', helper, '--hold', lockPath]
  if (shared) args.push('--shared')
  const child = spawn(python, args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { PATH: '/usr/bin:/bin', LC_ALL: 'C', PYTHONNOUSERSITE: '1' },
  })
  let stdout = ''
  let stderr = ''
  let settled = false
  let timer
  let abortHandler
  try {
    await new Promise((resolve, reject) => {
      const finish = (error) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        signal?.removeEventListener('abort', abortHandler)
        error ? reject(error) : resolve()
      }
      abortHandler = () => {
        void terminate(child)
        finish(new Error('Owner workspace lock acquisition aborted'))
      }
      signal?.addEventListener('abort', abortHandler, { once: true })
      timer = setTimeout(() => finish(new Error('Owner workspace lock acquisition timed out')), readyTimeoutMs)
      child.stdout.setEncoding('utf8')
      child.stderr.setEncoding('utf8')
      child.stdout.on('data', chunk => {
        stdout += chunk
        if (stdout.includes('\n')) {
          if (stdout.split('\n', 1)[0] === 'READY') finish()
          else finish(new Error('Owner workspace lock helper returned an invalid handshake'))
        }
      })
      child.stderr.on('data', chunk => { stderr += chunk })
      child.once('error', error => finish(new Error(`Owner workspace lock helper failed: ${error.message}`)))
      child.once('close', code => finish(new Error(`Owner workspace lock helper exited before READY (${code}): ${stderr.trim()}`)))
      if (signal?.aborted) abortHandler()
    })
    return child
  } catch (error) {
    child.stdin.destroy()
    await terminate(child)
    throw error
  }
}

async function release(child) {
  if (!child.stdin.destroyed) child.stdin.end()
  if (child.exitCode !== null || child.signalCode !== null) return
  await new Promise(resolve => {
    const timer = setTimeout(() => {
      void terminate(child).finally(resolve)
    }, 1500)
    timer.unref?.()
    child.once('close', () => { clearTimeout(timer); resolve() })
  })
}

export async function withOwnerWorkspaceLock(exec, next, {
  env = process.env, spawn = nodeSpawn,
} = {}) {
  const mode = ownerHostLockMode(exec)
  const lockPath = lockFileFromEnv(env)
  if (!mode || !lockPath) return next()
  const child = await acquire(lockPath, mode === 'shared', exec?.signal, spawn)
  try {
    if (exec?.signal?.aborted) exec.signal.throwIfAborted()
    return await next()
  } finally {
    await release(child)
  }
}
