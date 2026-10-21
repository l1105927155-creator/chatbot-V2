// Shared validation for the router-published, single-turn QQ source.
// This module decides whether a source is authentic; profile plugins decide
// which platform identity and tool set that source may use.
import { createHash } from 'node:crypto'
import { constants, fstatSync, openSync, readFileSync, closeSync, lstatSync, realpathSync } from 'node:fs'
import { basename } from 'node:path'
import { isAbsolute, join } from 'node:path'

export const TURN_SOURCE_FIELDS = Object.freeze([
  'platform_id', 'bot_id', 'conversation_key', 'requester_id',
  'inbound_message_id', 'epoch', 'session_id', 'nonce', 'prompt_sha256',
])
const OPTIONAL_SOURCE_FIELDS = Object.freeze(['image_manifest'])
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])

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
  const keys = Object.keys(source ?? {}).sort().join(',')
  const requiredKeys = [...TURN_SOURCE_FIELDS].sort().join(',')
  const imageKeys = [...TURN_SOURCE_FIELDS, ...OPTIONAL_SOURCE_FIELDS].sort().join(',')
  if (!source || (keys !== requiredKeys && keys !== imageKeys)
    || !Object.values(source).every(text)
    || source.epoch !== epoch || source.session_id !== sessionId
    || !/^[a-f0-9]{64}$/.test(source.prompt_sha256)) {
    throw new Error('invalid current turn source')
  }
  return Object.freeze(source)
}

export function validateClaimedPrompt(message, source) {
  const content = message?.content
  const imageManifest = parseImageManifest(source)
  const expectedLength = imageManifest ? imageManifest.images.length + 1 : 1
  if (!Array.isArray(content) || content.length !== expectedLength || content[0]?.type !== 'text'
    || typeof content[0].text !== 'string'
    || createHash('sha256').update(content[0].text).digest('hex') !== source.prompt_sha256) {
    throw new Error('claimed prompt mismatch')
  }
  if (imageManifest && content.slice(1).some(block => block?.type !== 'image'
    || !block.attachment || typeof block.attachment.attachmentId !== 'string')) {
    throw new Error('claimed image sequence mismatch')
  }
  return true
}

function parseImageManifest(source) {
  if (source.image_manifest === undefined) return undefined
  let manifest
  try { manifest = JSON.parse(source.image_manifest) } catch { throw new Error('invalid trusted image manifest') }
  if (!manifest || Object.keys(manifest).sort().join(',') !== 'images,version' || manifest.version !== 1
    || !Array.isArray(manifest.images) || manifest.images.length < 1 || manifest.images.length > 20) {
    throw new Error('invalid trusted image manifest')
  }
  const filenames = new Set()
  for (const item of manifest.images) {
    if (!item || Object.keys(item).sort().join(',') !== 'filename,mime_type,sha256'
      || typeof item.filename !== 'string' || !/^image-[a-f0-9]{48}\.bin$/.test(item.filename)
      || !/^[a-f0-9]{64}$/.test(item.sha256) || !IMAGE_TYPES.has(item.mime_type)) {
      throw new Error('invalid trusted image manifest entry')
    }
    if (filenames.has(item.filename)) throw new Error('duplicate trusted image proof')
    filenames.add(item.filename)
  }
  return manifest
}

export function sameTurnSource(left, right) {
  return TURN_SOURCE_FIELDS.every(field => left[field] === right[field])
    && left.image_manifest === right.image_manifest
}

/** Verify private raw proofs synchronously before each tool authorization. */
export function verifyTurnImageProofs(sourceRoot, source) {
  const manifest = parseImageManifest(source)
  if (!manifest) return []
  const proofs = []
  let totalBytes = 0
  for (const item of manifest.images) {
    const path = join(sourceRoot, basename(item.filename))
    const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW)
    let data
    try {
      const meta = fstatSync(descriptor)
      if (!meta.isFile() || meta.nlink !== 1 || meta.size < 1 || meta.size > 20 * 1024 * 1024
        || (meta.mode & 0o077) !== 0
        || (typeof process.getuid === 'function' && meta.uid !== process.getuid())) {
        throw new Error('trusted image proof must be a private regular file')
      }
      data = readFileSync(descriptor)
    } finally { closeSync(descriptor) }
    if (createHash('sha256').update(data).digest('hex') !== item.sha256) {
      throw new Error('trusted image proof digest mismatch')
    }
    totalBytes += data.byteLength
    if (totalBytes > 200 * 1024 * 1024) throw new Error('trusted image proofs exceed the aggregate input limit')
    proofs.push({ data, mediaType: item.mime_type })
  }
  return proofs
}

function stable(value) {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']'
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}'
  }
  return JSON.stringify(value)
}

/** Normalize trusted bytes through DSH's own attachment service and bind the returned refs. */
export async function validateTurnImages(ctx, sourceRoot, source, message) {
  const manifest = parseImageManifest(source)
  if (!manifest) return []
  const proofs = verifyTurnImageProofs(sourceRoot, source)
  const attachments = ctx.get?.('attachments') ?? ctx.attachments
  if (!attachments || typeof attachments.saveImages !== 'function') {
    throw new Error('DSH attachment service is unavailable for trusted image validation')
  }
  const refs = await attachments.saveImages(proofs)
  const claimed = message?.content?.slice(1).map(block => block?.attachment)
  if (!Array.isArray(refs) || !Array.isArray(claimed)
    || refs.length !== manifest.images.length || claimed.length !== refs.length
    || refs.some((ref, index) => stable(ref) !== stable(claimed[index]))) {
    throw new Error('claimed image references do not match trusted image proofs')
  }
  return refs
}

export function validateOwnerSource(source, owners) {
  if (source.conversation_key !== 'private:' + source.requester_id
    || !owners.some(owner => owner.platform_id === source.platform_id
      && owner.bot_id === source.bot_id && owner.user_id === source.requester_id)) {
    throw new Error('owner private chat required')
  }
  return true
}

