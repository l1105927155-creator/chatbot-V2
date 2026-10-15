// Optional browser acceptance. Requires playwright-core and an installed Chromium.
// Run against a sandbox started with --no-dsh, never against a real QQ instance.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(process.env.SANDBOX_PLAYWRIGHT_MODULE || 'playwright-core');

async function main() {
  const base = process.argv[2] || 'http://127.0.0.1:6221';
  assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname));
  const output = path.resolve(process.env.SANDBOX_UI_OUTPUT || '.runtime/sandbox-browser');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const checks = [], errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', e => errors.push(e.message));
  async function state() { return (await page.request.get(base + '/api/state')).json(); }
  async function until(fn, label) {
    for (let i = 0; i < 100; i++) {
      if (await fn()) return;
      await page.waitForTimeout(150);
    }
    throw new Error('timed out: ' + label);
  }
  async function send(text) {
    const count = (await state()).events.length;
    await page.locator('#message-input').fill(text);
    const delivered = page.waitForResponse(r => r.url() === base + '/api/send' && r.request().method() === 'POST');
    await page.locator('#message-input').press('Enter');
    const response = await delivered;
    assert.equal(response.status(), 202, await response.text());
    await until(async () => (await state()).events.length > count, 'UI event delivered');
    await page.waitForTimeout(1600);
  }
  try {
    await until(async () => {
      try { return (await state()).connected; } catch { return false; }
    }, 'OneBot connection');
    const initial = await state();
    assert.equal(initial.mode, 'disabled', 'browser check requires --no-dsh');
    await page.goto(base);
    await page.waitForSelector('.conversation-item');
    await page.locator('.conversation-item').filter({ hasText: '91000' }).click();
    await page.locator('#sender-select').selectOption('90001');
    await send('Alice browser fixture');
    await page.locator('#sender-select').selectOption('90002');
    await send('Bob browser fixture');
    const events = (await state()).events;
    assert.equal(events.at(-1).sender_id, '90002');
    assert.equal(events.at(-2).sender_id, '90001');
    checks.push('member switching produces real OneBot sender identities');
    page.once('dialog', d => d.accept('1'));
    await page.locator('#at-button').click();
    await send((await page.locator('#message-input').inputValue()) + 'browser mention fixture');
    assert((await state()).events.at(-1).event.message.some(s => s.type === 'at' && String(s.data.qq) === '99000'));
    checks.push('Bot mention button emits an actual OneBot at segment');

    await send('/v2probe');
    await until(async () => (await page.locator('.from-bot .bubble').allTextContents()).some(t => t.includes('v2 phase 1 probe: ok')), 'actual Bot reply');
    await page.locator('.from-bot').last().getByRole('button', { name: '引用', exact: true }).click();
    await send('/v2probe');
    const quoted = (await state()).events.filter(e => e.direction === 'inbound').at(-1);
    assert.equal(quoted.event.message.filter(s => s.type === 'reply').length, 1);
    assert.equal(await page.locator('#reply-banner').isVisible(), false);
    await page.locator('.message').filter({ hasText: 'Bob browser fixture' }).first().getByTitle('检查原始 JSON').click();
    assert((await page.locator('#raw-content').innerText()).includes('90002'));
    const beforeReplay = (await state()).events.length;
    await page.locator('#replay-event').click();
    await page.waitForTimeout(500);
    assert.equal((await state()).events.length, beforeReplay);
    await page.locator('#replay-new-message').click();
    await until(async () => (await state()).events.length === beforeReplay + 1, 'new-ID replay');
    await page.locator('#close-inspector').click();
    checks.push('deterministic Bot reply, quoted get_msg, raw inspector and both replay modes');

    for (const kind of ['image', 'file', 'forward']) await send('/sandbox ' + kind);
    await page.waitForSelector('img.image-preview');
    assert(await page.locator('img.image-preview').first().evaluate(i => i.complete && i.naturalWidth > 0));
    const download = page.locator('a.download-card').first();
    const assetResponse = await page.request.get(new URL(await download.getAttribute('href'), base).href);
    assert.equal(assetResponse.status(), 200);
    assert((await assetResponse.text()).includes('沙箱'));
    const forward = page.locator('.from-bot').filter({ has: page.locator('details.forward-node') }).last().locator('.bubble > details.forward-node');
    await forward.locator(':scope > summary').click();
    await until(async () => (await forward.innerText()).includes('第一条转发测试'), 'forward expands');
    await page.waitForTimeout(3200);
    assert(await forward.evaluate(e => e.open), 'polling must preserve expanded forward');
    checks.push('actual image preview, downloadable file and persistent forward expansion');

    // Forward existing image/file messages again, preserving fixture references.
    const forwarded = page.waitForResponse(r => r.url() === base + '/api/send' && r.request().method() === 'POST');
    await page.locator('#forward-button').click();
    const forwardingResponse = await forwarded;
    assert.equal(forwardingResponse.status(), 202, await forwardingResponse.text());
    checks.push('UI-selected history re-forwards media through actual OneBot format');

    const memberId = String(Date.now()).slice(-10);
    const replies = ['<img src=x onerror=window.sandboxXss=1>', memberId];
    const dialog = async d => d.accept(replies.shift());
    page.on('dialog', dialog);
    await page.locator('#add-member').click();
    await until(async () => (await state()).members.some(m => m.id === memberId), 'member created');
    page.off('dialog', dialog);
    assert.equal(await page.evaluate(() => window.sandboxXss), undefined);
    assert.equal(await page.locator('.member-list img').count(), 0);
    checks.push('member creation renders hostile nicknames as text');

    await page.locator('.conversation-item').filter({ hasText: '90002' }).click();
    assert.equal(await page.locator('#reply-banner').isVisible(), false);
    assert.equal(await page.locator('#sender-select').inputValue(), '90002');
    assert(await page.locator('#sender-select').isDisabled());
    await send('/v2probe');
    assert.deepEqual((await state()).events.at(-1).conversation, { type: 'private', id: '90002' });
    checks.push('private conversation locks sender to its real simulated peer');

    await page.locator('.conversation-item').filter({ hasText: '91000' }).click();
    await page.screenshot({ path: path.join(output, 'sandbox-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 768, height: 900 });
    await page.screenshot({ path: path.join(output, 'sandbox-tablet.png'), fullPage: true });
    assert.deepEqual(errors, []);
    const result = { passed: true, checks, page_errors: errors, screenshots: ['sandbox-desktop.png', 'sandbox-tablet.png'] };
    fs.writeFileSync(path.join(output, 'ui-result.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
