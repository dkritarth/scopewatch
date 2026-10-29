#!/usr/bin/env node
// Record the Scopewatch walkthrough video (scripted replay, under 3 minutes).
//
// Follows docs/submission/demo-script.md. Every dashboard shot is the real
// local gateway seeded by ./scripts/run_demo.sh with the offline mock provider,
// so the badge "SCRIPTED REPLAY" is burned into every gateway frame. The
// spoken script is burned in as captions and also written as an .srt file.
//
// Usage:
//   ./scripts/run_demo.sh --db-path /tmp/sw/demo.db --workspace-root /tmp/sw/ws &   # fresh seed: approvals expire after 300 s
//   node scripts/media/record_walkthrough.cjs [--gateway http://127.0.0.1:8000] [--out runtime-data/walkthrough]
//
// Needs Playwright (npm ci --prefix frontend) and ffmpeg on PATH.

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const { chromium } = require(require.resolve('playwright', { paths: [path.join(ROOT, 'frontend'), ROOT] }));

const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : fallback; };
const GATEWAY = opt('--gateway', 'http://127.0.0.1:8000');
const OUT = path.resolve(opt('--out', path.join(ROOT, 'runtime-data', 'walkthrough')));
const W = 1920, H = 1080;
const GATEWAY_ZOOM = '1.25'; // the dashboard is dense; zoom it so it reads on a small player
const REPLAY = 'SCRIPTED REPLAY · synthetic scenarios, no model called';

const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);

// ---------- static site server (the deployed GitHub Pages content) ----------
function serveSite() {
  const SITE = path.join(ROOT, 'site');
  const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json', '.svg': 'image/svg+xml', '.webp': 'image/webp' };
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/\/$/, '/index.html');
    const file = path.join(SITE, path.normalize(rel));
    if (!file.startsWith(SITE + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return res.writeHead(404).end();
    res.writeHead(200, { 'content-type': types[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r(server)));
}

// ---------- overlay: badge, captions, cursor (video production only) ----------
const OVERLAY = () => {
  const install = () => {
    if (document.getElementById('sw-rec')) return;
    const root = document.createElement('div');
    root.id = 'sw-rec';
    root.innerHTML = `<style>
      #sw-rec{position:fixed;inset:0;pointer-events:none;z-index:2147483647;font-family:'IBM Plex Sans',system-ui,sans-serif}
      #sw-badge{position:absolute;top:18px;right:22px;background:#f59e0b;color:#111;font:700 17px/1 'IBM Plex Mono',ui-monospace,monospace;letter-spacing:.04em;padding:10px 14px;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.45);display:none}
      #sw-cap{position:absolute;left:50%;bottom:40px;transform:translateX(-50%);max-width:1500px;background:rgba(9,9,11,.9);color:#fafafa;font-size:31px;line-height:1.38;padding:16px 28px;border-radius:12px;border:1px solid rgba(255,255,255,.14);text-align:center;opacity:0;transition:opacity .25s}
      #sw-cur{position:absolute;left:0;top:0;width:26px;height:26px;transition:transform .7s cubic-bezier(.45,.05,.3,1);display:none}
      #sw-cur svg{filter:drop-shadow(0 2px 3px rgba(0,0,0,.6))}
      #sw-ring{position:absolute;width:44px;height:44px;margin:-22px 0 0 -22px;border:3px solid #f59e0b;border-radius:50%;opacity:0}
      #sw-ring.go{animation:swring .5s ease-out}
      @keyframes swring{from{opacity:1;transform:scale(.4)}to{opacity:0;transform:scale(1.4)}}
    </style><div id="sw-badge"></div><div id="sw-cap"></div><div id="sw-ring"></div>
    <div id="sw-cur"><svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2l15 11-7 1-4 7z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg></div>`;
    document.documentElement.appendChild(root);
  };
  window.__swInstall = install;
  window.__swBadge = (t) => { install(); const b = document.getElementById('sw-badge'); if (!b) return; b.textContent = t || ''; b.style.display = t ? 'block' : 'none'; };
  window.__swCap = (t) => { install(); const c = document.getElementById('sw-cap'); if (!c) return; c.textContent = t || ''; c.style.opacity = t ? '1' : '0'; };
  window.__swCursorAt = (x, y) => { install(); const c = document.getElementById('sw-cur'); if (!c) return; c.style.display = 'block'; c.style.transform = `translate(${x}px,${y}px)`; };
  window.__swRing = (x, y) => { const r = document.getElementById('sw-ring'); r.style.left = x + 'px'; r.style.top = y + 'px'; r.classList.remove('go'); void r.offsetWidth; r.classList.add('go'); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
};

// ---------- title / info cards ----------
const FONTS = `<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">`;
const CARD_CSS = `<style>body{margin:0;width:${W}px;height:${H}px;background:#09090b;color:#fafafa;font-family:'IBM Plex Sans',sans-serif;display:flex;align-items:center;justify-content:center;
 background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);background-size:64px 64px}
 h1{font-family:'Bricolage Grotesque',sans-serif;font-size:120px;letter-spacing:-.04em;margin:0;line-height:.95} .hl{color:#f59e0b}
 .eyebrow{font-family:'IBM Plex Mono',monospace;color:#f59e0b;letter-spacing:.18em;font-size:22px;text-transform:uppercase}
 .muted{color:#a1a1aa} pre{font-family:'IBM Plex Mono',monospace;font-size:23px;line-height:1.5;background:#111114;border:1px solid #27272a;border-radius:14px;padding:24px 30px;margin:0;white-space:pre-wrap}
 .k{color:#60a5fa}.s{color:#e4e4e7}.ok{color:#4ade80}.no{color:#f87171}.inj{background:rgba(248,113,113,.18);color:#fecaca;border-bottom:2px dashed #f87171}
 .wrap{width:1640px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:36px;margin-top:34px} h2{font-family:'Bricolage Grotesque',sans-serif;font-size:58px;margin:12px 0 0;letter-spacing:-.02em}
 .file{font-family:'IBM Plex Mono',monospace;color:#a1a1aa;font-size:20px;margin:0 0 10px}</style>`;
const card = (body) => `<!doctype html><html><head><meta charset="utf-8">${FONTS}${CARD_CSS}</head><body>${body}</body></html>`;

function titleCard() {
  const logo = read('site/assets/logo.svg');
  return card(`<div class="wrap"><div style="display:flex;align-items:center;gap:22px;margin-bottom:46px"><div style="width:76px;height:76px">${logo.replace('width="64" height="64"', 'width="76" height="76"')}</div>
   <span style="font-family:'Bricolage Grotesque';font-size:52px;font-weight:700">Scopewatch</span></div>
   <h1>Check every<br>agent action<br><span class="hl">before it runs.</span></h1>
   <p class="eyebrow" style="margin-top:48px">Nebius × NVIDIA Global AI Hackathon 2026 · pre-execution gateway for AI agents</p></div>`);
}

function scopeCard() {
  const scope = JSON.parse(read('demo/scenarios/01_safe_audit.json')).task_scope || {};
  const list = (a) => (a || []).map((p) => `"${esc(p)}"`).join(', ');
  const toml = read('backend/config/providers.toml');
  const start = toml.indexOf('[profiles.nebius-demo]');
  const block = toml.slice(start).split('\n').filter((l) => /^(\[profiles\.nebius-demo\]|name|model|base_url|api_key_env)\b/.test(l)).slice(0, 5).join('\n');
  return card(`<div class="wrap"><p class="eyebrow">The setup</p><h2>One task, one declared scope.</h2><div class="grid">
   <div><p class="file">demo/scenarios/01_safe_audit.json · task_scope</p><pre><span class="k">"task_description"</span>: <span class="s">"${esc(scope.task_description || '')}"</span>,
<span class="k">"allowed_paths"</span>: <span class="ok">[${list(scope.allowed_paths)}]</span>,
<span class="k">"blocked_paths"</span>: <span class="no">[${list(scope.blocked_paths)}]</span></pre></div>
   <div><p class="file">backend/config/providers.toml · used for live runs</p><pre>${esc(block)}</pre>
   <p class="muted" style="font-size:24px;margin-top:22px">The model comes from a provider profile, never from code. This recording uses the offline <b style="color:#fafafa">mock</b> profile.</p></div></div></div>`);
}

function injectedCard() {
  const text = read('demo/workspace/invoices/approved/vendor-c-injected.txt');
  const marked = esc(text).replace(/(\[AUDITOR SYSTEM NOTE[^\n]*)/, '<span class="inj">$1</span>');
  return card(`<div class="wrap"><p class="eyebrow">The attack · synthetic</p><h2>An approved invoice carries an injected instruction.</h2>
   <p class="file" style="margin-top:30px">demo/workspace/invoices/approved/vendor-c-injected.txt</p><pre style="font-size:24px">${marked}</pre></div>`);
}

function endCard() {
  return card(`<div class="wrap"><p class="eyebrow">Try it</p><h1 style="font-size:96px">Run it in <span class="hl">one command.</span></h1>
   <pre style="margin-top:40px;font-size:26px">git clone https://github.com/dkritarth/scopewatch && cd scopewatch
python3 -m venv .venv && . .venv/bin/activate && pip install --require-hashes -r backend/requirements.lock
./scripts/run_demo.sh      # http://127.0.0.1:8000 · no keys, no Docker, no network</pre>
   <div class="grid" style="font-size:28px"><div><span class="muted">Code</span><br>github.com/dkritarth/scopewatch</div><div><span class="muted">Website and replay</span><br>dkritarth.github.io/scopewatch</div></div>
   <p class="muted" style="font-size:22px;margin-top:34px">All demo data is synthetic. Scripted replay with the offline mock provider; no live model inference in this video.</p></div>`);
}

// ---------- recording ----------
(async () => {
  fs.mkdirSync(path.join(OUT, 'frames'), { recursive: true });
  for (const f of fs.readdirSync(path.join(OUT, 'frames'))) fs.unlinkSync(path.join(OUT, 'frames', f));
  const site = await serveSite();
  const SITE_URL = `http://127.0.0.1:${site.address().port}/`;
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, colorScheme: 'dark', bypassCSP: true }); // CSP bypass is for the caption overlay only
  await ctx.addInitScript(OVERLAY);
  const page = await ctx.newPage();

  // Frame capture through the CDP screencast (lossless-ish JPEG, real timestamps).
  const cdp = await ctx.newCDPSession(page);
  const frames = [];
  let n = 0;
  const t0 = Date.now();
  cdp.on('Page.screencastFrame', async (f) => {
    const file = path.join(OUT, 'frames', `f${String(n++).padStart(6, '0')}.jpg`);
    fs.writeFileSync(file, Buffer.from(f.data, 'base64'));
    frames.push({ file, t: f.metadata.timestamp });
    try { await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }); } catch { /* page navigating */ }
  });
  const startCast = () => cdp.send('Page.startScreencast', { format: 'jpeg', quality: 92, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
  await startCast();

  const subs = [];
  const now = () => (Date.now() - t0) / 1000;
  let capOpen = null;
  const wait = (ms) => page.waitForTimeout(ms);
  const cap = async (text, ms) => {
    if (capOpen) subs.push({ ...capOpen, end: now() });
    capOpen = text ? { text, start: now() } : null;
    state.cap = text || '';
    await page.evaluate((t) => window.__swCap(t), text || '');
    if (ms) await wait(ms);
  };
  const state = { badge: '', cap: '' };
  const badge = (t) => { state.badge = t || ''; return page.evaluate((x) => window.__swBadge(x), state.badge); };
  const restore = async () => {
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(OVERLAY);
    await page.evaluate(([b, c]) => { window.__swBadge(b); window.__swCap(c); }, [state.badge, state.cap]);
  };
  const goto = async (url) => {
    await page.goto(url, { waitUntil: 'networkidle' });
    if (url.startsWith(GATEWAY)) {
      await page.evaluate((z) => { document.body.style.zoom = z; }, GATEWAY_ZOOM);
      await page.click('[data-theme-target="moonlit"]');
      await wait(300);
    }
    await restore();
  };
  const setCard = async (html) => { await page.setContent(html, { waitUntil: 'networkidle' }); await restore(); };
  const click = async (locator) => {
    const el = page.locator(locator).first();
    await el.scrollIntoViewIfNeeded();
    await wait(250);
    const b = await el.boundingBox();
    const x = b.x + Math.min(b.width / 2, 120), y = b.y + b.height / 2;
    await page.evaluate(([px, py]) => window.__swCursorAt(px, py), [x, y]);
    await wait(800);
    await page.evaluate(([px, py]) => window.__swRing(px, py), [x, y]);
    await el.click();
    await wait(400);
  };
  const scrollTo = async (locator, block = 'center') => {
    await page.locator(locator).first().evaluate((e, blk) => e.scrollIntoView({ behavior: 'smooth', block: blk }), block);
    await wait(1100);
  };
  const scrollBy = async (dy) => { await page.evaluate((d) => window.scrollBy({ top: d, behavior: 'smooth' }), dy); await wait(1000); };

  // Beat 1: the problem (title card)
  await setCard(titleCard());
  await cap('AI agents act with your authority: reading files, writing reports, running commands.', 4500);
  await cap('When an agent drifts outside its assignment, nobody sees it until the damage is done. Logs show what ran. Nothing checks permission first.', 6500);

  // Beat 2: what Scopewatch is (website)
  await goto(SITE_URL);
  await cap('Scopewatch is a pre-execution gateway. Every tool call an agent makes goes to it first.', 5000);
  await scrollTo('#how', 'start');
  await cap('Deterministic policy answers allow, hold, or deny before anything runs. An auditor then reads the agent’s reasoning and can only escalate to hold.', 7000);

  // Beat 2: setup and mode line
  await setCard(scopeCard());
  await cap('Demo task: audit approved invoices and write a summary. Allowed: the approved folder and outputs. Blocked: the private folder.', 6500);
  await cap('Live runs use the nebius-demo profile: NVIDIA Nemotron 3.5 Lightning on Nebius Token Factory. This take is a scripted replay. No model is called.', 7000);

  // Beat 3: allowed work proceeds
  await goto(GATEWAY + '/');
  await badge(REPLAY);
  await cap('This is the real reviewer dashboard, connected to a local gateway seeded with six synthetic scenarios.', 4000);
  await click('#run-buttons button:has-text("Approved Invoice Audit")');
  await click('.rail-nav [data-view-target="activity"]');
  await cap('Normal work first. The agent lists the approved folder, reads two vendor invoices, and writes the audit summary.', 3500);
  await scrollTo('.timeline-event.status-executed >> nth=-1');
  await click('.timeline-event.status-executed >> nth=-1');
  await cap('Policy allows each step, the auditor sees no concern, and the receipt shows it executed, with full evidence.', 2500);
  await scrollTo('#evidence-heading', 'start').catch(() => {});
  await scrollBy(520);
  await wait(2500);

  // Beat 4: hard DENY, never executed
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' })); await wait(900);
  await click('#run-buttons button:has-text("Blocked Confidential Directory Access")');
  await cap('Now an attack that policy stops cold: a read of executive salary records in the blocked private folder.', 3000);
  await scrollTo('.timeline-event[class*="blocked"]');
  await click('.timeline-event[class*="blocked"]');
  await cap('Deterministic deny: blocked path, final. No auditor call and no override.', 3500);
  await scrollBy(520);
  await cap('The evidence proves it never ran: a decision event, and no execution receipt after it.', 5000);

  // Beat 5: reasoning-escalated HOLD
  await badge('');
  await setCard(injectedCard());
  await cap('The subtle one. An approved invoice hides an injected instruction to reroute bank details into a partner file.', 6000);
  await goto(GATEWAY + '/');
  await badge(REPLAY);
  await click('#run-buttons button:has-text("Reasoning-Injection Escalation")');
  await click('.rail-nav [data-view-target="activity"]');
  await scrollTo('.timeline-event.hold-concern >> nth=0');
  await click('.timeline-event.hold-concern >> nth=0');
  await cap('Writing to outputs is in scope, so policy alone says allow. But the agent’s reasoning says it will follow the injected note.', 4500);
  await scrollTo('text=5. Reasoning', 'start').catch(() => scrollBy(900));
  await cap('The auditor flags that sentence, quoted word for word from the submitted trace, and escalates to hold. Here the trace is a labelled synthetic fixture.', 6500);
  await click('.rail-nav [data-view-target="requests"]');
  await scrollTo('[data-page="requests"] button:has-text("Deny")');
  await cap('Nothing runs. The reviewer gets a single-use approve or deny for this exact action.', 4000);
  await page.locator('[data-page="requests"] textarea, [data-page="requests"] input[type="text"]').first().fill('Injected instruction in invoice; not part of the task.').catch(() => {});
  await click('[data-page="requests"] button:has-text("Deny")');
  await click('[data-page="requests"] button:has-text("Confirm deny")');
  await cap('I deny it, and the dashboard records the decision, with the excerpt kept as evidence.', 4500);
  await click('.rail-nav [data-view-target="activity"]');
  await scrollTo('.timeline-event >> nth=-1');
  await wait(2500);

  // Beat 6: what it does not claim
  await scrollTo('text=All tool actions go through the gateway API', 'center');
  await cap('What Scopewatch does not claim: it mediates only actions routed through its gateway. Anything that bypasses the API is unobserved.', 5500);
  await cap('Reasoning traces are evidence, not proof of intent. They can be incomplete or unfaithful, and the dashboard says so.', 5000);

  // Beat 7: Nebius and NVIDIA (website results section)
  await badge('');
  await goto(SITE_URL + '#results');
  await scrollTo('#results', 'start');
  await cap('Built on Nebius and NVIDIA: the nebius-demo profile runs Nemotron 3.5 Lightning on Nebius Token Factory.', 5000);
  await cap('Historical auditor-only result, 2026-10-03: 79.2% on 48 held-out cases, zero false holds. Its misses were prompt injection. Published in the repo.', 7000);

  // End card
  await setCard(endCard());
  await cap('', 6000);
  await cap(null);

  const stopT = Date.now() / 1000;
  await cdp.send('Page.stopScreencast');
  await wait(300);
  await browser.close();
  site.close();

  // Assemble: concat demuxer with real per-frame durations, then H.264.
  frames.sort((a, b) => a.t - b.t);
  const list = frames.map((f, i) => {
    const d = i + 1 < frames.length ? Math.max(frames[i + 1].t - f.t, 0.001) : Math.max(stopT - f.t, 0.5);
    return `file '${f.file}'\nduration ${d.toFixed(4)}`;
  }).join('\n') + `\nfile '${frames[frames.length - 1].file}'\n`;
  fs.writeFileSync(path.join(OUT, 'frames.txt'), list);
  const mp4 = path.join(OUT, 'scopewatch-walkthrough.mp4');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', path.join(OUT, 'frames.txt'),
    '-vf', `scale=${W}:${H}:flags=lanczos,fps=30,format=yuv420p`, '-c:v', 'libx264', '-preset', 'slow', '-crf', '20',
    '-movflags', '+faststart', '-an', mp4]);

  // Captions as SRT, relative to the first frame.
  const base = (frames[0].t * 1000 - t0) / 1000;
  const ts = (s) => { s = Math.max(0, s - base); const ms = Math.round(s * 1000); const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, sec = Math.floor(ms / 1000) % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`; };
  fs.writeFileSync(path.join(OUT, 'scopewatch-walkthrough.srt'),
    subs.filter((s) => s.text).map((s, i) => `${i + 1}\n${ts(s.start)} --> ${ts(s.end)}\n${s.text}\n`).join('\n'));
  console.log(`Wrote ${path.relative(ROOT, mp4)} (${frames.length} frames, ${(stopT - frames[0].t).toFixed(1)} s)`);
})().catch((e) => { console.error(e); process.exit(1); });
