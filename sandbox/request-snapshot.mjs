// Sandbox observation only. Estimates are deliberately separate from provider usage.
import { createHash } from 'node:crypto'
import { mkdirSync, openSync, closeSync, writeFileSync, lstatSync, readFileSync, constants } from 'node:fs'
import { join } from 'node:path'
import { estimateContent } from '../dsh/node_modules/@deepseek-ai/dsh-token-meter/lib/types/estimate.js'

export const digest = value => createHash('sha256').update(value).digest('hex')
export function describe(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? null)
  return { sha256: digest(text), bytes: Buffer.byteLength(text), chars: text.length,
    token_estimate: estimateContent([{ type: 'text', text }]),
    token_estimator: 'dsh-token-meter/estimateContent@0.2.0-rc.1' }
}

export function toolSnapshot(tools = []) {
  return tools.map(tool => ({ name: tool.name, description: describe(tool.description ?? ''),
    schema: describe(tool.parameters ?? {}), defer_loading: tool.deferLoading === true }))
}

export function catalogSnapshot(skills = [], provenance = []) {
  return skills.map(skill => {
    const local = skill.path?.startsWith('/workspace/skills/') === true
    const chain = provenance.find(item => item.name === skill.name)
    return { name: skill.name, origin: local ? 'group-workspace' : skill.source,
      provider: skill.provider, description: describe(skill.description ?? ''),
      invocation: skill.invocation,
      // This hashes routing metadata, not an unloaded Skill body. The loaded body
      // is covered by the actual skill tool result's hash.
      descriptor: describe({ name: skill.name, description: skill.description,
        whenToUse: skill.whenToUse, invocation: skill.invocation }),
      hash_scope: 'catalog-metadata', shadowed: chain?.shadowed ?? [],
      warning: local && ['chat-history', 'filesystem', 'background-research'].includes(skill.name)
        ? 'local-foundation-guide-override' : undefined }
  })
}

export function effectiveCatalog(messages = []) {
  const message = messages.findLast(item => item.source?.kind === 'skill-catalog')
  return { catalog_observed: Boolean(message),
    catalog: (message?.source?.entries ?? []).map(item => ({ name: item.name,
      description: describe(item.description ?? '') })),
    catalog_content: message ? describe(message.content) : undefined }
}

export function requestSettings(options) {
  return Object.fromEntries(['reasoningEffort', 'temperature', 'maxTokens', 'stop', 'purpose']
    .filter(key => options[key] !== undefined).map(key => [key, options[key]]))
}

export function comparable(value) {
  if (Array.isArray(value)) return value.map(comparable)
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !['token_estimate', 'token_estimator'].includes(key))
    .map(([key, item]) => [key, comparable(item)]))
  return value
}

export function saveArtifact(directory, value) {
  const serialized = JSON.stringify(value)
  const meta = describe(serialized)
  if (!directory) return { ...meta, saved: false, reason: 'artifact-directory-unconfigured' }
  if (meta.bytes > 16 * 1024 * 1024) return { ...meta, saved: false, reason: 'artifact-size-limit' }
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  const parent = lstatSync(directory)
  if (!parent.isDirectory() || parent.isSymbolicLink() || (parent.mode & 0o077) !== 0) {
    throw new Error('request artifact directory must be private')
  }
  const filename = meta.sha256 + '.json'
  let fd
  try {
    fd = openSync(join(directory, filename), 'wx', 0o600)
    writeFileSync(fd, serialized)
  } catch (error) {
    if (error.code !== 'EEXIST') throw error
    const existing = join(directory, filename)
    const meta = lstatSync(existing)
    if (!meta.isFile() || meta.isSymbolicLink() || (meta.mode & 0o077) !== 0) {
      throw new Error('unsafe existing request artifact')
    }
    fd = openSync(existing, constants.O_RDONLY | constants.O_NOFOLLOW)
    if (digest(readFileSync(fd)) !== filename.slice(0, -5)) {
      throw new Error('existing request artifact hash mismatch')
    }
  } finally { if (fd !== undefined) closeSync(fd) }
  return { ...meta, saved: true, filename, truncated: false }
}

export function compareSnapshots(before, after) {
  const changed = []
  for (const layer of ['sections', 'contexts', 'registry_catalog', 'catalog', 'tools', 'tool_history', 'settings', 'input']) {
    if (JSON.stringify(comparable(before[layer])) !== JSON.stringify(comparable(after[layer]))) changed.push(layer)
  }
  return { changed, same_revision: before.revision === after.revision,
    same_model: before.provider === after.provider && before.model === after.model,
    before: { revision: before.revision, source_sha256: before.source_sha256, profile: before.profile,
      assembly_observed: before.assembly_observed, catalog_observed: before.catalog_observed },
    after: { revision: after.revision, source_sha256: after.source_sha256, profile: after.profile,
      assembly_observed: after.assembly_observed, catalog_observed: after.catalog_observed } }
}
