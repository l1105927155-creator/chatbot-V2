import { AsyncLocalStorage } from 'node:async_hooks'
import { dirname, join } from 'node:path'
import { readImageFile } from '@deepseek-ai/dsh-attachment-local'

// Native SessionController first proves the image is reachable in this log.
// Async-local routing keeps concurrent Web and other-group reads independent.
export function installAttachments(ctx) {
  const source = new AsyncLocalStorage()
  const store = ctx.attachments
  const controller = ctx.sessionController
  const read = store.readImage
  const attachment = controller.attachment
  async function readImage(ref, signal) {
    const mounted = source.getStore()
    return mounted
      ? readImageFile(join(dirname(mounted.root), 'attachments/v1'), ref, signal)
      : read.call(this, ref, signal)
  }
  async function readAttachment(request) {
    const mounted = await ctx.sessionPersistence.sourceFor(request.sessionId)
    return mounted
      ? source.run(mounted, () => attachment.call(this, request))
      : attachment.call(this, request)
  }
  store.readImage = readImage
  controller.attachment = readAttachment
  return () => {
    if (store.readImage === readImage) store.readImage = read
    if (controller.attachment === readAttachment) controller.attachment = attachment
  }
}
