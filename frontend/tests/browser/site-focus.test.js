import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";
import { chromium } from "playwright";

const root = join(import.meta.dirname, "../../..");
const types = { html: "text/html", js: "text/javascript", css: "text/css", svg: "image/svg+xml", json: "application/json" };

async function openReplay(t) {
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, "http://127.0.0.1").pathname;
    try {
      const content = await readFile(join(root, "site", path));
      response.writeHead(200, { "Content-Type": types[path.split(".").pop()] || "application/octet-stream" }).end(content);
    } catch { response.writeHead(404).end(); }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const browser = await chromium.launch();
  t.after(async () => {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}/demo.html`);
  await page.locator("#scenario-list button").first().waitFor();
  return page;
}

const describeFocus = page => page.evaluate(() => {
  const a = document.activeElement;
  return { tag: a.tagName, text: (a.innerText || "").slice(0, 40), inTimeline: !!a.closest("#timeline"), inScenarios: !!a.closest("#scenario-list") };
});

test("static replay keeps keyboard focus on the scenario button after selecting it", async t => {
  const page = await openReplay(t);
  const button = page.locator("#scenario-list button").nth(1);
  await button.focus();
  await page.keyboard.press("Enter");
  const focus = await describeFocus(page);
  assert.equal(focus.inScenarios, true, `focus fell to ${JSON.stringify(focus)}`);
});

test("static replay keeps keyboard focus on the timeline row after inspecting it", async t => {
  const page = await openReplay(t);
  await page.locator("#timeline li").nth(1).focus();
  await page.keyboard.press("Enter");
  const focus = await describeFocus(page);
  assert.equal(focus.inTimeline, true, `focus fell to ${JSON.stringify(focus)}`);
  assert.equal(await page.evaluate(() => [...document.querySelectorAll("#timeline li")].indexOf(document.activeElement)), 1);
});

test("static replay does not drop focus to the page body after a reviewer decision", async t => {
  const page = await openReplay(t);
  const hold = page.locator("#scenario-list button").filter({ hasText: "Human Approval" });
  await hold.click();
  while (await page.locator("#review-bar").isHidden()) await page.locator("#btn-step").click();
  await page.locator("#review-bar .btn-approve").focus();
  await page.keyboard.press("Enter");
  assert.equal(await page.locator("#review-bar").isHidden(), true);
  const tag = await page.evaluate(() => document.activeElement.tagName);
  assert.notEqual(tag, "BODY", "focus was lost when the approve button was removed");
});
