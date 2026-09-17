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
    ...["app", "fixtures", "reviewer-state"].map((name) =>
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
      assert.equal(await page.locator("#timeline button").count(), run.events.length);
      for (const event of run.events) {
        const eventButton = page.locator(`[data-event-id="${event.id}"]`);
        await eventButton.focus();
        await page.keyboard.press("Enter");
        assert.equal(await eventButton.getAttribute("aria-current"), "true");
        assert.equal(await eventButton.evaluate((button) => button === document.activeElement), true);
        assert.equal(await page.locator("#event-evidence h3").innerText(), event.title);
        assert.match(await page.locator("#event-evidence").innerText(), /Unavailable/);
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
    assert.equal(await page.locator("#event-evidence h3").innerText(), retainedEvent.title);
    for (const colorScheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
      for (const width of [320, 390, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
          `${colorScheme} layout overflows at ${width}px`);
        assert.equal(await page.locator("#event-evidence h3").isVisible(), true);
      }
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(externalRequests, []);
  } finally {
    await browser?.close();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});
