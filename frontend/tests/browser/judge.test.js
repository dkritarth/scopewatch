import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, mkdirSync, rmSync, existsSync, readdirSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright';

const reviewer = 'synthetic-judge-reviewer';
const agent = 'synthetic-judge-agent';

async function gateway(t, ttl = 300) {
  const root = join(import.meta.dirname, '../../..');
  const dir = mkdtempSync(join(tmpdir(), 'scopewatch-judge-'));
  mkdirSync(join(dir, 'workspace'));
  const socket = createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const script = `
from pathlib import Path
import uvicorn
from scopewatch.app import create_app
from scopewatch.db import init_db
p = Path(${JSON.stringify(dir)})
init_db(p / 'test.db')
uvicorn.run(create_app(db_path=p / 'test.db', workspace_root=p / 'workspace'), host='127.0.0.1', port=${port}, log_level='warning')
`;
  const proc = spawn('python3', ['-c', script], { stdio: ['ignore', 'pipe', 'pipe'], env: {
    ...process.env, PYTHONPATH: join(root, 'backend'), DEMO_TOKEN: agent,
    SCOPEWATCH_API_KEY: '', SCOPEWATCH_REVIEWER_TOKEN: reviewer,
    SCOPEWATCH_ALLOW_RESET: 'true', SCOPEWATCH_AUDITOR_PROFILE: 'mock',
    SCOPEWATCH_REASONING_AUDIT: 'true', SCOPEWATCH_APPROVAL_TTL: String(ttl),
    SCOPEWATCH_RUN_WORKSPACES_DIR: join(dir, 'runs'),
  } });
  let output = '';
  proc.stderr.on('data', chunk => { output += chunk; });
  t.after(async () => {
    proc.kill('SIGTERM');
    await once(proc, 'exit');
    rmSync(dir, { recursive: true, force: true });
  });
  const origin = `http://127.0.0.1:${port}`;
  let healthy = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { healthy = (await fetch(`${origin}/api/v1/health`)).ok; } catch { /* process binding */ }
    if (healthy) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(healthy, output);
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin);
  await page.locator('#demo-reseed').waitFor({ state: 'visible' });
  return { origin, page, errors, dir };
}

function workspaceFiles(root) {
  assert.ok(existsSync(root), 'inspect the actual existing run workspace');
  return Object.fromEntries(readdirSync(root, { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile()).map(entry => {
      const path = join(entry.parentPath, entry.name);
      return [path.slice(root.length + 1), createHash('sha256').update(readFileSync(path)).digest('hex')];
    }));
}

async function login(page, token = reviewer) {
  await page.locator('#reviewer-token-input').fill(token);
  await page.locator('#reviewer-token-save').click();
}

async function reseed(page) {
  await page.locator('#demo-reseed').click();
  await page.waitForFunction(() => document.querySelector('#demo-controls-status').textContent.includes('reseeded'));
}

test('stranger on arbitrary gateway port authenticates, reviews 06, denies and recovers safely', { timeout: 30000 }, async t => {
  const { origin, page, errors, dir } = await gateway(t);
  assert.match(await page.locator('#gateway-status').innerText(), /Live gateway connected/);
  assert.equal(await page.locator('#run-buttons button').count(), 0, 'opening empty gateway never creates a run');
  await page.locator('#demo-reseed').click();
  await page.waitForFunction(() => document.querySelector('#demo-controls-status').textContent.includes('refused'));
  await login(page, agent);
  await page.locator('#demo-reseed').click();
  await page.waitForFunction(() => document.querySelector('#demo-controls-status').textContent.includes('agent credentials'));
  await login(page);
  await reseed(page);
  assert.match(await page.locator('#run-buttons').innerText(), /06 Invoice injection.*synthetic mock replay/);
  await page.locator('.rail-nav [data-view-target="activity"]').click();
  assert.match(await page.locator('#event-evidence').innerText(), /HOLD|held|pending/i);
  assert.match(await page.locator('#event-evidence').innerText(), /Synthetic fixture/);
  await page.locator('.rail-nav [data-view-target="requests"]').click();
  const card = page.locator('.approval-card');
  await card.waitFor();
  assert.match(await card.innerText(), /partner-payment-details/);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  await page.screenshot({ path: '/tmp/290-judge-review.png', fullPage: true });
  assert.match(await card.locator('.approval-card-expiry').innerText(), /\d{4}-\d\d-\d\dT.*Z.*seconds remaining/);
  page.on('dialog', dialog => dialog.accept());
  await page.locator('#demo-reset').click();
  await page.waitForFunction(() => document.querySelector('#demo-controls-status').textContent.includes('Resolve pending'));
  assert.equal(await card.count(), 1);
  const storedRuns = await (await fetch(`${origin}/api/v1/runs`)).json();
  const runWorkspace = join(dir, 'runs', storedRuns[0].id);
  const before = workspaceFiles(runWorkspace);
  const storedApprovals = await (await fetch(`${origin}/api/v1/approvals`)).json();
  await card.locator('.btn-deny').click();
  const denialResponse = page.waitForResponse(response => response.url().endsWith(`/approvals/${storedApprovals[0].id}/deny`));
  await card.locator('.btn-deny').click();
  const denied = await (await denialResponse).json();
  assert.equal(denied.execution_receipt.status, 'NOT_EXECUTED');
  assert.equal(denied.execution_receipt.action_request_id, storedApprovals[0].action_request_id);
  assert.equal(denied.execution_receipt.error_code, 'APPROVAL_DENIED');
  assert.deepEqual(workspaceFiles(runWorkspace), before);
  await page.locator('#review-receipt').waitFor({ state: 'visible' });
  assert.match(await page.locator('#review-receipt').innerText(), /DENIED.*not executed/);
  const runs = await (await fetch(`${origin}/api/v1/runs`)).json();
  const events = await (await fetch(`${origin}/api/v1/runs/${runs[0].id}/events`)).json();
  assert.ok(events.some(event => event.event_type === 'APPROVAL_DENIED'));
  assert.ok(!events.some(event => event.event_type.startsWith('EXECUTION_')));
  await page.locator('#demo-reset').click();
  await page.waitForFunction(() => document.querySelector('#demo-controls-status').textContent.includes('evidence reset'));
  assert.equal(await page.locator('#run-buttons button').count(), 0);
  await reseed(page);
  await page.reload();
  assert.match(await page.locator('#reviewer-access-state').innerText(), /No token set/);
  await page.goto(`${origin}/?live=0`);
  assert.match(await page.locator('#gateway-status').innerText(), /Static replay mode/);
  assert.ok(await page.locator('#run-buttons button').count() > 1);
  assert.equal(await page.locator('#demo-controls').isHidden(), true);
  assert.deepEqual(errors, []);
});

test('expired review never autoapproves or executes and can be reset', { timeout: 30000 }, async t => {
  const { origin, page } = await gateway(t, 2);
  await login(page);
  await reseed(page);
  await page.locator('.rail-nav [data-view-target="requests"]').click();
  await page.waitForFunction(() => document.querySelector('.approval-card-expiry')?.textContent.includes('Expired'));
  assert.equal(await page.locator('.btn-approve').isDisabled(), true);
  const runs = await (await fetch(`${origin}/api/v1/runs`)).json();
  // Approval GET reconciles expiry, but never resolves it as an approval.
  const pending = await (await fetch(`${origin}/api/v1/approvals?status=PENDING`)).json();
  assert.deepEqual(pending, []);
  const events = await (await fetch(`${origin}/api/v1/runs/${runs[0].id}/events`)).json();
  assert.ok(events.some(event => event.event_type === 'APPROVAL_EXPIRED'));
  assert.ok(!events.some(event => /EXECUTION_|APPROVAL_GRANTED/.test(event.event_type)));
});

test('gateway connection and run failures never substitute replay fixtures', { timeout: 30000 }, async t => {
  const { origin, page } = await gateway(t);
  await page.route('**/api/v1/runs', route => route.fulfill({ status: 503,
    contentType: 'application/json', body: '{"error":"unavailable"}' }));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#gateway-status').textContent.includes('Gateway data unavailable'));
  assert.equal(await page.locator('#run-buttons button').count(), 0);
  assert.equal(await page.locator('#runs-retry').isVisible(), true);
  await page.route('**/api/v1/health', route => route.abort());
  await page.goto(origin);
  await page.locator('#connection-error').waitFor({ state: 'visible' });
  assert.match(await page.locator('#gateway-status').innerText(), /Gateway unavailable/);
  assert.equal(await page.locator('#run-buttons button').count(), 0);
});

test('reset discards a polling response captured before deletion', { timeout: 30000 }, async t => {
  const { origin, page } = await gateway(t);
  await login(page);
  await reseed(page);
  const approvals = await (await fetch(`${origin}/api/v1/approvals`)).json();
  const denied = await fetch(`${origin}/api/v1/approvals/${approvals[0].id}/deny`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Reviewer-Token': reviewer },
    body: '{}',
  });
  assert.equal(denied.status, 200);
  let captured;
  const capturedRead = new Promise(resolve => { captured = resolve; });
  let release;
  const releaseRead = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  let first = true;
  await page.route('**/api/v1/runs', async route => {
    if (!first) return route.continue();
    first = false;
    const response = await route.fetch();
    captured();
    await releaseRead;
    await route.fulfill({ response });
  });
  await page.evaluate(() => {
    window.__scopewatch.refreshRuns().finally(() => { window.__oldPollFinished = true; });
  });
  await capturedRead;
  page.on('dialog', dialog => dialog.accept());
  await page.locator('#demo-reset').click();
  await page.waitForFunction(() => document.querySelector('#demo-controls-status').textContent.includes('evidence reset'));
  assert.equal(await page.locator('#run-buttons button').count(), 0);
  release();
  await page.waitForFunction(() => window.__oldPollFinished);
  assert.deepEqual(await (await fetch(`${origin}/api/v1/runs`)).json(), []);
  assert.equal(await page.locator('#run-buttons button').count(), 0, 'old response must not resurrect deleted runs');
});
