// Browser QA for the expanded local sandbox control drawer.
// Run only against the isolated scripted Owner sandbox, never against QQ.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.resolve(process.env.SANDBOX_PLAYWRIGHT_MODULE || '.runtime/sandbox-browser/node_modules/playwright-core'));

async function main() {
  const base = process.argv[2] || 'http://127.0.0.1:6331';
  assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'sandbox must be loopback only');
  const output = path.resolve(process.env.SANDBOX_UI_OUTPUT || '.runtime/sandbox-browser');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const checks = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const request = page.request;
  async function get(route) {
    const response = await request.get(base + route); assert.equal(response.status(), 200, `${route}: ${await response.text()}`); return response.json();
  }
  async function post(route, body) {
    const response = await request.post(base + route, { data: body }); assert([200, 201, 202].includes(response.status()), `${route}: ${await response.text()}`); return response.json();
  }
  async function until(fn, label, timeoutMs = 30000) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) { if (await fn()) return; await page.waitForTimeout(150); }
    throw new Error(`timed out: ${label}`);
  }
  async function waitForSend() {
    const response = await page.waitForResponse(r => r.url() === base + '/api/send' && r.request().method() === 'POST');
    assert.equal(response.status(), 202, await response.text());
  }
  async function submitText(text) {
    await page.locator('#message-input').fill(text);
    const sent = waitForSend(); await page.locator('#message-input').press('Enter'); await sent;
  }
  let originalAdmission;
  try {
    const initial = await get('/api/state');
    assert.equal(initial.mode, 'scripted');
    assert.equal(initial.scenario?.owner_id, '90009');
    originalAdmission = initial.scenario.admitted;
    await page.goto(base); await page.waitForSelector('.conversation-item');
    await page.locator('#scenario-toggle').click();
    await page.waitForFunction(() => !document.querySelector('#scenario-owner').textContent.includes('读取'));
    assert((await page.locator('#scenario-owner').innerText()).includes('90009'));
    assert.equal(await page.locator('#scenario-mode').innerText(), '脚本响应');
    checks.push('drawer shows immutable startup Owner and scripted mode');

    const removeKey = originalAdmission.find(key => key !== 'private:90009');
    const checkbox = page.locator(`#admission-list input[value="${removeKey}"]`);
    await checkbox.uncheck();
    await page.locator('#save-admission').click();
    await until(async () => !(await get('/api/scenario')).admitted.includes(removeKey), 'admission CAS update');
    const revisionAfterRemoval = (await get('/api/scenario')).revision;
    assert(revisionAfterRemoval > initial.scenario.revision);
    await page.locator(`#admission-list input[value="${removeKey}"]`).check();
    await page.locator('#save-admission').click();
    await until(async () => (await get('/api/scenario')).admitted.includes(removeKey), 'admission restore');
    assert.deepEqual((await get('/api/scenario')).admitted, originalAdmission);
    checks.push('admission save uses revision CAS and restores the original allowlist');

    await page.locator('#fault-target').selectOption('model');
    await page.locator('#fault-kind').selectOption('error');
    await page.locator('#fault-phase').selectOption('before');
    const faultPost = page.waitForResponse(r => r.url() === base + '/api/faults' && r.request().method() === 'POST');
    await page.locator('#precise-fault-form button[type="submit"]').click();
    const armedResponse = await faultPost; assert.equal(armedResponse.status(), 201, await armedResponse.text());
    const armed = await get('/api/faults');
    const rule = armed.faults.find(item => item.status === 'armed'); assert(rule, 'UI should create an armed rule');
    const cancelButton = page.locator(`.rule-row[data-fault-id="${rule.id}"]`).getByRole('button', { name: '移除' });
    await cancelButton.click();
    await until(async () => (await get('/api/faults')).faults.some(item => item.id === rule.id && item.status === 'cancelled'), 'fault cancellation');
    checks.push('fault rule can be armed and cancelled from the drawer');

    await page.locator('.conversation-item').filter({ hasText: '90009' }).click();
    await page.locator('#evidence-conversation').selectOption('private:90009');
    await page.locator('#refresh-evidence').click();
    await until(async () => (await page.locator('#evidence-content').innerText()).includes('Canonical Journal'), 'evidence loaded');
    const evidence = await get('/api/evidence?conversation_key=private%3A90009');
    assert.equal(evidence.atomic_snapshot, false, 'evidence correctly discloses source snapshot is not atomic');
    assert(Array.isArray(evidence.journal) && Array.isArray(evidence.states) && Array.isArray(evidence.audit));
    const downloadPromise = page.waitForEvent('download');
    await page.locator('#export-evidence').click();
    const download = await downloadPromise;
    const exportedPath = path.join(output, 'sandbox-expanded-evidence.json');
    assert.equal(await download.failure(), null);
    const exportResponse = await request.get(base + '/api/evidence/export?conversation_key=private%3A90009');
    assert.equal(exportResponse.status(), 200);
    assert.match(exportResponse.headers()['content-disposition'] || '', /attachment/);
    const exportBody = await exportResponse.text(); fs.writeFileSync(exportedPath, exportBody);
    const exported = JSON.parse(exportBody);
    assert.equal(exported.atomic_snapshot, false);
    checks.push('evidence filters by conversation and exports the read-only source snapshot');

    let messageCount = (await get('/api/state')).events.length;
    await submitText('/v2agent persona developer');
    await until(async () => (await get('/api/state')).events.length > messageCount, 'Owner persona command delivered');
    await page.waitForTimeout(700);
    messageCount = (await get('/api/state')).events.length;
    await submitText('/v2agent scene development');
    await until(async () => (await get('/api/state')).events.length > messageCount, 'Owner scene command delivered');
    await page.waitForTimeout(700);

    // Script one real Bash tool call through the configured DSH provider. The
    // command waits before writing its marker so cancellation can be observed.
    const ownerKey = 'private:90009';
    const workspace = '.codex-ui-cancel-probe';
    const script = await post('/api/scripts', { conversation_key: ownerKey, kind: 'tool', name: 'bash',
      arguments: { command: `sleep 25; printf finished > ${workspace}`, description: 'Wait for UI cancellation', workdir: '.' },
      text_after: 'The cancellation test finished.' });
    assert.equal(script.status, 'armed');
    await page.locator('#wait-event').selectOption('tool/start');
    const toolWait = page.waitForResponse(r => r.url() === base + '/api/wait' && r.request().method() === 'POST');
    await page.locator('#event-wait-form button[type="submit"]').click();
    // The API request remains pending until the actual Bash tool starts.
    await page.waitForTimeout(100);
    await submitText('Please perform the prepared test command now.');
    const toolWaitResponse = await toolWait;
    const started = await toolWaitResponse.json();
    assert.equal(started.matched, true, JSON.stringify(started));
    assert.equal(started.event.event, 'tool/start');
    assert.equal(started.event.tool, 'bash');
    checks.push('event wait returns the actual scripted Owner Bash tool/start event');

    await page.locator('#wait-event').selectOption('dsh/stop:cancelled');
    const cancelWait = page.waitForResponse(r => r.url() === base + '/api/wait' && r.request().method() === 'POST');
    await page.locator('#event-wait-form button[type="submit"]').click();
    await page.waitForTimeout(100);
    const cancelSend = waitForSend(); await page.locator('#cancel-turn').click(); await cancelSend;
    const cancelWaitResponse = await cancelWait;
    const cancelled = await cancelWaitResponse.json();
    assert.equal(cancelled.matched, true, JSON.stringify(cancelled));
    assert.equal(cancelled.event.event, 'dsh/stop');
    assert.equal(cancelled.event.stop_reason, 'cancelled');
    checks.push('Owner cancel is an ordinary OneBot private message and wait observes confirmed cancellation');

    await page.locator('#evidence-conversation').selectOption(ownerKey);
    await page.locator('#refresh-evidence').click();
    await until(async () => await page.locator('#pending-select').count() === 1, 'pending row displayed');
    const pendingOption = await page.locator('#pending-select').locator('option:checked').evaluate(option => ({
      value: option.value, key: option.dataset.conversationKey, domain: option.dataset.domain,
    }));
    assert.equal(pendingOption.key, ownerKey); assert.equal(pendingOption.domain, 'owner');
    const recoverSend = waitForSend(); await page.locator('#recover-pending').click(); await recoverSend;
    await until(async () => {
      const snapshot = await get('/api/evidence?conversation_key=private%3A90009');
      return snapshot.states.some(item => item.domain === 'owner' && item.pending_upper_cursor == null);
    }, 'pending recovered');
    assert.equal((await get('/api/evidence?conversation_key=private%3A90009')).states.find(item => item.domain === 'owner')?.pending_upper_cursor, null);
    checks.push('pending picker binds to the exact Owner private state and recovery clears it');

    await page.screenshot({ path: path.join(output, 'sandbox-expanded-controls-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 768, height: 900 });
    await page.screenshot({ path: path.join(output, 'sandbox-expanded-controls-tablet.png'), fullPage: true });
    assert.deepEqual(errors, []);
    const result = { passed: true, base, checks, page_errors: errors,
      screenshots: ['sandbox-expanded-controls-desktop.png', 'sandbox-expanded-controls-tablet.png'],
      exported_evidence: path.basename(exportedPath), scripted_bash_status: cancelled.event.stop_reason,
      pending_recovered: pendingOption.value };
    fs.writeFileSync(path.join(output, 'sandbox-expanded-ui-result.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
  } finally {
    // Restore only the admission list this QA changed. Do not stop the user's sandbox.
    if (originalAdmission) {
      try { await request.put(base + '/api/scenario', { data: { revision: (await get('/api/scenario')).revision, admitted: originalAdmission } }); } catch {}
    }
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
