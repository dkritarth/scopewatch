#!/usr/bin/env node
// Offline media capture. Uses the frontend's pinned Playwright dependency.
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { createServer } from 'node:net';
import { mkdtemp, mkdir, readFile, writeFile, rm, copyFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join, relative } from 'node:path';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

let stage = 'runtime provenance';
async function main() {
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const { values } = parseArgs({ options: {
    ref: { type: 'string', default: 'origin/main' },
    output: { type: 'string', default: 'docs/submission/media' },
    help: { type: 'boolean', default: false },
  } });
  if (values.help) {
    console.log('Usage: node scripts/media/capture.mjs [--ref origin/main] [--output docs/submission/media]');
    process.exit(0);
  }
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  const commit = git('rev-parse', '--verify', `${values.ref}^{commit}`);
  // Capture only the runtime tree of the named commit, never quietly label local edits as main.
  const runtime = ['backend', 'frontend', 'demo', 'scripts/run_demo.sh', 'scripts/seed_demo.py'];
  assert.ok(git('diff', commit, '--', ...runtime) === '', 'Runtime differs from --ref; commit or restore runtime edits before capture.');
  assert.ok(git('ls-files', '--others', '--exclude-standard', '--', ...runtime) === '', 'Untracked runtime files would invalidate capture provenance.');
  stage = 'local browser dependency';
  const require = createRequire(join(root, 'frontend/package.json'));
  const { chromium } = require('playwright');
  const output = resolve(root, values.output);
  await mkdir(output, { recursive: true });
  const scratch = await mkdtemp(join(tmpdir(), 'scopewatch-media-'));
  const portServer = createServer();
  await new Promise(resolve => portServer.listen(0, '127.0.0.1', resolve));
  const port = portServer.address().port;
  await new Promise(resolve => portServer.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  // Do not inherit provider keys, proxy settings, remote executors, or caller configuration.
  const env = Object.fromEntries(['PATH', 'HOME', 'LANG', 'VIRTUAL_ENV'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
  Object.assign(env, {
    HOST: '127.0.0.1', PYTHONUNBUFFERED: '1',
    SCOPEWATCH_AGENT_PROFILE: 'mock', SCOPEWATCH_AUDITOR_PROFILE: 'mock',
    SCOPEWATCH_EXECUTOR: 'local', SCOPEWATCH_APPROVAL_TTL: '3600',
    SCOPEWATCH_RUN_WORKSPACES_DIR: join(scratch, 'runs'),
  });
  const backend = spawn('bash', ['scripts/run_demo.sh', '--mode', 'scripted', '--profile', 'mock',
    '--db-path', join(scratch, 'demo.db'), '--workspace-root', join(scratch, 'workspace'), '--port', String(port)],
  { cwd: root, env, stdio: 'ignore' });
  const exited = new Promise(resolve => backend.once('exit', resolve));
  let browser;
  stage = 'mock gateway startup';
  try {
    let healthy = false;
    for (let attempt = 0; attempt < 600; attempt++) {
      if (backend.exitCode !== null || backend.signalCode !== null) throw new Error('Local mock launcher exited before readiness.');
      try {
        if ((await fetch(`${origin}/api/v1/health`, { signal: AbortSignal.timeout(500) })).ok) { healthy = true; break; }
      } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(healthy, 'Local mock gateway did not become ready.');
    stage = 'browser launch';
    browser = await chromium.launch({ headless: true });
    const viewport = { width: 1440, height: 1080 };
    const page = await browser.newPage({ viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce' });
    const errors = [];
    const failedResponses = [];
    page.on('response', response => { if (response.status() >= 400) failedResponses.push(`${response.status()} ${new URL(response.url()).pathname}`); });
    const externalRequests = [];
    const consoleErrors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin !== origin) {
        externalRequests.push('Blocked non-local request');
        return route.abort();
      }
      return route.continue();
    });
    stage = 'scripted scenario checks';
    const runs = await (await fetch(`${origin}/api/v1/runs`)).json();
    assert.equal(runs.length, 6);
    const injection = runs.find(run => run.name === 'Reasoning-Injection Escalation');
    assert.ok(injection);
    const events = await (await fetch(`${origin}/api/v1/runs/${injection.id}/events`)).json();
    const hold = events.find(event => event.event_type === 'POLICY_HELD' && event.details.reason_code === 'REASONING_SCOPE_CONCERN');
    assert.ok(hold, 'Scripted injection must reach a reasoning hold.');
    const detail = await (await fetch(`${origin}/api/v1/runs/${injection.id}/actions/${hold.action_request_id}`)).json();
    assert.equal(detail.action_request.reasoning_provenance, 'SYNTHETIC_FIXTURE');
    assert.equal(detail.execution_receipt, null);
    assert.equal(detail.policy_decision.outcome, 'HOLD');
    assert.equal(events.some(event => event.action_request_id === hold.action_request_id && event.event_type === 'EXECUTION_SUCCEEDED'), false);
    await page.goto(`${origin}/?live=1`);
    await page.waitForFunction(() => document.getElementById('home-pending')?.textContent === '1');
    await page.getByRole('button', { name: injection.name, exact: true }).click();
    await page.getByRole('button', { name: 'Moonlit theme', exact: true }).click();
    const assets = [];
    async function capture(file, caption, alt) {
      stage = file;
      assert.doesNotMatch(await page.locator('body').innerText(), /\/home\/|\/Users\/|sk-or-|ghp_|Bearer\s/i, 'Sensitive text invalidates capture.');
      await page.evaluate(() => document.fonts.ready);
      const raw = await page.screenshot({ fullPage: true, animations: 'disabled' });
      // Put the caption outside the unchanged UI so detached PNGs retain their provenance.
      const framed = await browser.newPage({ viewport, deviceScaleFactor: 1 });
      let png;
      try {
        await framed.setContent('<!doctype html><html lang="en"><meta charset="utf-8"><title>Captioned capture</title><style>body{margin:0;color:#16293d;background:#fff;font:18px/1.5 system-ui}figure{margin:0}figcaption{padding:16px 24px;border-bottom:2px solid #49677d}img{display:block;width:1440px}</style><figure><figcaption></figcaption><img></figure></html>');
        await framed.locator('figcaption').evaluate((element, text) => { element.textContent = text; }, `${caption} Captured runtime commit: ${commit}.`);
        await framed.locator('img').evaluate((element, data) => { element.src = data; }, `data:image/png;base64,${raw.toString('base64')}`);
        await framed.locator('img').evaluate(image => image.decode());
        png = await framed.screenshot({ path: join(scratch, file), fullPage: true, animations: 'disabled' });
      } finally { await framed.close(); }
      assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
      const width = png.readUInt32BE(16), height = png.readUInt32BE(20);
      assert.equal(width, viewport.width);
      assert.ok(height >= viewport.height);
      assets.push({ file, caption, alt, width, height, sha256: createHash('sha256').update(png).digest('hex') });
    }
    await capture('scripted-home-moonlit.png',
      'SCRIPTED MOCK: celestial dashboard home for the seeded invoice-injection run. Local gateway data, no live model inference.',
      'Moonlit dashboard home with executed and denied counts and a pending review request.');
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator(`#timeline [data-event-id="${hold.id}"]`).click();
    await page.locator('.provenance-badge').filter({ hasText: 'Synthetic fixture' }).waitFor();
    await page.locator('.reasoning-trace').waitFor();
    assert.match(await page.locator('#event-evidence').innerText(), /REASONING_SCOPE_CONCERN|CONCERN/);
    await page.getByRole('button', { name: 'Sunbeam theme', exact: true }).click();
    await capture('scripted-hold-sunbeam.png',
      'SCRIPTED MOCK: the seeded action is held for a reasoning concern. The trace is a synthetic fixture, not provider-exposed reasoning. No execution receipt exists for this held action.',
      'Sunbeam activity timeline with the selected reasoning hold and synthetic fixture evidence.');
    await page.locator('.rail-nav [data-view-target="requests"]').click();
    await page.locator('.approval-full-arguments').waitFor();
    assert.deepEqual(JSON.parse(await page.locator('.approval-full-arguments').innerText()), detail.action_request.arguments);
    await page.getByRole('button', { name: 'Moonlit theme', exact: true }).click();
    await capture('scripted-request-moonlit.png',
      'SCRIPTED MOCK: pending review shows the exact proposed synthetic write and approval controls. This capture does not approve or execute the action.',
      'Moonlit request page with proposed write content and approve and deny controls.');
    const knownFontFailures = new Set(['404 /fonts/dm-sans.woff2', '404 /fonts/instrument-serif.woff2', '404 /fonts/instrument-serif-italic.woff2']);
    assert.deepEqual(failedResponses.filter(item => !knownFontFailures.has(item)), [], 'Unexpected HTTP failures invalidate captures.');
    assert.equal(consoleErrors.length, failedResponses.length, 'Unexpected console errors invalidate captures.');
    assert.ok(consoleErrors.every(message => message === 'Failed to load resource: the server responded with a status of 404 (Not Found)'));
    assert.deepEqual(errors, [], 'Browser errors invalidate the captures.');
    assert.deepEqual(externalRequests, [], 'Unexpected external requests invalidate offline capture.');
    const architecture = await readFile(join(root, 'docs/submission/media/architecture.html'), 'utf8');
    await page.setContent(architecture);
    await page.locator('figure').waitFor();
    await capture('architecture.png', 'ARCHITECTURE: authored diagram of gateway-mediated execution. This is a design illustration, not a runtime or sponsor-use capture.', 'Policy runs first; final deny stops; allowed actions may be held by audit; approved holds pass an exact single-use gate before execution. Decisions are stored before effects.');
    const manifest = {
      schema_version: 1, captured_at_utc: new Date().toISOString(), captured_commit: commit,
      frontend_tree: git('rev-parse', `${commit}:frontend`),
      capture_script_sha256: createHash('sha256').update(await readFile(fileURLToPath(import.meta.url))).digest('hex'),
      architecture_source_sha256: createHash('sha256').update(architecture).digest('hex'),
      mode: 'scripted', agent_profile: 'mock', auditor_profile: 'mock', executor: 'local',
      provider_calls: 0, reasoning_provenance: 'SYNTHETIC_FIXTURE',
      warnings: failedResponses.map(item => `${item}; gateway used fallback fonts`),
      browser: `Chromium ${browser.version()}`, playwright: require('playwright/package.json').version,
      viewport, device_scale_factor: 1, locale: 'en-US', timezone: 'UTC',
      checks: ['six seeded runs', 'reasoning hold present', 'synthetic trace provenance', 'held action has no successful execution', 'request content matches stored arguments exactly', 'no JavaScript page errors', 'only disclosed font HTTP failures', 'no external browser requests', 'PNG signatures and dimensions'],
      assets,
    };
    stage = 'publish verified files';
    for (const asset of assets) await copyFile(join(scratch, asset.file), join(output, asset.file));
    await writeFile(join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
    console.log(`Captured ${assets.length} PNGs from ${commit}; scripted/mock only. Manifest: ${relative(root, join(output, 'manifest.json'))}`);
  } finally {
    if (browser) await browser.close();
    if (backend.exitCode === null && backend.signalCode === null) {
      backend.kill('SIGTERM');
      const force = setTimeout(() => backend.kill('SIGKILL'), 3000);
      await exited;
      clearTimeout(force);
    }
    await rm(scratch, { recursive: true, force: true });
  }

}
main().catch(() => { console.error(`Capture failed during ${stage}. No new manifest was written. Check local dependencies and runtime state.`); process.exitCode = 1; });
