/**
 * Full user-driven carousel studio CDP verification (Playwright).
 * Proves hard phase gates + long-job waits. No auto-advance assumed.
 *
 * Run: node scripts/e2e-carousel-flow.mjs
 */
import { chromium } from "playwright";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const BASE = process.env.CAROUSEL_URL || "http://localhost:3002/carousel";
const OUT = path.resolve("scripts/.e2e-out");
fs.mkdirSync(OUT, { recursive: true });

const log = (...a) => console.log(new Date().toISOString(), ...a);

async function waitFor(page, fn, { timeoutMs = 60_000, intervalMs = 1500, label = "condition" } = {}) {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeoutMs) {
    last = await fn();
    if (last) return last;
    await page.waitForTimeout(intervalMs);
  }
  throw new Error(`Timeout waiting for ${label} after ${timeoutMs}ms (last=${JSON.stringify(last)})`);
}

async function uiState(page) {
  return page.evaluate(() => {
    const text = (sel) => (document.querySelector(sel)?.textContent || "").trim();
    const exists = (sel) => Boolean(document.querySelector(sel));
    const btnText = (sel) => {
      const el = document.querySelector(sel);
      return el ? (el.textContent || "").replace(/\s+/g, " ").trim() : null;
    };
    const themeRows = document.querySelectorAll('[data-testid="carousel-phase-2"] [role="checkbox"]');
    const hooksTree = exists('[data-testid="topics-hooks-tree"]');
    const hookNodes = document.querySelectorAll(".topics-hooks-node.is-hook, .topics-hooks-kind");
    const phaseChips = [...document.querySelectorAll(".studio-phase-chip")].map((c) => ({
      text: (c.textContent || "").replace(/\s+/g, " ").trim(),
      active: c.classList.contains("is-active"),
      done: c.classList.contains("is-done"),
    }));
    return {
      phase1: exists('[data-testid="carousel-phase-1"]'),
      phase2: exists('[data-testid="carousel-phase-2"]'),
      phase3: exists('[data-testid="carousel-phase-3"]'),
      phase4: exists('[data-testid="carousel-phase-4"]'),
      phase5: exists('[data-testid="carousel-phase-5"]'),
      continueThemes: btnText('[data-testid="carousel-continue-themes"]'),
      extractBtn: btnText('[data-testid="carousel-extract-themes"]'),
      previewBtn: btnText('[data-testid="carousel-continue-preview"]'),
      generateBtn: btnText('[data-testid="carousel-generate"]'),
      selectImagesBtn: btnText('[data-testid="carousel-select-images"]'),
      themeCount: themeRows.length,
      hooksTree,
      hookKindCount: hookNodes.length,
      phaseChips,
      bodySnippet: (document.body?.innerText || "").slice(0, 400),
      error: text('[role="alert"]') || null,
    };
  });
}

async function shot(page, name) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  log("screenshot", file);
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.setDefaultTimeout(30_000);
  let st;

  const report = { steps: [], ok: false };

  try {
    log("goto", BASE);
    await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await waitFor(
      page,
      async () => (await uiState(page)).phase1,
      { timeoutMs: 60_000, label: "phase1" }
    );
    await shot(page, "01-loaded");

    // --- Step 1: select video (no themes yet) ---
    log("STEP1 select video");
    await waitFor(
      page,
      async () => {
        const n = await page.locator(".studio-video-row").count();
        const loading = await page.getByText("Loading recent videos").count();
        log("video rows", n, "loading?", loading > 0);
        return n > 0 ? n : null;
      },
      { timeoutMs: 120_000, intervalMs: 2000, label: "video list rows" }
    );

    // Prefer NipRhbyq / Indian Oil video if present, else first row
    const videoClicked = await page.evaluate(() => {
      const rows = [...document.querySelectorAll(".studio-video-row")];
      const prefer = rows.find((b) =>
        /NipRhbyq|Indian Oil|Successful People|Boring Businesses/i.test(b.textContent || "")
      );
      const pick = prefer || rows[0];
      if (!pick) return false;
      pick.click();
      return (pick.textContent || "").replace(/\s+/g, " ").trim().slice(0, 100);
    });
    assert.ok(videoClicked, "Could not click a video");
    log("selected video", videoClicked);
    report.steps.push({ step: 1, selectVideo: videoClicked });

    // Hard gate: wait for Continue CTA; themes list must stay empty until Continue
    st = await waitFor(
      page,
      async () => {
        const s = await uiState(page);
        if (s.continueThemes) return s;
        // If themes already visible with Extract and no Continue → fallthrough
        if (s.themeCount > 0 && s.extractBtn && !s.continueThemes) {
          return { ...s, fallthrough: true };
        }
        return null;
      },
      { timeoutMs: 60_000, intervalMs: 1000, label: "Continue CTA after video select" }
    );
    if (st.fallthrough) {
      throw new Error("FALLTHROUGH: themes/Extract visible without Continue click");
    }
    assert.equal(st.phase3, false, "phase3 must not appear before Continue/extract");
    assert.equal(st.phase5, false, "phase5 must not appear before generate");
    await shot(page, "02-video-selected-before-continue");

    // Click Continue in Step 2
    log("STEP1→2 Continue for themes");
    const continueSel = '[data-testid="carousel-continue-themes"]';
    await page.click(continueSel);
    report.steps.push({ step: "1b", continue: continueSel });

    // Wait for themes to appear
    st = await waitFor(
      page,
      async () => {
        const s = await uiState(page);
        return s.themeCount > 0 ? s : null;
      },
      { timeoutMs: 180_000, intervalMs: 2000, label: "themes after Continue" }
    );
    log("themes appeared", st.themeCount);
    assert.ok(st.themeCount > 0, "themes must appear after Continue");
    assert.equal(st.phase3, false, "hooks must not appear before Extract");
    assert.equal(st.phase5, false, "phase5 must not appear before generate");
    await shot(page, "03-themes-after-continue");
    report.steps.push({ step: 2, themes: st.themeCount });

    // --- Step 2: select first theme + Extract ---
    log("STEP2 select theme + extract");
    await page.evaluate(() => {
      const row = document.querySelector('[data-testid="carousel-phase-2"] [role="checkbox"]');
      row?.click();
    });
    await page.waitForTimeout(500);
    await page.click('[data-testid="carousel-extract-themes"]');
    report.steps.push({ step: "2b", extract: "clicked" });

    // Poll until extracting clears and phase3/hooks tree appears (can take minutes)
    st = await waitFor(
      page,
      async () => {
        const s = await uiState(page);
        const extracting = /Extracting/i.test(s.extractBtn || "");
        if (extracting) {
          log("…still extracting", s.extractBtn);
          return null;
        }
        if (s.phase3 && s.hooksTree) return s;
        if (s.error) return s; // surface error
        return null;
      },
      { timeoutMs: 360_000, intervalMs: 5000, label: "hooks/topics tree after extract" }
    );
    if (st.error && !st.phase3) {
      throw new Error(`Extract failed with UI error: ${st.error}`);
    }
    assert.equal(st.phase3, true, "phase3 must show after extract");
    assert.equal(st.hooksTree, true, "topics-hooks-tree must render");
    assert.ok(st.hookKindCount > 0, "tree must have topic/hook nodes");
    assert.equal(st.phase5, false, "no phase5 fallthrough after extract");
    await shot(page, "04-hooks-topics-after-extract");
    log("hooks/topics OK", { hookKindCount: st.hookKindCount });
    report.steps.push({ step: 3, hooksTree: true, nodes: st.hookKindCount });

    // --- Step 3: select a hook with real pointer click → continue to direction ---
    log("STEP3 select hook/topic → preview");
    const hookLoc = page.locator('[data-testid="topics-hooks-hook"], .topics-hooks-node.is-hook').first();
    const topicLoc = page.locator('[data-testid="topics-hooks-topic"], .topics-hooks-node:not(.is-hook)').first();
    if ((await hookLoc.count()) > 0) {
      await hookLoc.scrollIntoViewIfNeeded();
      await hookLoc.click();
    } else {
      await topicLoc.scrollIntoViewIfNeeded();
      await topicLoc.click();
    }
    await page.waitForTimeout(500);
    const selectedAfter = await page.evaluate(() => ({
      hooks: document.querySelectorAll(".topics-hooks-node.is-hook.is-selected").length,
      topics: document.querySelectorAll(".topics-hooks-node.is-selected:not(.is-hook)").length,
      previewDisabled: document.querySelector('[data-testid="carousel-continue-preview"]')?.disabled ?? true,
      countText: document.querySelector('[data-testid="topics-hooks-selection-count"]')?.textContent || "",
    }));
    assert.ok(selectedAfter.hooks + selectedAfter.topics >= 1, "hook/topic must show selected");
    assert.equal(selectedAfter.previewDisabled, false, "Continue must enable after selection");
    assert.ok(/selected/i.test(selectedAfter.countText), "selection count must update");
    await shot(page, "04b-hook-selected");
    await page.click('[data-testid="carousel-continue-preview"]');
    st = await waitFor(
      page,
      async () => {
        const s = await uiState(page);
        return s.phase4 ? s : null;
      },
      { timeoutMs: 120_000, intervalMs: 2000, label: "phase4 direction/preview" }
    );
    assert.equal(st.phase4, true);
    assert.equal(st.phase5, false, "no phase5 before generate");
    await shot(page, "05-direction-stage");
    report.steps.push({ step: 4, direction: true });

    // --- Step 4: Generate carousels (text only) ---
    log("STEP4 generate carousels");
    await page.click('[data-testid="carousel-generate"]');
    st = await waitFor(
      page,
      async () => {
        const s = await uiState(page);
        const building = /Building/i.test(s.generateBtn || "");
        if (building) {
          log("…still building", s.generateBtn);
          return null;
        }
        if (s.phase5) return s;
        if (s.error && /fail|timeout|reach/i.test(s.error)) return s;
        return null;
      },
      { timeoutMs: 360_000, intervalMs: 5000, label: "phase5 after generate" }
    );
    if (!st.phase5) {
      throw new Error(`Generate did not reach phase5. error=${st.error} state=${JSON.stringify(st)}`);
    }
    await shot(page, "06-carousel-texts");
    report.steps.push({ step: 5, carousels: true });

    // --- Step 5: Select images ---
    log("STEP5 select images");
    const selectReady = await waitFor(
      page,
      async () => {
        const btn = page.locator('[data-testid="carousel-select-images"]');
        if ((await btn.count()) === 0) return null;
        if (await btn.isDisabled()) return null;
        return true;
      },
      { timeoutMs: 60_000, label: "select images button enabled" }
    );
    assert.ok(selectReady);
    await page.click('[data-testid="carousel-select-images"]');

    st = await waitFor(
      page,
      async () => {
        const s = await page.evaluate(() => {
          const btn = document.querySelector('[data-testid="carousel-select-images"]');
          const selecting = /Select|filter|Working|Loading|images/i.test(btn?.textContent || "");
          const imgs = document.querySelectorAll(
            '[data-testid="carousel-phase-5"] img[src*="frame"], [data-testid="carousel-phase-5"] img[src*="/media/"]'
          );
          const readyNote = (document.body?.innerText || "").includes("Frames:");
          return {
            btn: (btn?.textContent || "").replace(/\s+/g, " ").trim(),
            imgCount: imgs.length,
            readyNote,
            disabled: btn?.disabled ?? true,
          };
        });
        log("…image select status", s);
        if (s.imgCount > 0 || s.readyNote) return s;
        return null;
      },
      { timeoutMs: 420_000, intervalMs: 6000, label: "images after select & filter" }
    );
    assert.ok(st.imgCount > 0 || st.readyNote, "expected images or quality note after select");
    await shot(page, "07-images-ready");
    report.steps.push({ step: 6, images: st });

    report.ok = true;
    log("FULL FLOW GREEN");
    fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
  } catch (e) {
    report.ok = false;
    report.error = String(e?.stack || e);
    try {
      await shot(page, "FAIL");
      const st = await uiState(page);
      report.finalState = st;
    } catch {}
    fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
    console.error(report.error);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main();
