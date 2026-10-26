import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';
import { chromium } from 'playwright';

test('celestial dashboard connects real data, reviews exact content, streams denial, submits a preset, and fits mobile themes', { timeout: 90000 }, async () => {
  const root = join(import.meta.dirname, '../../..');
  const temporary = mkdtempSync(join(tmpdir(), 'scopewatch-celestial-'));
  const portServer = createServer();
  await new Promise(resolve => portServer.listen(0, '127.0.0.1', resolve));
  const port = portServer.address().port;
  await new Promise(resolve => portServer.close(resolve));
  let logs = '';
  const backend = spawn('bash', ['scripts/run_demo.sh', '--db-path', join(temporary, 'test.db'), '--port', String(port)], {
    cwd: root, env: { ...process.env, SCOPEWATCH_APPROVAL_TTL: '3600', SCOPEWATCH_AGENT_PROFILE: 'mock', SCOPEWATCH_AUDITOR_PROFILE: 'mock', SCOPEWATCH_RUN_WORKSPACES_DIR: join(temporary, 'runs') }, stdio: ['ignore','pipe','pipe'],
  });
  backend.stdout.on('data', data => { logs += data; });
  backend.stderr.on('data', data => { logs += data; });
  const origin = `http://127.0.0.1:${port}`;
  let browser;
  try {
    let healthy = false;
    for (let i=0; i<300; i++) {
      try { if ((await fetch(`${origin}/api/v1/health`)).ok) { healthy=true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(healthy, logs);
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width:1440, height:1080 } });
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(`${origin}/?live=1`);
    await page.waitForFunction(() => document.getElementById('home-pending')?.textContent === '1');
    assert.equal(await page.locator('#run-buttons button').count(), 6);
    assert.equal(await page.locator('#home-executed').textContent(), '2');
    if (process.env.SCOPEWATCH_SCREENSHOT_DIR) {
      mkdirSync(process.env.SCOPEWATCH_SCREENSHOT_DIR, {recursive:true});
      await page.screenshot({path: join(process.env.SCOPEWATCH_SCREENSHOT_DIR, 'scopewatch-celestial-home.png'), fullPage:true});
    }
    await page.locator('.rail-nav [data-view-target="requests"]').click();
    const proposed = await page.locator('.approval-full-arguments').textContent();
    assert.match(proposed, /content/);
    assert.match(proposed, /payment|bank|account/i);
    await page.locator('.btn-deny').click();
    assert.match(await page.locator('.btn-deny').textContent(), /Confirm deny/);
    await page.locator('.btn-deny').click();
    await page.waitForFunction(() => document.getElementById('pending-approvals-count')?.textContent === '0');
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator('#timeline .timeline-event').filter({ hasText: 'Denied approval for action write_text' }).waitFor();
    const runs = await (await fetch(`${origin}/api/v1/runs`)).json();
    const injection = runs.find(run => run.name === 'Reasoning-Injection Escalation');
    const events = await (await fetch(`${origin}/api/v1/runs/${injection.id}/events`)).json();
    const denied = events.find(event => event.event_type === 'APPROVAL_DENIED');
    assert.ok(denied);
    assert.equal(events.some(event => event.action_request_id === denied.action_request_id && event.event_type === 'EXECUTION_SUCCEEDED'), false);
    await page.getByRole('button', {name: 'Approved Invoice Audit', exact:true}).click();
    await page.locator('.rail-nav [data-view-target="test"]').click();
    await page.locator('[data-preset="safe_read"]').click();
    assert.equal(await page.locator('#action-resource').inputValue(), 'invoices/approved/vendor-a.txt');
    await page.locator('#submit-action-btn').click();
    await page.waitForFunction(() => document.getElementById('action-status-msg')?.textContent.includes('ALLOW'));
    for (const width of [320,390,768,1440]) {
      await page.setViewportSize({width,height:1000});
      for (const theme of ['moonlit','sunbeam']) {
        await page.locator(`[data-theme-target="${theme}"]`).click();
        assert.equal(await page.locator('body').getAttribute('data-theme'), theme);
        for (const view of ['home','activity','requests','test','badges']) {
          await page.locator(`.rail-nav [data-view-target="${view}"]`).click();
          const size = await page.evaluate(() => ({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}));
          assert.ok(size.scroll<=size.width+1, `${view}/${theme} overflows at ${width}: ${JSON.stringify(size)}`);
        }
      }
    }
    assert.deepEqual(pageErrors, []);
  } finally {
    if (browser) await browser.close();
    if (backend.exitCode === null && backend.signalCode === null) {
      const exited = new Promise(resolve => backend.once('exit', resolve));
      backend.kill('SIGTERM');
      const forceKill = setTimeout(() => backend.kill('SIGKILL'), 2000);
      await exited;
      clearTimeout(forceKill);
    }
    rmSync(temporary, {recursive:true,force:true});
  }
});
