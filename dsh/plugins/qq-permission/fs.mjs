import { dirname } from 'node:path'
import SandboxedFileSystem from '@deepseek-ai/dsh-fs-sandbox'
import { FsError } from '@deepseek-ai/dsh-fs'

const denied = (operation, path) => new FsError(
  `cannot ${operation} "${path}": authorization denied`,
  'FS_PERMISSION_DENIED',
)

/**
 * Adds per-operation, per-target QQ authorization to DSH's native filesystem.
 * All filesystem work remains in SandboxedFileSystem so its workspace fence,
 * atomic writes, versions, output values, and cancellation behavior are kept.
 */
export class AuthorizedFileSystem extends SandboxedFileSystem {
  static inject = ['sandboxPolicy', 'v2Authority']

  constructor(ctx, config) {
    super(ctx, config)
    this.authority = ctx.v2Authority
  }

  currentSource(operation, path) {
    const source = this.authority?.current()
    if (!source) throw denied(operation, path)
    return source
  }

  owner(source) {
    return this.authority.isOwner(source)
  }

  async authorizedTarget(target, source, operation) {
    let fresh
    try {
      fresh = await super.resolve(this.processPath(target))
    } catch (error) {
      throw error
    }
    // Retain DSH's presentation path while sending the freshly resolved key to
    // the original provider. A changed key means a link/ancestor changed.
    fresh = { ...fresh, displayPath: target.displayPath }
    const isOwner = this.owner(source)
    if (!isOwner) {
      if (fresh.targetKey !== target.targetKey) {
        throw denied(operation, target.displayPath)
      }
      if (this.authority.reserved(fresh.targetKey)) {
        throw denied(operation, target.displayPath)
      }
      if (!this.authority.allowFile(source, operation, fresh.targetKey)) {
        throw denied(operation, target.displayPath)
      }
    }
    return { target: fresh, owner: isOwner }
  }

  async stat(target, signal) {
    const source = this.currentSource('read', target.displayPath)
    let checked
    try {
      checked = await this.authorizedTarget(target, source, 'read')
    } catch (readError) {
      try {
        checked = await this.authorizedTarget(target, source, 'write')
      } catch {
        throw readError
      }
    }
    const info = await super.stat(checked.target, signal)
    if (!checked.owner && info && info.type !== 'file') {
      throw denied('stat', target.displayPath)
    }
    return info
  }

  async readText(target, signal) {
    const source = this.currentSource('read', target.displayPath)
    const { target: checked } = await this.authorizedTarget(target, source, 'read')
    return super.readText(checked, signal)
  }

  async streamText(target, signal) {
    const source = this.currentSource('read', target.displayPath)
    const self = this
    const nativeStreamText = super.streamText.bind(this)
    // Resolve and authorize when consumption starts, using the captured source
    // rather than a later turn's current identity.
    return (async function* () {
      const { target: checked } = await self.authorizedTarget(target, source, 'read')
      const native = await nativeStreamText(checked, signal)
      yield* native
    }).call(this)
  }

  async readBytes(target, signal, maxBytes) {
    const source = this.currentSource('read', target.displayPath)
    const { target: checked } = await this.authorizedTarget(target, source, 'read')
    return super.readBytes(checked, signal, maxBytes)
  }

  async readByteRange(target, range, signal) {
    const source = this.currentSource('read', target.displayPath)
    const { target: checked } = await this.authorizedTarget(target, source, 'read')
    return super.readByteRange(checked, range, signal)
  }

  async listDir(target, signal) {
    const source = this.currentSource('read', target.displayPath)
    if (!this.owner(source)) throw denied('list', target.displayPath)
    const { target: checked } = await this.authorizedTarget(target, source, 'list')
    return super.listDir(checked, signal)
  }

  async writeText(target, content, expected, signal, sandboxPolicy) {
    const source = this.currentSource('write', target.displayPath)
    const { target: checked, owner } = await this.authorizedTarget(target, source, 'write')
    const existing = await super.stat(checked, signal)
    if (!owner) {
      if (existing && !this.authority.allowFile(source, 'read', checked.targetKey)) {
        throw denied('overwrite', target.displayPath)
      }
      const parentPath = dirname(checked.targetKey)
      const parent = await super.resolve(parentPath)
      const parentInfo = await super.stat(parent, signal)
      if (!parentInfo || parentInfo.type !== 'directory') {
        throw denied('create', target.displayPath)
      }
      if (this.authority.reserved(checked.targetKey)
          || !this.authority.allowFile(source, 'write', checked.targetKey)
          || (existing && !this.authority.allowFile(source, 'read', checked.targetKey))) {
        throw denied('write', target.displayPath)
      }
    }
    return super.writeText(checked, content, expected, signal, sandboxPolicy)
  }

  async editText(target, edit, expected, signal, sandboxPolicy) {
    const source = this.currentSource('edit', target.displayPath)
    const { target: checked, owner } = await this.authorizedTarget(target, source, 'write')
    if (!owner && !this.authority.allowFile(source, 'read', checked.targetKey)) {
      throw denied('edit', target.displayPath)
    }
    return super.editText(checked, edit, expected, signal, sandboxPolicy)
  }
}

export default AuthorizedFileSystem
