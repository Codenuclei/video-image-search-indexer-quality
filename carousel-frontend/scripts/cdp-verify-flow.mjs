/**
 * CDP / Playwright verification of Carousel Studio user-driven flow.
 * Run from repo: node carousel-frontend/scripts/cdp-verify-flow.mjs
 * Or: cd carousel-frontend && node scripts/cdp-verify-flow.mjs
 */
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const BASE = process.env.CAROUSEL_URL || "http://localhost:3002/carousel";
const OUT = path.join(ROOT, ".cdp-verify");
const CHROME =
  process.env.CHROME_PATH ||
  "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing";
const LOG = [];

function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.map(String).join(" ")}`;
  LOG.push(line);
  console.log(line);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function poll(fn, { timeoutMs = 180_000, intervalMs = 2500, label = "poll" } = {}) {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeoutMs) {
    last = await fn();
    if (last?.fail) throw new Error(`${label} failed: ${JSON.stringify(last).slice(0, 400)}`);
    if (last?.ok) {
      log(`OK ${label}`, JSON.stringify(last).slice(0, 320));
      return last;
    }
    log(`… ${label}`, JSON.stringify(last).slice(0, 240));
    await sleep(intervalMs);
  }
  throw new Error(`${label} timed out after ${timeoutMs}ms last=${JSON.stringify(last)}`);
}

async function pageState(page) {
  return page.evaluate(() => {
    const text = document.body?.innerText || "";
    const phase = (n) => !!document.querySelector(`[data-testid="carousel-phase-${n}"]`);
    const btnInfo = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      return {
        text: (el.innerText || "").replace(/\s+/g, " ").trim(),
        disabled: !!el.disabled,
      };
    };
    const themeCards = [
      ...document.querySelectorAll(
        '[data-testid="carousel-phase-2"] button[role="checkbox"], [data-testid="carousel-phase-2"] .studio-select-row'
      ),
    ];
    const selectedThemes = themeCards.filter(
      (b) =>
        b.className.includes("is-selected") ||
        b.getAttribute("aria-checked") === "true"
    );
    const tree = document.querySelector('[data-testid="topics-hooks-tree"]');
    const hookNodes = tree
      ? [...tree.querySelectorAll(".topics-hooks-node.is-hook")].length
      : 0;
    const topicNodes = tree
      ? [...tree.querySelectorAll(".topics-hooks-node:not(.is-hook)")].length
      : 0;
    const selectedHooks = tree
      ? [...tree.querySelectorAll(".topics-hooks-node.is-hook.is-selected")].length
      : 0;
    const selectedTopics = tree
      ? [...tree.querySelectorAll(".topics-hooks-node.is-selected:not(.is-hook)")].length
      : 0;
    const alert =
      [...document.querySelectorAll('[role="alert"]')]
        .map((e) => e.innerText)
        .join(" | ")
        .slice(0, 300) || null;
    const outlineError = [...document.querySelectorAll("p, div")]
      .map((e) => e.innerText || "")
      .find((t) => /Carousel generation failed|Image selection failed|Generate returned no/i.test(t));
    return {
      phase1: phase(1),
      phase2: phase(2),
      phase3: phase(3),
      phase4: phase(4),
      phase5: phase(5),
      continueFromVideo: btnInfo('[data-testid="carousel-continue-from-video"]'),
      continueThemes: btnInfo('[data-testid="carousel-continue-themes"]'),
      extractBtn: btnInfo('[data-testid="carousel-extract-themes"]'),
      previewBtn: btnInfo('[data-testid="carousel-continue-preview"]'),
      generateBtn: btnInfo('[data-testid="carousel-generate"]'),
      selectImagesBtn: btnInfo('[data-testid="carousel-select-images"]'),
      themeCardCount: themeCards.length,
      selectedThemeCount: selectedThemes.length,
      treePresent: !!tree,
      hookNodes,
      topicNodes,
      selectedHooks,
      selectedTopics,
      alert,
      outlineError: outlineError ? outlineError.slice(0, 200) : null,
      loading:
        /Extracting hooks|Loading themes|Generating themes|Building|Selecting images|Working…|Generating carousels/i.test(
          text
        ),
      imagesReady: /Frames:|candidates|images ready|Images selected/i.test(text),
      slideCount: (text.match(/Slide\s+\d+/gi) || []).length,
      carouselTabs: document.querySelectorAll('[data-testid="carousel-tablist"] button').length,
      bodyHead: text.replace(/\s+/g, " ").slice(0, 600),
    };
  });
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  log("launching Chrome", CHROME);
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--window-size=1400,1000"],
  });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  page.setDefaultTimeout(30_000);

  const assert = (cond, msg) => {
    if (!cond) throw new Error(`ASSERT: ${msg}`);
    log("assert ok:", msg);
  };

  try {
    log("goto", BASE);
    await page.goto(BASE, { waitUntil: "networkidle", timeout: 90_000 }).catch(async () => {
      await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60_000 });
    });

    await poll(
      async () => {
        const s = await pageState(page);
        return {
          ok: s.phase1 && (s.bodyHead.includes("Create a carousel") || s.bodyHead.includes("Choose a video")),
          ...s,
        };
      },
      { timeoutMs: 60_000, intervalMs: 1500, label: "studio-ready" }
    );

    // Prefer a known captioned video if listed
    const videoClicked = await page.evaluate(() => {
      const buttons = [...document.querySelectorAll(".studio-video-list button, [data-testid='carousel-phase-1'] li button, [data-testid='carousel-phase-1'] button")];
      const candidates = buttons.filter(
        (b) =>
          !b.matches('[data-testid="carousel-continue-from-video"]') &&
          !b.matches('[data-testid="carousel-continue-themes"]') &&
          (b.innerText || "").trim().length > 8
      );
      const prefer =
        candidates.find((b) => /Indian Oil|NipR|Day at|Physics/i.test(b.innerText || "")) ||
        candidates[0];
      if (!prefer) return null;
      prefer.click();
      return (prefer.innerText || "").slice(0, 120);
    });
    assert(videoClicked, "clicked a video");
    log("selected video", videoClicked);
    await sleep(2000);

    let s = await pageState(page);
    await page.screenshot({ path: path.join(OUT, "01-after-video.png"), fullPage: true });
    assert(!s.phase3 && !s.phase5, "no auto jump to hooks/end after video select");
    assert(s.themeCardCount === 0, "themes must not auto-load after video select");
    assert(s.selectedThemeCount === 0, "no auto-selected themes");

    // Wait for Continue enabled (saves list may load)
    s = await poll(
      async () => {
        const st = await pageState(page);
        const btn = st.continueFromVideo || st.continueThemes;
        return { ok: !!btn && !btn.disabled, ...st };
      },
      { timeoutMs: 45_000, intervalMs: 1500, label: "continue-enabled" }
    );

    if (s.continueFromVideo && !s.continueFromVideo.disabled) {
      await page.click('[data-testid="carousel-continue-from-video"]');
    } else {
      await page.click('[data-testid="carousel-continue-themes"]');
    }

    s = await poll(
      async () => {
        const st = await pageState(page);
        const fail =
          st.alert &&
          /Theme segmentation failed|unreachable|timed out/i.test(st.alert) &&
          st.themeCardCount === 0;
        return { ok: st.themeCardCount > 0 && !st.loading, fail, ...st };
      },
      { timeoutMs: 240_000, intervalMs: 3000, label: "themes-loaded" }
    );
    await page.screenshot({ path: path.join(OUT, "02-themes.png"), fullPage: true });
    assert(s.selectedThemeCount === 0, "themes listed but none auto-selected");
    assert(!s.phase3, "hooks tree must not appear before extract");

    // Select first theme card
    await page.evaluate(() => {
      const cards = [
        ...document.querySelectorAll(
          '[data-testid="carousel-phase-2"] button[role="checkbox"]'
        ),
      ];
      if (!cards[0]) throw new Error("no theme card");
      cards[0].click();
    });
    await sleep(700);
    s = await pageState(page);
    assert(s.selectedThemeCount >= 1, "theme selected by click");
    assert(!s.phase3, "theme select alone must not open hooks");

    await page.click('[data-testid="carousel-extract-themes"]');
    s = await poll(
      async () => {
        const st = await pageState(page);
        const fail =
          st.alert &&
          /Hook & topic extract failed|proxy timed out|unreachable|Internal Server Error/i.test(
            st.alert
          ) &&
          !st.treePresent;
        return {
          ok:
            st.phase3 &&
            st.treePresent &&
            (st.hookNodes > 0 || st.topicNodes > 0) &&
            !st.loading,
          fail,
          ...st,
        };
      },
      { timeoutMs: 360_000, intervalMs: 4000, label: "extract-tree" }
    );
    await page.screenshot({ path: path.join(OUT, "03-hooks-tree.png"), fullPage: true });
    assert(s.selectedHooks === 0, "no auto-selected hooks after extract");

    // Select first hook with a real pointer click (respects CSS pointer-events)
    const hook = page.locator('[data-testid="topics-hooks-hook"], .topics-hooks-node.is-hook').first();
    await hook.scrollIntoViewIfNeeded();
    await hook.click();
    await sleep(600);
    s = await pageState(page);
    assert(s.selectedHooks + s.selectedTopics >= 1, "selected hook or topic");
    assert(s.previewBtn && !s.previewBtn.disabled, "Continue enabled after selection");
    // Deselect then reselect to prove toggle
    await hook.click();
    await sleep(300);
    s = await pageState(page);
    const afterDeselect = s.selectedHooks + s.selectedTopics;
    await hook.click();
    await sleep(300);
    s = await pageState(page);
    assert(afterDeselect === 0, "deselect cleared selection");
    assert(s.selectedHooks + s.selectedTopics >= 1, "re-selected after toggle");
    await page.screenshot({ path: path.join(OUT, "03b-hook-selected.png"), fullPage: true });

    await page.click('[data-testid="carousel-continue-preview"]');
    s = await poll(
      async () => {
        const st = await pageState(page);
        return { ok: st.phase4 && !!st.generateBtn, ...st };
      },
      { timeoutMs: 180_000, intervalMs: 2500, label: "direction-phase" }
    );
    await page.screenshot({ path: path.join(OUT, "04-direction.png"), fullPage: true });

    await page.click('[data-testid="carousel-generate"]');
    s = await poll(
      async () => {
        const st = await pageState(page);
        const fail =
          (st.outlineError && /failed|no carousels/i.test(st.outlineError)) ||
          (st.alert && /Carousel generation failed|timed out/i.test(st.alert) && !st.phase5);
        return {
          ok:
            st.phase5 &&
            (st.carouselTabs > 0 || st.slideCount > 0 || /Your carousels|Slide/i.test(st.bodyHead)) &&
            !st.loading,
          fail,
          ...st,
        };
      },
      { timeoutMs: 420_000, intervalMs: 5000, label: "generate-texts" }
    );
    await page.screenshot({ path: path.join(OUT, "05-generated.png"), fullPage: true });
    assert(!/No carousel text yet/i.test(s.bodyHead), "carousel text present");

    // Select images (button may appear only when images not ready)
    s = await pageState(page);
    if (s.selectImagesBtn) {
      assert(!s.selectImagesBtn.disabled, "select images enabled");
      await page.click('[data-testid="carousel-select-images"]');
      s = await poll(
        async () => {
          const st = await pageState(page);
          const fail =
            st.outlineError && /Image selection failed/i.test(st.outlineError);
          return {
            ok: (st.imagesReady || /Frames:|candidates|kept/i.test(st.bodyHead)) && !st.loading,
            fail,
            ...st,
          };
        },
        { timeoutMs: 420_000, intervalMs: 5000, label: "select-images" }
      );
    } else {
      throw new Error("select-images button missing after generate");
    }
    await page.screenshot({ path: path.join(OUT, "06-images.png"), fullPage: true });

    log("FULL FLOW PASSED");
    fs.writeFileSync(
      path.join(OUT, "result.json"),
      JSON.stringify({ ok: true, log: LOG }, null, 2)
    );
    console.log("RESULT: PASS");
    await browser.close();
    process.exit(0);
  } catch (err) {
    log("FAIL", String(err));
    try {
      await page.screenshot({ path: path.join(OUT, "FAIL.png"), fullPage: true });
      fs.writeFileSync(
        path.join(OUT, "fail-state.json"),
        JSON.stringify(await pageState(page), null, 2)
      );
    } catch {}
    fs.writeFileSync(
      path.join(OUT, "result.json"),
      JSON.stringify({ ok: false, error: String(err), log: LOG }, null, 2)
    );
    console.error("RESULT: FAIL", err);
    await browser.close().catch(() => {});
    process.exit(1);
  }
}

main();
