import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..");

function readCss() {
  return readFileSync(join(repoRoot, "styles", "reviewer.css"), "utf-8");
}

test("reviewer.css keeps both responsive breakpoints (#62 item 5, #30)", () => {
  const css = readCss();
  // Tablet breakpoint (~reviewer.css:1369)
  assert.match(css, /@media\s*\(\s*max-width:\s*1120px\s*\)/, "1120px tablet breakpoint must exist");
  // Mobile breakpoint (~reviewer.css:1387)
  assert.match(css, /@media\s*\(\s*max-width:\s*640px\s*\)/, "640px mobile breakpoint must exist");
  // Tablet collapses the dashboard grid to a single column
  const tabletBlock = css.slice(css.indexOf("max-width: 1120px"), css.indexOf("max-width: 1120px") + 800);
  assert.match(tabletBlock, /\.dashboard-grid/, "tablet breakpoint must restyle .dashboard-grid");
  assert.match(tabletBlock, /grid-template-columns:\s*1fr/, "tablet breakpoint must collapse to one column");
  // Mobile stacks filters and timeline cards
  const mobileBlock = css.slice(css.indexOf("max-width: 640px"), css.indexOf("max-width: 640px") + 1200);
  assert.match(mobileBlock, /\.filters/, "mobile breakpoint must restyle .filters");
  assert.match(mobileBlock, /\.timeline-event/, "mobile breakpoint must restyle .timeline-event");
});
