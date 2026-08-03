import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const BASE = process.env.CAROUSEL_URL || "http://localhost:3002/carousel";
const OUT = path.resolve("scripts/.e2e-out");
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(new Date().toISOString(), ...a);

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") log("console", m.type(), m.text().slice(0, 200));
  });
  page.on("pageerror", (e) => log("pageerror", e.message));

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60000 });
  // wait videos
  for (let i = 0; i < 60; i++) {
    const n = await page.locator(".studio-video-row").count();
    if (n > 0) break;
    await page.waitForTimeout(2000);
  }
  await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".studio-video-row")];
    const prefer = rows.find((b) => /Indian Oil|Successful People|Boring Businesses|NipRhbyq/i.test(b.textContent || ""));
    (prefer || rows[0])?.click();
  });
  await page.waitForSelector('[data-testid="carousel-continue-from-video"]:not([disabled])', { timeout: 60000 });
  await page.click('[data-testid="carousel-continue-from-video"]');
  for (let i = 0; i < 90; i++) {
    const n = await page.locator('[data-testid="carousel-phase-2"] [role="checkbox"]').count();
    if (n > 0) break;
    await page.waitForTimeout(2000);
  }
  await page.evaluate(() => document.querySelector('[data-testid="carousel-phase-2"] [role="checkbox"]')?.click());
  await page.click('[data-testid="carousel-extract-themes"]');
  for (let i = 0; i < 120; i++) {
    const has = await page.locator('[data-testid="topics-hooks-tree"]').count();
    const extracting = await page.locator('[data-testid="carousel-extract-themes"]').textContent();
    log("wait extract", i, "tree", has, "btn", (extracting||"").replace(/\s+/g," ").slice(0,60));
    if (has > 0 && !/Extracting/i.test(extracting||"")) break;
    await page.waitForTimeout(3000);
  }
  await page.locator('[data-testid="carousel-phase-3"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);

  const diag = await page.evaluate(() => {
    const el = document.querySelector(".topics-hooks-node.is-hook");
    if (!el) return { hooks: 0 };
    const r = el.getBoundingClientRect();
    const x = r.left + Math.min(40, r.width / 2);
    const y = r.top + r.height / 2;
    const top = document.elementFromPoint(x, y);
    const cs = getComputedStyle(el);
    const chain = [];
    let n = top;
    while (n && chain.length < 8) {
      chain.push({ tag: n.tagName, cls: (n.className||"").toString().slice(0,80), pe: getComputedStyle(n).pointerEvents });
      n = n.parentElement;
    }
    return {
      hooks: document.querySelectorAll(".topics-hooks-node.is-hook").length,
      selected: document.querySelectorAll(".topics-hooks-node.is-hook.is-selected").length,
      rect: { x: r.x, y: r.y, w: r.width, h: r.height },
      style: { pe: cs.pointerEvents, cursor: cs.cursor, opacity: cs.opacity, visibility: cs.visibility },
      hit: { tag: top?.tagName, cls: (top?.className||"").toString().slice(0,120), isSelfOrChild: !!(top && (el===top || el.contains(top))) },
      chain,
      nextIssue: !!document.querySelector("#__next-build-watcher, [data-nextjs-dialog], [data-next-badge], nextjs-portal"),
      portals: [...document.querySelectorAll("nextjs-portal, [data-nextjs-toast], [data-next-mark]")].map(e => e.outerHTML.slice(0,120)),
    };
  });
  log("DIAG", JSON.stringify(diag, null, 2));

  // REAL mouse click via Playwright
  const hook = page.locator(".topics-hooks-node.is-hook").first();
  const box = await hook.boundingBox();
  log("box", box);
  try {
    await hook.click({ timeout: 5000 });
    log("locator.click ok");
  } catch (e) {
    log("locator.click FAIL", e.message);
  }
  await page.waitForTimeout(500);
  let after = await page.evaluate(() => ({
    selectedHooks: document.querySelectorAll(".topics-hooks-node.is-hook.is-selected").length,
    previewDisabled: document.querySelector('[data-testid="carousel-continue-preview"]')?.disabled ?? null,
  }));
  log("AFTER locator.click", after);

  // If not selected, try mouse at coordinates
  if (after.selectedHooks === 0 && box) {
    await page.mouse.click(box.x + 30, box.y + box.height / 2);
    await page.waitForTimeout(400);
    after = await page.evaluate(() => ({
      selectedHooks: document.querySelectorAll(".topics-hooks-node.is-hook.is-selected").length,
      previewDisabled: document.querySelector('[data-testid="carousel-continue-preview"]')?.disabled ?? null,
    }));
    log("AFTER mouse.click", after);
  }

  // Toggle off/on
  if (after.selectedHooks >= 1) {
    await hook.click();
    await page.waitForTimeout(300);
    const mid = await page.evaluate(() => document.querySelectorAll(".topics-hooks-node.is-hook.is-selected").length);
    await hook.click();
    await page.waitForTimeout(300);
    const end = await page.evaluate(() => ({
      selectedHooks: document.querySelectorAll(".topics-hooks-node.is-hook.is-selected").length,
      previewDisabled: document.querySelector('[data-testid="carousel-continue-preview"]')?.disabled ?? null,
    }));
    log("toggle mid(deselected?)", mid, "end", end);
  }

  await page.screenshot({ path: path.join(OUT, "hook-select-verify.png"), fullPage: true });
  await browser.close();
  if (after.selectedHooks < 1) process.exit(2);
}
main().catch((e) => { console.error(e); process.exit(1); });
