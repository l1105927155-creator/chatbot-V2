// Read only host-configured guide paths. Never discover files in group workspaces.
import { constants, openSync, fstatSync, readFileSync, closeSync } from 'node:fs'
import { isAbsolute } from 'node:path'

const MAX_BYTES = 128 * 1024
const decoder = new TextDecoder('utf-8', { fatal: true })

export function readGuideFile(path, { optional = false } = {}) {
  if (typeof path !== 'string' || !isAbsolute(path)) throw new TypeError('absolute trusted guide path required')
  let fd
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  } catch (error) {
    if (optional && error.code === 'ENOENT') return ''
    throw error
  }
  try {
    const meta = fstatSync(fd)
    if (!meta.isFile() || meta.nlink !== 1
      || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
      throw new Error('guide must be an owned regular file')
    }
    if (meta.size > MAX_BYTES) throw new Error('guide exceeds 128 KiB')
    const bytes = readFileSync(fd)
    if (bytes.length > MAX_BYTES) throw new Error('guide exceeds 128 KiB')
    const text = decoder.decode(bytes).trim()
    if (!text || text.includes('\0')) throw new Error('guide must contain nonempty UTF-8 text')
    return text
  } finally {
    closeSync(fd)
  }
}

// Running Agents keep their last valid guide during an editor's truncate/write
// window. Initial invalid configuration still fails; state/authorization files
// never pass through this cache. A later valid save is read on the next request.
export function createGuideReader(logger) {
  const valid = new Map()
  const warned = new Set()
  return (path, options) => {
    try {
      const text = readGuideFile(path, options)
      valid.set(path, text)
      if (warned.delete(path)) logger?.info('trusted guide update recovered: ' + path)
      return text
    } catch (error) {
      if (!valid.has(path)) throw error
      if (!warned.has(path)) {
        warned.add(path)
        logger?.warn('trusted guide update rejected; keeping last valid text: ' + path)
      }
      return valid.get(path)
    }
  }
}
