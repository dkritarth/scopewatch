import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";
import { runs } from "../../scripts/fixtures.js";

test("synthetic reviewer controls, evidence, keyboard focus, and layouts", { timeout: 30000 }, async () => {
  const root = new URL("../../", import.meta.url);
  const files = new Map([
    ["/", ["index.html", "text/html"]],
    ["/styles/reviewer.css", ["styles/reviewer.css", "text/css"]],
    ...["app", "fixtures", "reviewer-state", "api"].map((name) =>
      [`/scripts/${name}.js`, [`scripts/${name}.js`, "text/javascript"]]),
  ]);
  const server = createServer(async (request, response) => {
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
