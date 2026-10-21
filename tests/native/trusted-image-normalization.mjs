import assert from 'node:assert/strict'
import { chmodSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import sharp from '../../.runtime/dsh-runtime/node_modules/sharp/dist/index.mjs'
import { Context } from '../../dsh/node_modules/@deepseek-ai/cordis/lib/index.js'
import LocalAttachmentStore from '../../dsh/node_modules/@deepseek-ai/dsh-attachment-local/lib/index.js'

const root = mkdtempSync(join(tmpdir(), 'v2-image-normalization-'))
chmodSync(root, 0o700)
try {
  const ctx = new Context()
  await ctx.plugin(LocalAttachmentStore, { dshHome: root })
  const source = await sharp({ create: { width: 3200, height: 2100, channels: 3, background: '#489abc' } })
    .png().toBuffer()
  const first = await ctx.attachments.saveImages([{ data: source, mediaType: 'image/png' }])
  const second = await ctx.attachments.saveImages([{ data: source, mediaType: 'image/png' }])
  assert.equal(first.length, 1)
  assert.equal(first[0].width * first[0].height <= 2048 * 2048, true)
  assert.deepEqual(first, second, 'revalidating the same raw proof must reproduce the same ordered image ref')
  assert.equal(first[0].originalDimensions.width, 3200)
  assert.equal(first[0].originalDimensions.height, 2100)
  await ctx.fiber.dispose()
  process.stdout.write(JSON.stringify({ sourceBytes: source.length, normalized: first[0] }) + '\n')
} finally {
  rmSync(root, { recursive: true, force: true })
}
