// Browser coverage for the reviewer credential flow (#115).
//
// Drives the real gateway with DEMO_TOKEN set (demo mode on) so the panel, the
// 401 refusal, and the authenticated retry are exercised end to end through the
// browser. No Docker, no deploy gate, no network: the gateway runs as a local
// uvicorn process on a free port with a synthetic token.
//
// Not covered here (recorded as unverified in the PR): the deploy gate process
// and Caddy. This proves the gateway demo guard contract the frontend targets.

import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";

// Synthetic credential for this test process only. Never a real demo token.
const DEMO_TOKEN = "browser-test-demo-token-not-real";

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close((err) => (err ? reject(err) : resolve(port)));
    });
  });
}

async function startGateway(repoRoot, { demoMode }) {
  const tmpDir = mkdtempSync(join(tmpdir(), "scopewatch-demo-auth-"));
  const workspacePath = join(tmpDir, "workspace");
  mkdirSync(join(workspacePath, "invoices", "approved"), { recursive: true });
  mkdirSync(join(workspacePath, "invoices", "private"), { recursive: true });
  writeFileSync(join(workspacePath, "invoices", "approved", "vendor-a.txt"), "vendor a invoice total: $500", "utf-8");
  writeFileSync(join(workspacePath, "invoices", "private", "salaries.txt"), "confidential payroll", "utf-8");

  const port = await getFreePort();
  const runnerScript = `
import uvicorn
from pathlib import Path
from scopewatch.app import create_app
from scopewatch.db import init_db

db_path = Path(${JSON.stringify(join(tmpDir, "test.db"))})
init_db(db_path)
app = create_app(db_path=db_path, workspace_root=Path(${JSON.stringify(workspacePath)}))
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="warning")
`;
  const venvPython = join(repoRoot, ".venv", "bin", "python");
  const pythonBin = existsSync(venvPython) ? venvPython : "python3";

  const proc = spawn(pythonBin, ["-c", runnerScript], {
    stdio: "ignore",
    env: {
      ...process.env,
      PYTHONPATH: join(repoRoot, "backend"),
      // Demo mode on/off drives whether the dashboard offers the panel.
      ...(demoMode ? { DEMO_TOKEN } : { DEMO_TOKEN: "" }),
    },
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
      // Retry until the process binds.
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  assert.equal(healthy, true, "gateway failed to become healthy");
  return { port, proc };
}

test("hosted demo: mutations need a reviewer token, reads never carry one", { timeout: 60000 }, async () => {
  const repoRoot = join(import.meta.dirname, "..", "..", "..");
  const { port, proc } = await startGateway(repoRoot, { demoMode: true });
  // The deployed stack seeds scripted scenarios at first start, so /api/v1/runs
  // is never empty and the dashboard never needs a token just to load. Mirror
  // that here: create one run with the token, out of band.
  const seeded = await fetch(`http://127.0.0.1:${port}/api/v1/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": DEMO_TOKEN },
    body: JSON.stringify({
      name: "Seeded scenario run",
      task_scope: {
        schema_version: "1",
        task_description: "Audit approved vendor invoices and generate a summary report.",
        allowed_paths: ["invoices/approved", "outputs"],
        blocked_paths: ["invoices/private"],
        allowed_tools: ["workspace"],
        allowed_operations: ["list_directory", "read_text", "write_text"],
        allowed_network_destinations: [],
        requires_approval: ["delete_path"],
        created_at: new Date().toISOString(),
      },
    }),
  });
  assert.equal(seeded.status, 201, `seeding the demo run failed: ${await seeded.text()}`);

  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));

    // Record every request the dashboard makes and its headers.
    const requests = [];
    page.on("request", (request) => {
      requests.push({ url: request.url(), method: request.method(), headers: request.headers() });
    });

    await page.goto(`http://127.0.0.1:${port}/?live=1`);
    await page.waitForFunction(
      () => (document.getElementById("gateway-status")?.textContent || "").includes("Live gateway connected"),
      { timeout: 15000 },
    );

    // Demo mode is advertised publicly, so the panel opens on its own.
    const panel = page.locator("#reviewer-access-panel");
    await panel.waitFor({ state: "visible", timeout: 10000 });
    const panelNote = await panel.innerText();
    assert.match(panelNote, /Reading runs, events, and evidence needs no token/);
    assert.match(panelNote, /never written to storage/);

    // Reads so far carried no credential.
    const reads = requests.filter((r) => r.method === "GET" && r.url.includes("/api/"));
    assert.ok(reads.length > 0, "expected at least one gateway read");
    for (const read of reads) {
      assert.equal(read.headers["x-demo-token"], undefined, `read must not carry a token: ${read.url}`);
    }

    // A mutation without a token is refused, and the error names the panel.
    await page.locator('#run-buttons button:has-text("Live")').first().click();
    await page.selectOption("#action-preset", "safe_read");
    await page.click("#submit-action-btn");
    await page.waitForFunction(
      () => (document.getElementById("action-status-msg")?.textContent || "").includes("Submission error"),
      { timeout: 10000 },
    );
    const refusalText = await page.locator("#action-status-msg").innerText();
    assert.match(refusalText, /Demo access token required/i);
    assert.match(refusalText, /Reviewer access/);
    assert.ok(!refusalText.includes(DEMO_TOKEN), "the refusal must never echo the token");

    // Now supply the credential and retry the same action.
    await page.fill("#reviewer-token-input", DEMO_TOKEN);
    await page.click("#reviewer-token-save");
    assert.equal(await page.inputValue("#reviewer-token-input"), "", "input is cleared after use");
    assert.match(await page.locator("#reviewer-access-state").innerText(), /in memory for this page only/i);

    await page.click("#submit-action-btn");
    await page.waitForFunction(
      () => (document.getElementById("action-status-msg")?.textContent || "").includes("Action processed"),
      { timeout: 15000 },
    );

    // The retry carried the token; reads still did not.
    const post = requests.filter((r) => r.url.includes("/actions") && r.method === "POST");
    assert.ok(post.length >= 2, `expected an unauthenticated and an authenticated submit, saw ${post.length}`);
    assert.equal(post.at(-1).headers["x-demo-token"], DEMO_TOKEN, "authorised submit must carry the token");
    for (const read of requests.filter((r) => r.method === "GET" && r.url.includes("/api/"))) {
      assert.equal(read.headers["x-demo-token"], undefined, `read must not carry a token: ${read.url}`);
    }

    // Nothing persisted the credential.
    const stored = await page.evaluate(() => ({
      local: window.localStorage.length,
      session: window.sessionStorage.length,
      cookie: document.cookie,
    }));
    assert.equal(stored.local, 0, "localStorage must stay empty");
    assert.equal(stored.session, 0, "sessionStorage must stay empty");
    assert.equal(stored.cookie, "", "no cookie may hold the credential");

    // Forgetting the token drops it: the next mutation is refused again.
    await page.click("#reviewer-token-clear");
    assert.match(await page.locator("#reviewer-access-state").innerText(), /No token set/i);
    await page.click("#submit-action-btn");
    await page.waitForFunction(
      () => (document.getElementById("action-status-msg")?.textContent || "").includes("Submission error"),
      { timeout: 10000 },
    );

    assert.deepEqual(pageErrors, [], "no uncaught page errors");
  } finally {
    if (browser) await browser.close();
    proc.kill("SIGTERM");
  }
});

test("a gateway without demo mode never asks for a token (#115)", { timeout: 60000 }, async () => {
  const repoRoot = join(import.meta.dirname, "..", "..", "..");
  const { port, proc } = await startGateway(repoRoot, { demoMode: false });
  // Demo mode is off, but the dashboard still needs a run to show. Reads are
  // public here, so seeding needs no token.
  const seeded = await fetch(`http://127.0.0.1:${port}/api/v1/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: "Seeded local run",
      task_scope: {
        schema_version: "1",
        task_description: "Audit approved vendor invoices and generate a summary report.",
        allowed_paths: ["invoices/approved", "outputs"],
        blocked_paths: ["invoices/private"],
        allowed_tools: ["workspace"],
        allowed_operations: ["list_directory", "read_text", "write_text"],
        allowed_network_destinations: [],
        requires_approval: ["delete_path"],
        created_at: new Date().toISOString(),
      },
    }),
  });
  assert.equal(seeded.status, 201, `seeding the local run failed: ${await seeded.text()}`);

  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${port}/?live=1`);
    await page.waitForFunction(
      () => (document.getElementById("gateway-status")?.textContent || "").includes("Live gateway connected"),
      { timeout: 15000 },
    );
    // Local dev gateway: guards are off, so the credential panel stays hidden.
    assert.equal(await page.locator("#reviewer-access-panel").isVisible(), false);

    // And a mutation still works, because no token is required here.
    await page.locator('#run-buttons button:has-text("Live")').first().click();
    await page.selectOption("#action-preset", "safe_read");
    await page.click("#submit-action-btn");
    await page.waitForFunction(
      () => (document.getElementById("action-status-msg")?.textContent || "").includes("Action processed"),
      { timeout: 15000 },
    );
    assert.equal(await page.locator("#reviewer-access-panel").isVisible(), false);
  } finally {
    if (browser) await browser.close();
    proc.kill("SIGTERM");
  }
});