import { seedSimulatorRun } from "./gateway-fixture.js";
import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, existsSync } from "node:fs";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

for (const transport of ["sse", "polling"]) {
  test(`initial history failure recovers automatically through ${transport}`, { timeout: 30000 }, async () => {
    const server = createStaticServer();
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    let browser;
    let recovered = false;
    const run = { id: "synthetic-history-run", name: "History recovery", status: "ACTIVE",
      task_scope: { task_description: "Synthetic history recovery", allowed_paths: ["scratch"],
        blocked_paths: [], allowed_tools: ["workspace"], allowed_operations: ["read_text"], requires_approval: [] } };
    const event = { id: "recovered-event", sequence: 1, run_id: run.id,
      event_type: "RUN_CREATED", timestamp: "2026-01-01T00:00:00Z", summary: "Recovered stored event", details: {} };
    try {
      browser = await chromium.launch();
      const page = await browser.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.addInitScript(mode => {
        if (mode === "polling") {
          delete window.EventSource;
        } else {
          window.EventSource = class {
            constructor(url) { this.url = url; this.handlers = {}; window.__historyStream = this; }
            addEventListener(type, handler) { this.handlers[type] = handler; }
            close() {}
          };
        }
      }, transport);
      await page.route("**/api/v1/**", async route => {
        const path = new URL(route.request().url()).pathname;
        const json = value => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
        if (path.endsWith("/health")) return json({ status: "ok" });
        if (path.endsWith("/runs")) return json([run]);
        if (path.endsWith("/approvals")) return json([]);
        if (path.endsWith("/events")) {
          return recovered ? json([event]) : route.fulfill({ status: 503, body: "{}" });
        }
        return route.fulfill({ status: 404 });
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/?live=1`);
      await page.waitForFunction(() => Boolean(window.__runRefreshInterval));
      await page.locator('.rail-nav [data-view-target="activity"]').click();
      assert.equal(await page.locator("#run-buttons button").getAttribute("aria-pressed"), "true");
      assert.match(await page.locator("#timeline-status").innerText(), /Could not load stored events/);
      recovered = true;
      if (transport === "sse") {
        assert.equal(await page.evaluate(() => new URL(window.__historyStream.url, location.href).searchParams.get("after_sequence")), "0");
        await page.evaluate(stored => {
          window.__historyStream.handlers.RUN_CREATED({ data: JSON.stringify(stored) });
        }, event);
      }
      await page.locator('#timeline [data-event-id="recovered-event"]').waitFor({ timeout: 10000 });
      assert.match(await page.locator("#timeline").innerText(), /Recovered stored event/);
      assert.equal(await page.locator("#timeline-status").isHidden(), true);
      assert.deepEqual(errors, []);
    } finally {
      if (browser) await browser.close();
      server.close();
    }
  });
}

function createStaticServer() {
  const root = new URL("../../", import.meta.url);
  const files = new Map([
    ["/", ["index.html", "text/html"]],
    ["/styles/reviewer.css", ["styles/reviewer.css", "text/css"]],
    ["/styles/celestial.css", ["styles/celestial.css", "text/css"]],
    ...["dm-sans", "instrument-serif", "instrument-serif-italic"].map(name => [`/fonts/${name}.woff2`, [`fonts/${name}.woff2`, "font/woff2"]]),
    ...["app", "fixtures", "reviewer-state", "api", "celestial"].map((name) =>
      [`/scripts/${name}.js`, [`scripts/${name}.js`, "text/javascript"]]),
  ]);
  return createServer(async (request, response) => {
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    const file = files.get(pathname);
    if (!file) {
      response.writeHead(404).end();
      return;
    }
    try {
      response.writeHead(200, { "Content-Type": file[1] });
      response.end(await readFile(new URL(file[0], root)));
    } catch {
      response.destroy();
    }
  });
}

test("approval double-confirm arms, switches target, and fires exactly once", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator("#run-buttons button").first().waitFor();

    await page.locator('.rail-nav [data-view-target="test"]').click();
    await page.selectOption("#action-preset", "escalated_hold_concern");
    await page.click("#submit-action-btn");
    await page.locator('.rail-nav [data-view-target="requests"]').click();
    const card = page.locator(".approval-card").first();
    await card.waitFor({ timeout: 5000 });

    // First click arms deny: explicit confirm text + note, nothing resolves.
    await card.locator(".btn-deny").click();
    assert.equal(await card.locator(".btn-deny").innerText(), "Confirm deny");
    assert.equal(await card.locator(".approval-confirm-note").isVisible(), true);
    assert.match(await card.locator(".approval-confirm-note").innerText(), /Click again to confirm/);
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "1");

    // Clicking approve switches the arm instead of firing deny.
    await card.locator(".btn-approve").click();
    assert.equal(await card.locator(".btn-approve").innerText(), "Confirm approve");
    assert.equal(await card.locator(".btn-deny").innerText(), "Deny");
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "1");

    // Second click on the armed button fires exactly once.
    await card.locator(".btn-approve").click();
    await page.locator("#no-approvals-msg").waitFor({ timeout: 5000 });
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "0");
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

test("360px viewport stays usable with no horizontal overflow", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 360, height: 800 } });
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator("#run-buttons button").first().waitFor();

    const overflow = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      inner: window.innerWidth,
    }));
    assert.ok(overflow.doc <= 361, `no horizontal overflow at 360px, got scrollWidth ${overflow.doc}`);
    assert.ok(overflow.body <= 361, `body must not overflow at 360px, got ${overflow.body}`);

    // Core flows stay reachable: run select, timeline select, simulator submit.
    await page.locator("#run-buttons button").first().click();
    await page.locator("#timeline button").first().click();
    assert.ok((await page.locator("#event-evidence h3").innerText()).length > 0);
    await page.locator('.rail-nav [data-view-target="test"]').click();
    await page.selectOption("#action-preset", "approval_required");
    await page.click("#submit-action-btn");
    await page.locator('.rail-nav [data-view-target="requests"]').click();
    const card = page.locator(".approval-card").first();
    await card.waitFor({ timeout: 5000 });
    assert.equal(await card.locator(".btn-approve").isVisible(), true);
    assert.equal(await card.locator(".btn-deny").isVisible(), true);

    const afterOverflow = await page.evaluate(() => document.documentElement.scrollWidth);
    assert.ok(afterOverflow <= 361, `approval card must not overflow at 360px, got ${afterOverflow}`);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

test("missing EventSource falls back to polling with a visible text banner", { timeout: 30000 }, async () => {
  const staticFiles = new Map([
    ["/", ["index.html", "text/html"]],
    ["/styles/reviewer.css", ["styles/reviewer.css", "text/css"]],
    ["/styles/celestial.css", ["styles/celestial.css", "text/css"]],
    ...["dm-sans", "instrument-serif", "instrument-serif-italic"].map(name => [`/fonts/${name}.woff2`, [`fonts/${name}.woff2`, "font/woff2"]]),
    ...["app", "fixtures", "reviewer-state", "api", "celestial"].map((name) =>
      [`/scripts/${name}.js`, [`scripts/${name}.js`, "text/javascript"]]),
  ]);
  const root = new URL("../../", import.meta.url);
  const fakeRun = {
    id: "run-fake",
    name: "Fake live run",
    task_scope: {
      task_description: "Synthetic fallback check.",
      allowed_paths: ["invoices/approved"],
      blocked_paths: ["invoices/private"],
      allowed_tools: ["workspace"],
      allowed_operations: ["read_text"],
      requires_approval: [],
    },
  };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    const json = (value) => {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(value));
    };
    if (url.pathname === "/api/v1/health") return json({ status: "ok" });
    if (url.pathname === "/api/v1/runs") return json([fakeRun]);
    if (url.pathname === `/api/v1/runs/${fakeRun.id}/events`) return json([]);
    if (url.pathname === "/api/v1/approvals") return json([]);
    const file = staticFiles.get(url.pathname);
    if (!file) {
      response.writeHead(404).end();
      return;
    }
    try {
      response.writeHead(200, { "Content-Type": file[1] });
      response.end(await readFile(new URL(file[0], root)));
    } catch {
      response.destroy();
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    // Remove the SSE transport before any module loads: the dashboard must
    // degrade to polling with backoff instead of failing silently.
    await page.addInitScript(() => {
      delete window.EventSource;
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/?live=1`);
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator("#run-buttons button").first().waitFor({ timeout: 15000 });
    await page.waitForFunction(
      () => document.getElementById("gateway-status")?.textContent.match(/Live gateway connected|polling fallback/),
      { timeout: 15000 },
    );

    const liveRunButton = page.locator("#run-buttons button").first();
    await liveRunButton.waitFor({ timeout: 10000 });
    await liveRunButton.click();

    const banner = page.locator("#sse-banner");
    await page.waitForFunction(() => !document.getElementById("sse-banner")?.hidden, { timeout: 10000 });
    assert.equal(await banner.isVisible(), true);
    assert.match(await page.locator("#sse-banner-text").innerText(), /Polling/);
    assert.match(await page.locator("#gateway-status").innerText(), /polling fallback/);
    assert.equal(await page.locator("#sse-retry").isVisible(), true);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

test("malformed live frames never crash the timeline (debug hook)", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator("#run-buttons button").first().waitFor();
    const before = await page.locator("#timeline button").count();

    const results = await page.evaluate(() => {
      const hook = window.__scopewatch;
      return {
        hasHook: typeof hook?.safeTransformApiEvent === "function",
        outcomes: [
          hook.safeTransformApiEvent(null),
          hook.safeTransformApiEvent("junk{{{"),
          hook.safeTransformApiEvent({ noId: true }),
          hook.safeTransformApiEvent({ id: "x" }),
        ],
      };
    });
    assert.equal(results.hasHook, true, "debug hook must expose safeTransformApiEvent");
    assert.deepEqual(results.outcomes, [null, null, null, null]);
    assert.equal(await page.locator("#timeline button").count(), before);
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = createNetServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close((err) => (err ? reject(err) : resolve(port)));
    });
  });
}

test("live deny stays disabled while the resolution is in flight", { timeout: 60000 }, async () => {
  const repoRoot = join(import.meta.dirname, "..", "..", "..");
  const venvPython = join(repoRoot, ".venv", "bin", "python");
  const pythonBin = existsSync(venvPython) ? venvPython : "python3";
  const tmpDir = mkdtempSync(join(tmpdir(), "scopewatch-deny-pending-"));
  const dbPath = join(tmpDir, "test.db");
  const workspacePath = join(tmpDir, "workspace");
  mkdirSync(join(workspacePath, "invoices", "approved"), { recursive: true });
  mkdirSync(join(workspacePath, "invoices", "private"), { recursive: true });
  mkdirSync(join(workspacePath, "outputs"), { recursive: true });
  writeFileSync(join(workspacePath, "invoices", "approved", "vendor-a.txt"), "vendor a invoice total: $500", "utf-8");
  writeFileSync(join(workspacePath, "outputs", "old_report.txt"), "legacy report to delete", "utf-8");

  const port = await getFreePort();
  const runnerScript = `
import uvicorn
from pathlib import Path
from scopewatch.app import create_app
from scopewatch.db import init_db

db_path = Path("${dbPath}")
init_db(db_path)
app = create_app(db_path=db_path, workspace_root=Path("${workspacePath}"))
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="warning")
`;

  const backendProc = spawn(pythonBin, ["-c", runnerScript], {
    stdio: "pipe",
    env: { ...process.env, PYTHONPATH: join(repoRoot, "backend") },
  });

  let healthy = false;
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/api/v1/health`);
      if (res.ok) {
        healthy = true;
        break;
      }
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  assert.equal(healthy, true, "Backend failed to become healthy");
  await seedSimulatorRun(port);

  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${port}/?live=1`);
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator("#run-buttons button").first().waitFor({ timeout: 15000 });
    await page.waitForFunction(
      () => document.getElementById("gateway-status")?.textContent.includes("Live gateway connected"),
      { timeout: 15000 },
    );

    const liveRunButton = page.locator("#run-buttons button").first();
    await liveRunButton.waitFor({ timeout: 10000 });
    await liveRunButton.click();

    await page.locator('.rail-nav [data-view-target="test"]').click();
    await page.selectOption("#action-preset", "approval_required");
    await page.click("#submit-action-btn");
    await page.locator('.rail-nav [data-view-target="requests"]').click();
    const approvalCard = page.locator(".approval-card").first();
    await approvalCard.waitFor({ timeout: 10000 });

    // Stall the deny POST so the in-flight disabled state is observable.
    await page.route("**/api/v1/approvals/*/deny", async (route) => {
      await new Promise((r) => setTimeout(r, 600));
      await route.continue();
    });

    await approvalCard.locator(".btn-deny").click();
    assert.equal(await approvalCard.locator(".btn-deny").innerText(), "Confirm deny");
    await approvalCard.locator(".btn-deny").click();
    // Both buttons lock while the single-use resolution is pending.
    await page.waitForFunction(() => {
      const card = document.querySelector(".approval-card");
      if (!card) return true;
      const buttons = card.querySelectorAll(".btn-approve, .btn-deny");
      return buttons.length === 2 && [...buttons].every((b) => b.disabled);
    }, { timeout: 10000 });
    await page.locator("#no-approvals-msg").waitFor({ timeout: 15000 });
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "0");
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    backendProc.kill("SIGKILL");
    try {
      rmSync(tmpDir, { recursive: true, force: true });
    } catch {
      // cleanup
    }
  }
});

test("armed state and note survive an SSE frame (#208)", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator('.rail-nav [data-view-target="activity"]').click();
    await page.locator("#run-buttons button").first().waitFor();

    await page.locator('.rail-nav [data-view-target="test"]').click();
    await page.selectOption("#action-preset", "approval_required");
    await page.click("#submit-action-btn");
    await page.locator('.rail-nav [data-view-target="requests"]').click();
    const card = page.locator(".approval-card").first();
    await card.waitFor({ timeout: 5000 });

    // Verify card content requirements (#208): reason, arguments preview, run, expiry
    assert.match(await card.locator(".approval-card-run").innerText(), /Run:/);
    assert.match(await card.locator(".approval-card-reason").innerText(), /Reason:/);
    assert.match(await card.locator(".approval-card-args").innerText(), /Arguments:/);
    assert.match(await card.locator(".approval-card-expiry").innerText(), /Expires:/);

    // Type reviewer note
    const noteText = "Retain this note across SSE frame";
    await card.locator(".approval-reason-input").fill(noteText);

    // Arm approve
    await card.locator(".btn-approve").click();
    assert.equal(await card.locator(".btn-approve").innerText(), "Confirm approve");
    assert.equal(await card.locator(".approval-confirm-note").isVisible(), true);

    // Trigger an SSE frame / approvals re-render
    await page.evaluate(() => {
      window.__scopewatch.renderApprovals();
    });

    // Assert armed state and note survived!
    assert.equal(await card.locator(".approval-reason-input").inputValue(), noteText);
    assert.equal(await card.locator(".btn-approve").innerText(), "Confirm approve");
    assert.equal(await card.locator(".approval-confirm-note").isVisible(), true);

    // Second click finishes resolution
    await card.locator(".btn-approve").click();
    await page.locator("#no-approvals-msg").waitFor({ timeout: 5000 });
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

for (const delayed of ["history", "run-list", "terminal-stream"]) {
  test(`run refresh discards stale ${delayed} responses`, { timeout: 30000 }, async () => {
    const server = createStaticServer();
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    let browser;
    let heldRoute;
    let heldBody;
    let unblock;
    const blocked = new Promise(resolve => { unblock = resolve; });
    let discovery = delayed === "terminal-stream";
    let status = "ACTIVE";
    let delayNext = false;
    const run = { id: "synthetic-refresh-run", name: "Refresh ordering",
      task_scope: { task_description: "Synthetic refresh ordering", allowed_paths: ["outputs"],
        allowed_tools: ["workspace"], allowed_operations: ["read_text"], requires_approval: [] } };
    const event = { id: "refresh-created", sequence: 1, run_id: run.id, event_type: "RUN_CREATED",
      timestamp: "2026-11-05T00:00:00Z", summary: "Synthetic run created", details: {} };
    try {
      browser = await chromium.launch();
      const page = await browser.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.addInitScript(() => {
        window.EventSource = class {
          constructor() { this.handlers = {}; window.__runStream = this; }
          addEventListener(type, handler) { this.handlers[type] = handler; }
          close() {}
        };
      });
      await page.route("**/api/v1/**", async route => {
        const path = new URL(route.request().url()).pathname;
        const json = value => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
        if (path.endsWith("/health")) return json({ status: "ok" });
        if (path.endsWith("/approvals")) return json([]);
        const runs = discovery ? [{ ...run, status }] : [];
        if (delayNext && (delayed === "history" ? path.endsWith("/events") : path.endsWith("/runs"))) {
          heldRoute = route;
          heldBody = delayed === "history" ? [event] : runs;
          delayNext = false;
          unblock();
          return;
        }
        if (path.endsWith("/runs")) return json(runs);
        if (path.endsWith("/events")) return json([event]);
        return route.fulfill({ status: 404 });
      });
      await page.goto(`http://127.0.0.1:${server.address().port}/?live=1`);
      await page.waitForFunction(() => Boolean(window.__runRefreshInterval));
      await page.evaluate(() => clearInterval(window.__runRefreshInterval));
      await page.locator('.rail-nav [data-view-target="activity"]').click();
      discovery = true;
      delayNext = true;
      await page.evaluate(() => { window.__delayedRefresh = window.__scopewatch.refreshRuns(); });
      await blocked;
      status = "COMPLETED";
      if (delayed === "terminal-stream") {
        await page.evaluate(terminal => {
          window.__runStream.handlers.RUN_COMPLETED({ data: JSON.stringify(terminal) });
        }, { ...event, id: "refresh-completed", sequence: 2, event_type: "RUN_COMPLETED", summary: "Run completed" });
      } else {
        await page.evaluate(() => window.__scopewatch.refreshRuns());
        await page.locator('#run-buttons [data-run-id="synthetic-refresh-run"]').click();
      }
      const button = page.locator('#run-buttons [data-run-id="synthetic-refresh-run"]');
      assert.equal(await button.getAttribute("data-status"), "COMPLETED");
      assert.match(await page.locator("#run-task").innerText(), /COMPLETED/);
      await heldRoute.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(heldBody) });
      heldRoute = null;
      await page.evaluate(() => window.__delayedRefresh);
      assert.equal(await button.getAttribute("data-status"), "COMPLETED");
      assert.match(await page.locator("#run-task").innerText(), /COMPLETED/);
      await page.evaluate(() => window.__scopewatch.refreshRuns());
      assert.equal(await button.getAttribute("data-status"), "COMPLETED");
      assert.match(await page.locator("#run-task").innerText(), /COMPLETED/);
      assert.deepEqual(errors, []);
    } finally {
      if (heldRoute) await heldRoute.abort().catch(() => {});
      if (browser) await browser.close();
      server.close();
    }
  });
}

test("superseded multi-run history never populates evidence caches", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  let heldRoute;
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  let discovery = false;
  const run = id => ({ id, name: id, status: "ACTIVE", task_scope: {
    task_description: "Synthetic cache ordering", allowed_paths: ["outputs"],
    allowed_tools: ["workspace"], allowed_operations: ["write_text"], requires_approval: [] } });
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      const json = value => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
      if (path.endsWith("/health")) return json({ status: "ok" });
      if (path.endsWith("/approvals")) return json([]);
      if (path.endsWith("/runs")) return json(discovery ? [run("first-run"), run("second-run")] : []);
      if (path.endsWith("/first-run/events")) return json([{
        id: "stale-action", sequence: 1, run_id: "first-run", event_type: "ACTION_REQUESTED",
        timestamp: "2026-11-05T00:00:00Z", summary: "Superseded synthetic action",
        details: { tool: "workspace", operation: "write_text", resource: "outputs/stale.txt" },
      }]);
      if (path.endsWith("/second-run/events")) {
        heldRoute = route;
        release();
        return;
      }
      return route.fulfill({ status: 404 });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/?live=1`);
    await page.waitForFunction(() => Boolean(window.__runRefreshInterval));
    await page.evaluate(() => clearInterval(window.__runRefreshInterval));
    discovery = true;
    await page.evaluate(() => { window.__delayedRefresh = window.__scopewatch.refreshRuns(); });
    await blocked;
    discovery = false;
    await page.evaluate(() => window.__scopewatch.refreshRuns());
    const cacheSize = () => page.evaluate(async () => (await import("/scripts/app.js")).actionRequestStore.size);
    assert.equal(await cacheSize(), 0);
    await heldRoute.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    heldRoute = null;
    await page.evaluate(() => window.__delayedRefresh);
    assert.equal(await cacheSize(), 0);
    assert.equal(await page.locator("#run-buttons button").count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    if (heldRoute) await heldRoute.abort().catch(() => {});
    if (browser) await browser.close();
    server.close();
  }
});
