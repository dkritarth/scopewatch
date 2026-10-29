#!/usr/bin/env node
// Capture the README images from the project website so the README matches
// what GitHub Pages serves. Renders site/ over a local static server, in dark
// and light colour schemes, and writes PNGs to docs/images/readme/.
//
// Usage (needs `npm ci --prefix frontend` for Playwright and Chromium):
//   node scripts/capture_readme_images.cjs
//
// The hero shows the site's illustrative held write_text action, not a live
// gateway capture. Fonts load from Google Fonts like the deployed site; without
// network the capture falls back to system fonts.

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const SITE = path.join(ROOT, 'site');
const OUT = path.join(ROOT, 'docs', 'images', 'readme');
const { chromium } = require(require.resolve('playwright', { paths: [path.join(ROOT, 'frontend'), ROOT] }));

const TYPES = {
  '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.webp': 'image/webp', '.png': 'image/png',
};

function serve() {
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/\/$/, '/index.html');
    const file = path.join(SITE, path.normalize(rel));
    if (!file.startsWith(SITE + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const server = await serve();
  const url = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch();
  try {
    for (const scheme of ['dark', 'light']) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: scheme });
      await page.goto(url, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(1500); // hero stages and stamp animate in
      // Hero: top strip, nav and the held-action card, stopping above the scrolling ticker.
      const ticker = await page.$('.ticker-track');
      const tickerTop = ticker ? (await ticker.boundingBox()).y : 840;
      // Trim the page's side gutters so text stays legible at README width.
      await page.screenshot({ path: path.join(OUT, `hero-${scheme}.png`), clip: { x: 112, y: 0, width: 1216, height: Math.floor(tickerTop) - 16 } });
      const how = await page.$('#how');
      await how.scrollIntoViewIfNeeded();
      await page.waitForTimeout(600);
      const box = await how.boundingBox();
      await page.screenshot({ path: path.join(OUT, `how-it-works-${scheme}.png`), clip: { x: 112, y: box.y + 72, width: 1216, height: box.height - 144 } });
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  console.log(`Wrote README images to ${path.relative(ROOT, OUT)}/`);
})();
