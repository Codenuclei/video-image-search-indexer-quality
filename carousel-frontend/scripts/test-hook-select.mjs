/**
 * Targeted verification: hook checkbox toggles + selection count updates.
 * Uses real Playwright pointer clicks (not element.click()).
 *
 * Run: node scripts/test-hook-select.mjs
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.CAROUSEL_URL || "http://localhost:3002/carousel";
const OUT = path.resolve("scripts/.e2e-out");
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(new Date().toISOString(), ...a);

async function waitCount(page, sel, { min = 1, timeoutMs = 180_000 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const n = await page.locator(sel).count();
    if (n >= min) return n;
    await page.waitForTimeout(1500);
  }
  throw new Error(`Timeout waiting for ${sel} count>=${min}`);
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.setDefaultTimeout(120_000);
  page.on("pageerror", (e) => log("pageerror", e.message));

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60_000 });

  // CSS token sanity (BOM regression)
  const tokens = await page.evaluate(() => {
    const main = document.querySelector("main.carousel-studio");
    return {
      primary: getComputedStyle(main).getPropertyValue("--studio-primary").trim(),
    };
  });
  log("tokens", tokens);
  if (tokens.primary !== "#0f172a") {
    throw new Error(`studio tokens broken (got primary=${tokens.primary}) — CSS BOM?`);
  }

  await waitCount(page, ".studio-video-row", { timeoutMs: 120_000 });
  await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".studio-video-row")];
    const prefer = rows.find((b) =>
      /Indian Oil|Successful People|Boring Businesses|NipRhbyq/i.test(b.textContent || "")
    );
    (prefer || rows[0])?.click();
  });

  await page.waitForSelector('[data-testid="carousel-continue-themes"]:not([disabled])', {
    timeout: 60_000,
  });
  await page.click('[data-testid="carousel-continue-themes"]');

  await waitCount(page, '[data-testid="carousel-phase-2"] [role="checkbox"]', {
    timeoutMs: 180_000,
  });
  await page.locator('[data-testid="carousel-phase-2"] [role="checkbox"]').first().click();
  await page.click('[data-testid="carousel-extract-themes"]');

  // Poll extract
  for (let i = 0; i < 150; i++) {
    const has = await page.locator('[data-testid="topics-hooks-tree"]').count();
    const btn = ((await page.locator('[data-testid="carousel-extract-themes"]').textContent()) || "")
      .replace(/\s+/g, " ")
      .trim();
    if (i % 5 === 0) log("wait extract", i, "tree", has, "btn", btn.slice(0, 70));
    if (has > 0 && !/Extracting/i.test(btn)) break;
    const alert = await page.locator('[role="alert"]').first().textContent().catch(() => null);
    if (alert && /failed|unreachable|502|timed out/i.test(alert) && has === 0) {
      throw new Error(`Extract failed: ${alert}`);
    }
    await page.waitForTimeout(3000);
  }

  await page.locator('[data-testid="carousel-phase-3"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);

  const hook = page.locator('[data-testid="topics-hooks-hook"]').first();
  if ((await hook.count()) === 0) {
    throw new Error("No hook checkboxes rendered");
  }
  await hook.scrollIntoViewIfNeeded();

  const before = await page.evaluate(() => ({
    selected: document.querySelectorAll('[data-testid="topics-hooks-hook"][aria-checked="true"]').length,
    count: document.querySelector('[data-testid="topics-hooks-selection-count"]')?.textContent || "",
    previewDisabled: document.querySelector('[data-testid="carousel-continue-preview"]')?.disabled ?? null,
  }));
  log("BEFORE", before);
  if (before.selected !== 0) throw new Error("hooks must start unselected");

  await hook.click();
  await page.waitForTimeout(400);
  const after = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="topics-hooks-hook"][aria-checked="true"]');
    const cs = el ? getComputedStyle(el) : null;
    const check = el?.querySelector(".topics-hooks-check");
    return {
      selected: document.querySelectorAll('[data-testid="topics-hooks-hook"][aria-checked="true"]').length,
      count: document.querySelector('[data-testid="topics-hooks-selection-count"]')?.textContent || "",
      previewDisabled: document.querySelector('[data-testid="carousel-continue-preview"]')?.disabled ?? null,
      border: cs?.borderColor,
      checkBg: check ? getComputedStyle(check).backgroundColor : null,
    };
  });
  log("AFTER click", after);
  if (after.selected < 1) throw new Error("hook did not select");
  if (after.previewDisabled) throw new Error("Continue stayed disabled");
  if (!/hook/i.test(after.count)) throw new Error(`selection count missing: ${after.count}`);
  if (!after.checkBg?.includes("15, 23, 42")) throw new Error(`checkbox not filled: ${after.checkBg}`);
  if (!after.border?.includes("15, 23, 42") && !/lab\(7\./.test(after.border || "")) {
    throw new Error(`selected border not slate-900: ${after.border}`);
  }

  await hook.click();
  await page.waitForTimeout(300);
  const mid = await page.evaluate(
    () => document.querySelectorAll('[data-testid="topics-hooks-hook"][aria-checked="true"]').length
  );
  if (mid !== 0) throw new Error("deselect failed");
  await hook.click();
  await page.waitForTimeout(300);
  const end = await page.evaluate(
    () => document.querySelectorAll('[data-testid="topics-hooks-hook"][aria-checked="true"]').length
  );
  if (end < 1) throw new Error("reselect failed");

  await page.screenshot({ path: path.join(OUT, "hook-select-verify.png"), fullPage: true });
  log("OK hook selection toggle + count + Continue");
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
