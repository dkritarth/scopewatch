#!/usr/bin/env node
// Render docs/submission/brief/project-brief.html to the published PDF.
//   node scripts/media/build_project_brief.cjs
// Writes site/assets/docs/scopewatch-project-brief.pdf (served by GitHub Pages).

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const { chromium } = require(require.resolve('playwright', { paths: [path.join(ROOT, 'frontend'), ROOT] }));
const SRC = path.join(ROOT, 'docs', 'submission', 'brief', 'project-brief.html');
const OUT = path.join(ROOT, 'site', 'assets', 'docs', 'scopewatch-project-brief.pdf');

(async () => {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + SRC, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.pdf({
    path: OUT, format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="width:100%;font:7.5pt sans-serif;color:#71717a;padding:0 15mm;display:flex;justify-content:space-between"><span>Scopewatch project brief · 2026-10-29</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
  });
  await browser.close();
  console.log(`Wrote ${path.relative(ROOT, OUT)}`);
})().catch((e) => { console.error(e); process.exit(1); });
