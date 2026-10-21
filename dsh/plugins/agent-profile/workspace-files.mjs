import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HELPER = fileURLToPath(new URL('./workspace_file_io.py', import.meta.url))
const MAX_BUFFER = 2 * 1024 * 1024
const GUIDE_NAMES = new Set(['AGENTS.md', 'persona.md'])

function errorFor(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}

function mapError(row) {
  const codes = {
    FileNotFoundError: 'FS_NOT_FOUND',
    NotADirectoryError: 'FS_NOT_DIRECTORY',
    IsADirectoryError: 'FS_NOT_REGULAR_FILE',
    PermissionError: 'FS_PERMISSION_DENIED',
    UnicodeDecodeError: 'FS_NOT_TEXT',
    ValueError: /exceeds size limit/.test(row.message ?? '') ? 'FS_TOO_LARGE' : 'FS_IO_ERROR',
  }
  return errorFor(codes[row.error] ?? 'FS_IO_ERROR', row.message ?? 'group workspace read failed')
}

function runHelper(root, request, signal) {
  if (signal?.aborted) return Promise.reject(errorFor('FS_ABORTED', 'workspace read aborted'))
  return new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/python3', ['-I', '-S', HELPER], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { PATH: '/usr/bin:/bin', V2_GROUP_WORKSPACE_ROOT: root },
    })
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (callback, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
      callback(value)
    }
    const abort = () => {
      child.kill('SIGKILL')
      finish(reject, errorFor('FS_ABORTED', 'workspace read aborted'))
    }
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      finish(reject, errorFor('FS_IO_ERROR', 'group workspace read timed out'))
    }, 5000)
    timer.unref?.()
    signal?.addEventListener('abort', abort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', chunk => {
      stdout += chunk
      if (Buffer.byteLength(stdout) > MAX_BUFFER) {
        child.kill('SIGKILL')
        finish(reject, errorFor('FS_TOO_LARGE', 'group workspace helper response exceeds limit'))
      }
    })
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-1000) })
    child.on('error', error => finish(reject, errorFor('FS_IO_ERROR', error.message)))
    child.stdin.on('error', error => {
      child.kill('SIGKILL')
      finish(reject, errorFor('FS_IO_ERROR', error.message))
    })
    child.on('close', code => {
      if (settled) return
      if (code !== 0 && stdout === '') {
        return finish(reject, errorFor('FS_IO_ERROR', `group workspace helper failed: ${stderr.slice(0, 200)}`))
      }
      let response
      try { response = JSON.parse(stdout) } catch {
        return finish(reject, errorFor('FS_IO_ERROR', 'group workspace helper returned invalid data'))
      }
      if (!response.ok) return finish(reject, mapError(response))
      finish(resolve, response.result)
    })
    child.stdin.end(JSON.stringify(request))
  })
}

function pathInWorkspace(workspaceRoot, input) {
  if (typeof input !== 'string' || input.includes('\0')) {
    throw errorFor('FS_SANDBOX_DENIED', 'path is outside the group workspace')
  }
  let relative
  if (input === '/workspace') relative = ''
  else if (input.startsWith('/workspace/')) relative = input.slice('/workspace/'.length)
  else if (input === workspaceRoot) relative = ''
  else if (input.startsWith(`${workspaceRoot}/`)) relative = input.slice(workspaceRoot.length + 1)
  else throw errorFor('FS_SANDBOX_DENIED', 'path is outside the group workspace')
  if (!relative || relative.split('/').some(part => !part || part === '.' || part === '..' || part.includes('\\'))) {
    throw errorFor('FS_SANDBOX_DENIED', 'invalid group workspace path')
  }
  return relative
}

function targetPath(target) {
  const relative = target?.targetKey
  if (typeof relative !== 'string' || !relative || relative.startsWith('/')
      || relative.split('/').some(part => !part || part === '.' || part === '..' || part.includes('\\'))) {
    throw errorFor('FS_SANDBOX_DENIED', 'invalid workspace target')
  }
  return relative
}

/**
 * Create the host-side, read-only view of one verified QQ group workspace.
 * The Python helper verifies the dedicated mount and opens each path relative
 * to its pinned root descriptor with O_NOFOLLOW.
 */
export function createWorkspaceReader({ workspaceRoot, logger = console }) {
  if (typeof workspaceRoot !== 'string' || !path.isAbsolute(workspaceRoot)
      || path.resolve(workspaceRoot) !== workspaceRoot) {
    throw new TypeError('workspaceRoot must be a physical absolute path')
  }

  const readGuide = async (name) => {
    if (!GUIDE_NAMES.has(name)) throw new TypeError('unsupported workspace guide')
    try {
      const result = await runHelper(workspaceRoot, { op: 'read', path: name, kind: 'guide' })
      if (result?.text?.includes('\0')) throw errorFor('FS_NOT_TEXT', 'guide contains a NUL character')
      return result?.text ?? ''
    } catch (error) {
      if (error.code !== 'FS_NOT_FOUND') logger.warn?.(`group workspace guide ${name} unavailable: ${error.message}`)
      return ''
    }
  }

  const files = Object.freeze({
    async resolve(input, { signal } = {}) {
      const relative = pathInWorkspace(workspaceRoot, input)
      const result = await runHelper(workspaceRoot, { op: 'resolve', path: relative }, signal)
      return Object.freeze({ targetKey: result.path, displayPath: `/workspace/${result.path}` })
    },
    async stat(target, signal) {
      const result = await runHelper(workspaceRoot, { op: 'stat', path: targetPath(target) }, signal)
      if (result === null) return undefined
      return { type: result.type, size: result.size ?? undefined, version: result.version }
    },
    async listDir(target) {
      const base = targetPath(target)
      const rows = await runHelper(workspaceRoot, { op: 'list', path: base })
      return rows.map(row => ({
        name: row.name,
        type: row.type,
        size: row.size ?? undefined,
        version: row.version,
        target: Object.freeze({ targetKey: `${base}/${row.name}`, displayPath: `/workspace/${base}/${row.name}` }),
      }))
    },
    async fingerprintSkills() {
      return await runHelper(workspaceRoot, { op: 'fingerprint-skills' })
    },
    async readText(target, signal) {
      const result = await runHelper(workspaceRoot, { op: 'read', path: targetPath(target) }, signal)
      if (result === null) throw errorFor('FS_NOT_FOUND', `workspace file not found: ${target.displayPath}`)
      return result.text
    },
    processPath(target) {
      return target?.displayPath
    },
  })

  return Object.freeze({ readGuide, files })
}
