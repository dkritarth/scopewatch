import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";
import { chromium } from "playwright";

const root = join(import.meta.dirname, "../../..");

function exportedReplay(status) {
  const script = `
import asyncio, json, runpy, sys, tempfile
from pathlib import Path
from scopewatch.db import init_db, get_connection
from scopewatch.schemas import TaskScope, SubmitActionRequest
from scopewatch.service import ScopewatchService

with tempfile.TemporaryDirectory(prefix="scopewatch-site-approval-") as temp:
    directory = Path(temp)
    database = directory / "gateway.db"
    init_db(database)
    workspace = directory / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    service = ScopewatchService(db_path=database, workspace_root=workspace)
    scenario = json.loads(next(Path("demo/scenarios").glob("06_*.json")).read_text())
    run, _ = service.create_run(name=scenario["name"], task_scope=TaskScope(
        schema_version="1", task_description="Write a synthetic output",
        allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["write_text"], requires_approval=["write_text"],
        created_at="2026-11-05T00:00:00Z"))
    response = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
        tool="workspace", operation="write_text", resource="outputs/recorded.txt",
        arguments={"content":"synthetic output"},
        exposed_reasoning_trace="I will write the permitted output only.",
        reasoning_provenance="SYNTHETIC_FIXTURE")))
    approval = response.approval_request
    assert approval is not None
    state = sys.argv[1]
    if state in ("CONSUMED", "DENIED"):
        asyncio.run(service.resolve_approval(approval.id, state == "CONSUMED"))
    elif state in ("APPROVED", "EXPIRED"):
        connection = get_connection(database)
        if state == "APPROVED":
            connection.execute("UPDATE approval_requests SET status = 'APPROVED' WHERE id = ?", (approval.id,))
        else:
            connection.execute("UPDATE approval_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (approval.id,))
        connection.close()
        if state == "EXPIRED": service.list_approvals()
    exporter = runpy.run_path("scripts/export_site_demo.py")["export"]
    print(json.dumps(exporter(database)))
`;
  const venvPython = join(root, ".venv/bin/python");
  const python = existsSync(venvPython) ? venvPython : "python3";
  const result = spawnSync(python, ["-c", script, status], {
    cwd: root, encoding: "utf8", timeout: 30000,
    env: { ...process.env, PYTHONPATH: join(root, "backend"), DEMO_TOKEN: "",
      SCOPEWATCH_AGENT_PROFILE: "mock", SCOPEWATCH_AUDITOR_PROFILE: "mock",
      SCOPEWATCH_AUDITOR_ROUTING: "single", SCOPEWATCH_REASONING_AUDIT: "true",
      SCOPEWATCH_PROVIDERS_CONFIG: join(root, "backend/config/providers.toml"),
      SCOPEWATCH_EXECUTOR: "local", SCOPEWATCH_TOKEN_BUDGET_STORE: "" },
  });
  assert.equal(result.status, 0, [result.error?.message, `signal=${result.signal}`, result.stderr].filter(Boolean).join("\n"));
  return JSON.parse(result.stdout);
}

async function replay(t, status) {
  const data = exportedReplay(status);
  assert.equal(data.runs[0].actions[0].approval, status);
  const types = { html: "text/html", js: "text/javascript", css: "text/css", svg: "image/svg+xml", json: "application/json" };
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, "http://127.0.0.1").pathname;
    if (path === "/assets/demo-data.json") {
      response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(data));
      return;
    }
    try {
      const content = await readFile(join(root, "site", path));
      response.writeHead(200, { "Content-Type": types[path.split(".").pop()] || "application/octet-stream" }).end(content);
    } catch { response.writeHead(404).end(); }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let browser;
  t.after(async () => {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  });
  browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  const mutations = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    if (request.method() !== "GET") mutations.push(request.url());
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/demo.html`);
  await page.locator("#scenario-list button").first().waitFor();
  return { page, errors, mutations };
}

for (const status of ["CONSUMED", "DENIED", "EXPIRED", "APPROVED"]) {
  test(`static replay preserves recorded ${status} approval`, { timeout: 60000 }, async t => {
    const { page, errors, mutations } = await replay(t, status);
    for (let step = 0; step < 5; step++) await page.locator("#btn-step").click();
    const approval = page.locator("#evidence .ev").filter({ hasText: "3 · Human approval" });
    assert.equal(await approval.locator(".pill").innerText(), status);
    assert.equal(await page.locator("#track .node").nth(3).locator(".s").innerText(), status);
    assert.doesNotMatch(await approval.innerText(), /PENDING|NOT REQUIRED|decision was ALLOW/);
    assert.equal(await page.locator(".btn-approve, .btn-deny").count(), 0);
    assert.equal(await page.locator("#review-bar").isHidden(), true);
    const receipt = page.locator("#evidence .ev").filter({ hasText: "4 · Execution receipt" });
    await receipt.waitFor();
    const receiptStatus = status === "CONSUMED" ? "EXECUTED" : status === "DENIED" ? "NOT_EXECUTED" : "NOT RECORDED";
    assert.equal(await receipt.locator(".pill").innerText(), receiptStatus);
    if (status === "APPROVED" || status === "EXPIRED") {
      assert.equal(await page.locator("#track .node").nth(4).locator(".s").innerText(), "NO RECEIPT");
    }
    assert.deepEqual(mutations, []);
    assert.deepEqual(errors, []);
  });
}

for (const choice of ["APPROVED", "DENIED"]) {
test(`static replay pending ${choice} choice stays visibly hypothetical`, { timeout: 60000 }, async t => {
  const { page, errors, mutations } = await replay(t, "PENDING");
  for (let step = 0; step < 4; step++) await page.locator("#btn-step").click();
  assert.equal(await page.locator("#review-bar").isVisible(), true);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  await page.locator(choice === "APPROVED" ? ".btn-approve" : ".btn-deny").first().click();
  const approval = page.locator("#evidence .ev").filter({ hasText: "3 · Human approval" });
  assert.match(await approval.innerText(), /hypothetical/i);
  assert.match(await approval.innerText(), /recorded.*PENDING/i);
  assert.equal(await page.locator("#review-bar").isHidden(), true);
  await page.locator("#btn-step").click();
  assert.match(await page.locator("#track .node").nth(4).innerText(), /hypothetical/i);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  assert.deepEqual(mutations, []);
  assert.deepEqual(errors, []);
});
}
