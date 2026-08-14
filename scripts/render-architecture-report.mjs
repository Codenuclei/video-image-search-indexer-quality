#!/usr/bin/env node

import { createRequire } from "node:module";
import { access, mkdir, rename, stat, unlink } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const args = process.argv.slice(2);
const inputArg = args[0];
const outputArg = args[1];
if ((inputArg && !outputArg) || (!inputArg && outputArg) || args.length > 2) {
  throw new Error(
    "Usage: node scripts/render-architecture-report.mjs [input.html output.pdf]",
  );
}
const htmlPath = inputArg
  ? path.resolve(process.cwd(), inputArg)
  : path.join(repoRoot, "docs", "architecture-bottleneck-report.html");
const outputPath = outputArg
  ? path.resolve(process.cwd(), outputArg)
  : path.join(repoRoot, "docs", "artifacts", "architecture-bottleneck-report.pdf");
const outputDir = path.dirname(outputPath);
const temporaryPath = `${outputPath}.partial`;

const frontendRequire = createRequire(
  path.join(repoRoot, "carousel-frontend", "package.json"),
);
const { chromium } = frontendRequire("playwright");

await stat(htmlPath);
await mkdir(outputDir, { recursive: true });
await unlink(temporaryPath).catch((error) => {
  if (error?.code !== "ENOENT") throw error;
});

process.env.TZ = "UTC";
const browserCandidates = [
  chromium.executablePath(),
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
];
let executablePath;
for (const candidate of browserCandidates) {
  try {
    await access(candidate);
    executablePath = candidate;
    break;
  } catch {
    // Try the next already-installed browser.
  }
}
if (!executablePath) {
  throw new Error(
    "No local Chromium executable found. Install Playwright Chromium with " +
      "`cd carousel-frontend && npx playwright install chromium`.",
  );
}

const browser = await chromium.launch({ headless: true, executablePath });

try {
  const context = await browser.newContext({
    locale: "en-US",
    timezoneId: "UTC",
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();

  // The report is intentionally offline. Abort any accidental external request.
  await page.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.protocol === "file:" || requestUrl.protocol === "data:") {
      await route.continue();
    } else {
      await route.abort("blockedbyclient");
    }
  });

  await page.goto(pathToFileURL(htmlPath).href, {
    waitUntil: "load",
    timeout: 30_000,
  });
  await page.emulateMedia({ media: "print", colorScheme: "light" });
  await page.evaluate(async () => {
    await document.fonts.ready;
  });

  await page.pdf({
    path: temporaryPath,
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,
    displayHeaderFooter: false,
    tagged: true,
    outline: true,
    margin: {
      top: "0",
      right: "0",
      bottom: "0",
      left: "0",
    },
  });

  const rendered = await stat(temporaryPath);
  if (rendered.size === 0) {
    throw new Error("Renderer produced an empty PDF");
  }
  await rename(temporaryPath, outputPath);
  process.stdout.write(
    `Rendered ${path.relative(repoRoot, outputPath)} (${rendered.size} bytes)\n`,
  );
} finally {
  await browser.close();
  await unlink(temporaryPath).catch((error) => {
    if (error?.code !== "ENOENT") throw error;
  });
}
