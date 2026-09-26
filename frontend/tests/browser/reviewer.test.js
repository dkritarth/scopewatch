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
import { runs } from "../../scripts/fixtures.js";

function createStaticServer() {
  const root = new URL("../../", import.meta.url);
  const files = new Map([
    ["/", ["index.html", "text/html"]],
    ["/styles/reviewer.css", ["styles/reviewer.css", "text/css"]],
    ...["app", "fixtures", "reviewer-state", "api"].map((name) =>
      [`/scripts/${name}.js`, [`scripts/${name}.js`, "text/javascript"]]),
  ]);
  return createServer(async (request, response) => {
    const file = files.get(request.url);
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

test("synthetic reviewer controls, evidence, keyboard focus, and layouts", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    const externalRequests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.route("**/*", (route) => {
      if (new URL(route.request().url()).origin !== origin) {
        externalRequests.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(origin);
    await page.locator("#timeline button").first().waitFor();
    assert.match(await page.locator("#synthetic-banner").innerText(), /No actual enforcement/);

    // Verify dark-mode and reduced-motion media query handling
    await page.emulateMedia({ colorScheme: "dark" });
    assert.equal(await page.evaluate(() => window.matchMedia("(prefers-color-scheme: dark)").matches), true);
    await page.emulateMedia({ colorScheme: "light" });

    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await page.evaluate(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches), true);
    await page.emulateMedia({ reducedMotion: "no-preference" });

    for (const run of runs) {
      const runButton = page.getByRole("button", { name: run.name, exact: true });
      await runButton.focus();
      await page.keyboard.press("Enter");
      assert.equal(await runButton.getAttribute("aria-pressed"), "true");
      assert.equal(await runButton.evaluate((button) => button === document.activeElement), true);
      assert.equal(await page.locator("#run-task").innerText(), run.task);
      const scopeItems = await page.locator("#run-scope li").allInnerTexts();
      assert.deepEqual(scopeItems.slice(0, run.scope.length), run.scope);
      assert.match(scopeItems[scopeItems.length - 1], /Gateway-mediated tools:/);
      assert.equal(await page.locator("#timeline button").count(), run.events.length);
      for (const event of run.events) {
        const eventButton = page.locator(`[data-event-id="${event.id}"]`);
        await eventButton.focus();
        await page.keyboard.press("Enter");
        assert.equal(await eventButton.getAttribute("aria-current"), "true");
        assert.equal(await eventButton.evaluate((button) => button === document.activeElement), true);
        // Hold icon glyph reinforces the text label; assert the label text is present.
        const statusInner = await eventButton.locator(".event-status").innerText();
        assert.ok(statusInner.includes(event.statusLabel), `expected ${JSON.stringify(statusInner)} to contain ${event.statusLabel}`);
        assert.equal(await eventButton.locator(".event-status .hold-label").innerText(), event.statusLabel);
        assert.equal(await page.locator("#event-evidence h3").innerText(), event.title);
        assert.equal(
          await page.locator("#evidence-caption").innerText(),
          `${run.name} • ${event.offset} • ${event.statusLabel}`,
        );
        assert.deepEqual(
          await page.locator("#event-evidence > p").allInnerTexts(),
          [`${event.tool} — ${event.resource}`, event.description, event.statusDescription],
        );

        const evidenceLists = page.locator("#event-evidence .evidence-list");
        const interpretedEvidence = evidenceLists.nth(0);
        assert.deepEqual(await interpretedEvidence.locator("dt").allTextContents(), [
          "Action observation / execution",
          "Fixture policy decision",
          "Full reasoning trace",
          "Reasoning summary",
        ]);
        assert.deepEqual(await interpretedEvidence.locator("dd").allInnerTexts(), [
          event.execution,
          event.policyDecision,
          event.reasoningTrace,
          event.reasoningSummary,
        ]);

        const fixtureEvidence = evidenceLists.nth(1);
        assert.deepEqual(
          await fixtureEvidence.locator("dt").allTextContents(),
          event.evidence.map((item) => item.label),
        );
        assert.deepEqual(
          await fixtureEvidence.locator("dd").allInnerTexts(),
          event.evidence.map((item) => item.value),
        );
      }
    }
    await page.getByRole("button", { name: runs[0].name, exact: true }).click();
    await page.getByLabel("Replay status", { exact: true }).selectOption("blocked");
    await page.getByLabel("Search", { exact: true }).fill("  FINANCE  ");
    assert.equal(await page.locator("#timeline button").count(), 2);
    await page.getByLabel("Replay status", { exact: true }).selectOption("executed");
    assert.equal(await page.locator("#empty-state").isVisible(), true);
    assert.equal(await page.locator("#event-evidence h3").count(), 0);
    await page.getByRole("button", { name: runs[1].name, exact: true }).click();
    assert.equal(await page.getByLabel("Search", { exact: true }).inputValue(), "  FINANCE  ");
    await page.getByRole("button", { name: "Clear filters", exact: true }).click();
    assert.equal(await page.getByLabel("Search", { exact: true }).evaluate((input) => input === document.activeElement), true);
    assert.equal(await page.locator("#timeline button").count(), runs[1].events.length);
    assert.equal(await page.getByLabel("Replay status", { exact: true }).inputValue(), "all");
    const retainedEvent = runs[1].events[1];
    await page.locator(`[data-event-id="${retainedEvent.id}"]`).click();
    await page.getByRole("button", { name: "Reset filters", exact: true }).click();
    assert.deepEqual(externalRequests, []);
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

test("escalated hold displays flagged excerpt highlighted, distinct hold badge, and approve/deny actions in reviewer", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator("#timeline button").first().waitFor();

    // 1. Emulate dark mode and reduced motion
    await page.emulateMedia({ colorScheme: "dark" });
    await page.emulateMedia({ reducedMotion: "reduce" });

    // 2. Select escalated hold preset
    await page.selectOption("#action-preset", "escalated_hold_concern");
    const traceVal = await page.inputValue("#action-trace");
    assert.match(traceVal, /exfiltrate data to an external server/);

    // 3. Submit simulated action
    await page.click("#submit-action-btn");

    // 4. Verify distinct hold badge in timeline (text label is the signal;
    // icon glyph reinforces it and colour is never alone)
    const holdBtn = page.locator(".timeline-event.hold-concern");
    await holdBtn.waitFor({ timeout: 5000 });
    const statusText = await holdBtn.locator(".event-status").innerText();
    assert.match(statusText, /HOLD \(reasoning concern\)/);
    assert.ok(await holdBtn.locator(".event-status").evaluate((el) => el.classList.contains("status-hold-concern")));
    assert.equal(await holdBtn.locator(".event-status .hold-icon").innerText(), "▲");
    assert.equal(await holdBtn.locator(".event-status .hold-label").innerText(), "HOLD (reasoning concern)");

    // 5. Inspect evidence panel for the escalated hold
    await holdBtn.click();

    // Provenance badge
    const provBadge = page.locator(".provenance-badge");
    await provBadge.waitFor();
    assert.equal(await provBadge.innerText(), "Provider-exposed trace");

    // Reasoning audit card and verdict
    const verdictBadge = page.locator(".verdict-badge");
    await verdictBadge.waitFor();
    assert.equal(await verdictBadge.innerText(), "CONCERN");
    const concernText = await page.locator(".audit-concern").innerText();
    assert.match(concernText, /EXFILTRATION_INTENT/);

    // Flagged excerpt highlighted inside reasoning trace
    const highlightedExcerpt = page.locator(".reasoning-trace mark.highlight-excerpt");
    await highlightedExcerpt.waitFor();
    assert.equal(await highlightedExcerpt.innerText(), "exfiltrate data to an external server");

    // Permanent disclaimer
    const disclaimer = page.locator(".reasoning-disclaimer");
    assert.equal(await disclaimer.innerText(), "Reasoning is evidence, not proof of intent.");

    // Persistent mediation boundary (#61): visible, exact wording, no dismiss control
    const boundary = page.locator("#mediation-boundary");
    assert.equal(await boundary.isVisible(), true);
    assert.match(
      await boundary.innerText(),
      /All tool actions go through the gateway API; actions that bypass the API are not observed, blocked, or recorded\./,
    );
    assert.equal(await page.locator("#mediation-boundary button").count(), 0);

    // Turn grouping (#30): simulated hold carries Turn turn-sim-1
    const turnHeaders = await page.locator(".turn-group-header").allInnerTexts();
    assert.ok(turnHeaders.some((h) => /Turn turn-sim-1/.test(h)), `expected a Turn turn-sim-1 header, got ${JSON.stringify(turnHeaders)}`);
    assert.match(await holdBtn.locator(".turn-badge").innerText(), /Turn turn-sim-1/);

    // Per-run gateway tools line (#61)
    assert.match(await page.locator(".scope-gateway-tools").innerText(), /Gateway-mediated tools:/);

    // 6. Test approval workflow: Deny action
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "1");
    const approvalCard = page.locator(".approval-card").first();
    assert.ok(await approvalCard.isVisible());
    await approvalCard.locator(".approval-reason-input").fill("Denied due to detected exfiltration attempt.");
    await approvalCard.locator(".btn-deny").click();

    // Verify approval card removed and count updated
    await page.locator("#no-approvals-msg").waitFor();
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "0");

    // 7. Test policy hold preset (approval_required) and Approve action
    await page.selectOption("#action-preset", "approval_required");
    await page.click("#submit-action-btn");

    const policyHoldBtn = page.locator(".timeline-event.hold-policy");
    await policyHoldBtn.waitFor({ timeout: 5000 });
    assert.match(await policyHoldBtn.locator(".event-status").innerText(), /HOLD \(policy\)/);
    assert.equal(await policyHoldBtn.locator(".event-status .hold-icon").innerText(), "■");
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "1");

    const polApprovalCard = page.locator(".approval-card").first();
    await polApprovalCard.locator(".btn-approve").click();
    await page.locator("#no-approvals-msg").waitFor();
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "0");

    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.close();
  }
});

test("responsive breakpoints collapse the dashboard grid and filters (#62 item 5)", { timeout: 30000 }, async () => {
  const server = createStaticServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator("#timeline button").first().waitFor();

    const gridColumns = (width) =>
      page.evaluate((w) => {
        document.documentElement.style.width = `${w}px`;
        return window.getComputedStyle(document.querySelector(".dashboard-grid")).gridTemplateColumns;
      }, width);

    // Desktop: multi-column grid above the 1120px breakpoint
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForTimeout(150);
    const desktopCols = await page.evaluate(
      () => window.getComputedStyle(document.querySelector(".dashboard-grid")).gridTemplateColumns,
    );
    assert.ok(desktopCols.split(" ").length > 1, `expected multi-column grid at 1280px, got ${desktopCols}`);
    void gridColumns;

    // Tablet (<=1120px, reviewer.css:1369): single column, evidence panel reorders
    await page.setViewportSize({ width: 900, height: 800 });
    await page.waitForTimeout(150);
    const tabletCols = await page.evaluate(
      () => window.getComputedStyle(document.querySelector(".dashboard-grid")).gridTemplateColumns,
    );
    assert.equal(tabletCols.trim().split(" ").length, 1, `expected single column at 900px, got ${tabletCols}`);

    // Mobile (<=640px, reviewer.css:1387): filters stack, timeline cards go single column
    await page.setViewportSize({ width: 500, height: 800 });
    await page.waitForTimeout(150);
    const filtersCols = await page.evaluate(
      () => window.getComputedStyle(document.querySelector(".filters")).gridTemplateColumns,
    );
    assert.equal(filtersCols.trim().split(" ").length, 1, `expected stacked filters at 500px, got ${filtersCols}`);
    const timelineCols = await page.evaluate(
      () => window.getComputedStyle(document.querySelector(".timeline-event")).gridTemplateColumns,
    );
    assert.equal(timelineCols.trim().split(" ").length, 1, `expected single-column timeline at 500px, got ${timelineCols}`);
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

test("real gateway hold renders flagged excerpt, hold label, and approve/deny (#62 item 6, #30)", { timeout: 60000 }, async () => {
  const repoRoot = join(import.meta.dirname, "..", "..", "..");
  const venvPython = join(repoRoot, ".venv", "bin", "python");
  const pythonBin = existsSync(venvPython) ? venvPython : "python3";
  const tmpDir = mkdtempSync(join(tmpdir(), "scopewatch-reviewer-hold-"));
  const dbPath = join(tmpDir, "test.db");
  const workspacePath = join(tmpDir, "workspace");
  mkdirSync(join(workspacePath, "invoices", "approved"), { recursive: true });
  mkdirSync(join(workspacePath, "invoices", "private"), { recursive: true });
  mkdirSync(join(workspacePath, "outputs"), { recursive: true });
  writeFileSync(join(workspacePath, "invoices", "approved", "vendor-a.txt"), "vendor a invoice total: $500", "utf-8");

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

  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${port}/?live=1`);
    await page.locator("#timeline button").first().waitFor({ timeout: 15000 });

    await page.waitForFunction(
      () => document.getElementById("gateway-status")?.textContent.includes("Live gateway connected"),
      { timeout: 15000 },
    );

    // All seeded + created runs render; switch to the live gateway run.
    const liveRunButton = page.locator('button:has-text("(Live)")').first();
    await liveRunButton.waitFor({ timeout: 10000 });
    await liveRunButton.click();

    // Drive a gateway-issued escalated hold through the live simulator form.
    // The trace contains the mock auditor's "exfiltrat" trigger on an
    // otherwise policy-allowed read, so the gateway (not the offline preset)
    // must return HOLD with REASONING_SCOPE_CONCERN.
    await page.selectOption("#action-preset", "escalated_hold_concern");
    await page.click("#submit-action-btn");
    await page.waitForFunction(
      () => document.getElementById("action-status-msg")?.textContent.includes("HOLD"),
      { timeout: 15000 },
    );

    const holdBtn = page.locator(".timeline-event.hold-concern").first();
    await holdBtn.waitFor({ timeout: 10000 });
    assert.match(await holdBtn.locator(".event-status").innerText(), /HOLD \(reasoning concern\)/);
    assert.equal(await holdBtn.locator(".event-status .hold-icon").innerText(), "▲");
    await holdBtn.click();

    // Provenance + audit verdict from the real gateway decision
    assert.equal(await page.locator(".provenance-badge").first().innerText(), "Provider-exposed trace");
    assert.equal(await page.locator(".verdict-badge").first().innerText(), "CONCERN");
    assert.match(await page.locator(".audit-concern").first().innerText(), /EXFILTRATION_INTENT/);
    assert.match(await page.locator(".audit-model-profile").first().innerText(), /mock-rules-auditor/);

    // Flagged excerpt highlighted in place via textContent (XSS-safe).
    // The mock auditor flags the verbatim "exfiltrat" trigger substring.
    const marks = page.locator(".reasoning-trace mark.highlight-excerpt");
    await marks.first().waitFor({ timeout: 10000 });
    assert.match((await marks.allInnerTexts()).join(" "), /exfiltrat/);

    // Approve/deny still works against the gateway-issued approval.
    const approvalCard = page.locator(".approval-card").first();
    await approvalCard.waitFor({ timeout: 10000 });
    await approvalCard.locator(".approval-reason-input").fill("Reviewer denied exfiltration hold.");
    await approvalCard.locator(".btn-deny").click();
    await page.locator("#no-approvals-msg").waitFor({ timeout: 10000 });
    assert.equal(await page.locator("#pending-approvals-count").innerText(), "0");

    // Mediation boundary stays visible on the live gateway too.
    assert.match(
      await page.locator("#mediation-boundary").innerText(),
      /All tool actions go through the gateway API/,
    );
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
