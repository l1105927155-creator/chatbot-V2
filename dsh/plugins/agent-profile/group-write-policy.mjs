/** Current-turn authorization for writes to one group workspace. */
import { constants, fstatSync, openSync, closeSync, readFileSync, lstatSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

const numericId = value => typeof value === 'string' && /^[0-9]+$/.test(value)
const identityFields = ['platform_id', 'bot_id', 'conversation_key']

function privateConsoleState(path) {
  const absolute = resolve(path)
  const directory = lstatSync(dirname(absolute))
  if (!directory.isDirectory() || directory.isSymbolicLink()
      || directory.uid !== process.getuid() || (directory.mode & 0o077) !== 0) {
    throw new Error('console state directory must be private and owned')
  }
  const fd = openSync(absolute, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const meta = fstatSync(fd)
    if (!meta.isFile() || meta.uid !== process.getuid() || (meta.mode & 0o077) !== 0) {
      throw new Error('console state file must be private and owned')
    }
    return JSON.parse(readFileSync(fd, 'utf8'))
  } finally {
    closeSync(fd)
  }
}

function parseOwners(raw) {
  const owners = JSON.parse(raw ?? '[]')
  if (!Array.isArray(owners)) throw new Error('invalid trusted Bot Owner configuration')
  for (const owner of owners) {
    if (!owner || Object.keys(owner).sort().join(',') !== 'bot_id,platform_id,user_id'
        || ![owner.platform_id, owner.bot_id, owner.user_id].every(value =>
          typeof value === 'string' && value.length > 0 && value.trim() === value)) {
      throw new Error('invalid trusted Bot Owner configuration')
    }
  }
  return owners
}

/**
 * Read the console's revisioned sessions mapping on each Bash execution.
 * Missing/malformed configured state denies group-member writes. An unset
 * path deliberately preserves the pre-console deployment contract.
 */
export function canWriteGroupWorkspace(source, {
  stateFile = process.env.V2_CONSOLE_STATE_FILE,
  ownersRaw = process.env.V2_QQ_OWNERS,
} = {}) {
  if (!stateFile) return true
  try {
    const owners = parseOwners(ownersRaw)
    if (owners.some(owner => owner.platform_id === source.platform_id
        && owner.bot_id === source.bot_id && owner.user_id === source.requester_id)) return true
    if (!source || !identityFields.every(key => typeof source[key] === 'string')
        || !numericId(source.requester_id) || !/^group:.+/.test(source.conversation_key)) return false
    const state = privateConsoleState(stateFile)
    if (!state || typeof state !== 'object' || !state.sessions || typeof state.sessions !== 'object'
        || Array.isArray(state.sessions)) return false
    const key = JSON.stringify([source.platform_id, source.bot_id, source.conversation_key])
    const record = state.sessions[key]
    if (!record || !record.identity || !record.settings) return false
    if (identityFields.some(field => record.identity[field] !== source[field])) return false
    const editors = record.settings.editors
    return Array.isArray(editors) && editors.every(numericId) && editors.includes(source.requester_id)
  } catch {
    return false
  }
}
