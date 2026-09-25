import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
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
      assert.deepEqual(await page.locator("#run-scope li").allInnerTexts(), run.scope);
      assert.equal(await page.locator("#timeline button").count(), run.events.length);
      for (const event of run.events) {
        const eventButton = page.locator(`[data-event-id="${event.id}"]`);
        await eventButton.focus();
        await page.keyboard.press("Enter");
        assert.equal(await eventButton.getAttribute("aria-current"), "true");
        assert.equal(await eventButton.evaluate((button) => button === document.activeElement), true);
        assert.equal(await eventButton.locator(".event-status").innerText(), event.statusLabel);
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

    // 4. Verify distinct hold badge in timeline
    const holdBtn = page.locator(".timeline-event.hold-concern");
    await holdBtn.waitFor({ timeout: 5000 });
    const statusText = await holdBtn.locator(".event-status").innerText();
    assert.equal(statusText, "HOLD (reasoning concern)");
    assert.ok(await holdBtn.locator(".event-status").evaluate((el) => el.classList.contains("status-hold-concern")));

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
    assert.equal(await policyHoldBtn.locator(".event-status").innerText(), "HOLD (policy)");
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
