import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, existsSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close((err) => (err ? reject(err) : resolve(port)));
    });
  });
}

test("live gateway integration, action simulator, approvals, and 5-part evidence", { timeout: 35000 }, async () => {
  const repoRoot = join(import.meta.dirname, "..", "..", "..");
  const venvPython = join(repoRoot, ".venv", "bin", "python");
  const pythonBin = existsSync(venvPython) ? venvPython : "python3";
  const tmpDir = mkdtempSync(join(tmpdir(), "scopewatch-live-test-"));
  const dbPath = join(tmpDir, "test.db");
  const workspacePath = join(tmpDir, "workspace");
  mkdirSync(join(workspacePath, "invoices", "approved"), { recursive: true });
  mkdirSync(join(workspacePath, "invoices", "private"), { recursive: true });
  mkdirSync(join(workspacePath, "outputs"), { recursive: true });

  writeFileSync(
    join(workspacePath, "invoices", "approved", "vendor-a.txt"),
    "vendor a invoice total: $500",
    "utf-8"
  );
  writeFileSync(
    join(workspacePath, "invoices", "private", "salaries.txt"),
    "confidential payroll",
    "utf-8"
  );
  writeFileSync(
    join(workspacePath, "outputs", "old_report.txt"),
    "legacy report to delete",
    "utf-8"
  );

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

  const backendProc = spawn(
    pythonBin,
    ["-c", runnerScript],
    {
      stdio: "inherit",
      env: {
        ...process.env,
        PYTHONPATH: join(repoRoot, "backend"),
      },
    }
  );

  // Wait for backend to be healthy
  let healthy = false;
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/api/v1/health`);
      if (res.ok) {
        healthy = true;
        break;
      }
    } catch {
      // Retry
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  assert.equal(healthy, true, "Backend failed to become healthy");

  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage();
    const origin = `http://127.0.0.1:${port}/?live=1`;

    await page.goto(origin);
    await page.locator("#timeline button").first().waitFor();

    // 1. Verify always-visible safety statement
    const bannerText = await page.locator("#synthetic-banner").innerText();
    assert.match(
      bannerText,
      /This local baseline mediates only actions submitted through its synthetic demo gateway\. It does not intercept arbitrary host or agent operations\./
    );

    // 2. Verify gateway status shows live connection
    await page.waitForFunction(() => {
      const el = document.getElementById("gateway-status");
      return el && el.textContent.includes("Live gateway connected");
    }, { timeout: 10000 });

    // 3. Switch to the live run
    const liveRunButton = page.locator('button:has-text("Live")').first();
    await liveRunButton.waitFor();
    await liveRunButton.click();
    assert.equal(await liveRunButton.getAttribute("aria-pressed"), "true");

    // 4. Test safe action simulation (safe_read preset)
    await page.selectOption("#action-preset", "safe_read");
    assert.equal(await page.inputValue("#action-resource"), "invoices/approved/vendor-a.txt");
    await page.click("#submit-action-btn");

    await page.waitForFunction(() => {
      const msg = document.getElementById("action-status-msg");
      return msg && msg.textContent.includes("ALLOW");
    }, { timeout: 8000 });

    // Verify timeline updated with executed action
    const executedEventBtn = page.locator('.timeline-event:has-text("vendor-a.txt")').first();
    await executedEventBtn.waitFor({ timeout: 8000 });
    await executedEventBtn.click();

    // Verify five-part evidence panel and reasoning labels
    const evidenceText = await page.locator("#event-evidence").innerText();
    assert.match(evidenceText, /1\. Action observation/i);
    assert.match(evidenceText, /2\. Deterministic policy decision/i);
    assert.match(evidenceText, /3\. Human approval/i);
    assert.match(evidenceText, /4\. Controlled synthetic execution/i);
    assert.match(evidenceText, /5\. Reasoning/i);
    assert.match(
      evidenceText,
      /Unavailable\. No provider-exposed reasoning trace was supplied\./
    );
    assert.match(
      evidenceText,
      /Agent-authored summary\. This is not a provider-exposed reasoning trace\./
    );

    // 5. Test blocked action simulation (blocked_path)
    await page.selectOption("#action-preset", "blocked_path");
    await page.click("#submit-action-btn");
    await page.waitForFunction(() => {
      const msg = document.getElementById("action-status-msg");
      return msg && msg.textContent.includes("DENY");
    }, { timeout: 8000 });

    // 6. Test approval workflow (approval_required preset)
    await page.selectOption("#action-preset", "approval_required");
    await page.click("#submit-action-btn");
    await page.waitForFunction(() => {
      const msg = document.getElementById("action-status-msg");
      return msg && msg.textContent.includes("HOLD");
    }, { timeout: 8000 });

    // Verify approval card appears in approvals panel
    const approvalCard = page.locator(".approval-card").first();
    await approvalCard.waitFor({ timeout: 8000 });
    assert.match(await approvalCard.innerText(), /delete_path/);

    // Approve the action
    const approveBtn = approvalCard.locator(".btn-approve");
    await approveBtn.click();

    // Wait for approval card to be removed upon resolution
    await page.waitForFunction(() => {
      return document.querySelectorAll(".approval-card").length === 0;
    }, { timeout: 8000 });

    assert.equal(await page.locator("#pending-approvals-count").innerText(), "0");
  } finally {
    if (browser) await browser.close();
    backendProc.kill("SIGKILL");
    try {
      rmSync(tmpDir, { recursive: true, force: true });
    } catch {
      // Cleanup
    }
  }
});
