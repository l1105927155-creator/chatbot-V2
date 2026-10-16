// Shared validation for the router-published, single-turn QQ source.
// This module decides whether a source is authentic; profile plugins decide
// which platform identity and tool set that source may use.
import { createHash } from 'node:crypto'
import { constants, fstatSync, openSync, readFileSync, closeSync, lstatSync, realpathSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'

export const TURN_SOURCE_FIELDS = Object.freeze([
  'platform_id', 'bot_id', 'conversation_key', 'requester_id',
  'inbound_message_id', 'epoch', 'session_id', 'nonce', 'prompt_sha256',
])

const text = value => typeof value === 'string' && value.length > 0
  && value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)

export function privateSourceRoot(path) {
  if (typeof path !== 'string' || !isAbsolute(path)) throw new Error('absolute private source root required')
  const meta = lstatSync(path)
  if (!meta.isDirectory() || meta.isSymbolicLink() || (meta.mode & 0o077) !== 0
    || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
    throw new Error('turn source root must be private and owned by runtime user')
  }
  return realpathSync(path)
}

export function parseConfiguredOwners(raw) {
  const owners = JSON.parse(raw)
  if (!Array.isArray(owners) || owners.length === 0) throw new Error('non-empty V2_QQ_OWNERS required')
  for (const item of owners) {
    if (!item || Object.keys(item).sort().join(',') !== 'bot_id,platform_id,user_id'
      || !Object.values(item).every(text)) throw new Error('invalid V2_QQ_OWNERS')
  }
  return owners
}

export function readTurnSource(sourceRoot, epoch, sessionId) {
  if (!text(sessionId) || !text(epoch)) throw new Error('invalid current session or epoch')
  const file = join(sourceRoot, createHash('sha256').update(sessionId).digest('hex') + '.json')
  const descriptor = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW)
  let source
  try {
    const meta = fstatSync(descriptor)
    if (!meta.isFile() || (meta.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
      throw new Error('turn source file must be private and owned by runtime user')
    }
    source = JSON.parse(readFileSync(descriptor, 'utf8'))
  } finally {
    closeSync(descriptor)
  }
  if (!source || Object.keys(source).sort().join(',') !== [...TURN_SOURCE_FIELDS].sort().join(',')
    || !Object.values(source).every(text)
    || source.epoch !== epoch || source.session_id !== sessionId
    || !/^[a-f0-9]{64}$/.test(source.prompt_sha256)) {
    throw new Error('invalid current turn source')
  }
  return Object.freeze(source)
}

export function validateClaimedPrompt(message, source) {
  const content = message?.content
  if (!Array.isArray(content) || content.length !== 1 || content[0]?.type !== 'text'
    || typeof content[0].text !== 'string'
    || createHash('sha256').update(content[0].text).digest('hex') !== source.prompt_sha256) {
    throw new Error('claimed prompt mismatch')
  }
  return true
}

export function validateOwnerSource(source, owners) {
  if (source.conversation_key !== 'private:' + source.requester_id
    || !owners.some(owner => owner.platform_id === source.platform_id
      && owner.bot_id === source.bot_id && owner.user_id === source.requester_id)) {
    throw new Error('owner private chat required')
  }
  return true
}

