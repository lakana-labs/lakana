import { defineConfig, devices } from "@playwright/test";

const URL = "http://127.0.0.1:4173";

/**
 * The desktop width every screen is taken at, the main target (still-water.md). Tall, so the
 * sidebar reaches the foot of a long page and a page's lower half is in the picture.
 */
const DESKTOP = { width: 1280, height: 1800 };

/** A phone's width, for the main places only. */
const PHONE = { width: 390, height: 844 };

// The visual regression suite (docs/agent/testing.md, "Visual regression"): screenshots of every
// screen against the stubs of e2e/, compared with the baselines committed in
// e2e/visual/screenshots/. It runs inside the pinned Playwright image only (scripts/visual.mjs),
// never on the host: another machine's fonts and rendering would differ.
export default defineConfig({
  testDir: "e2e/visual",
  // Not *.spec.ts, so the smoke suite (playwright.config.ts) never picks it up.
  testMatch: "*.visual.ts",
  outputDir: "test-results/visual",
  snapshotPathTemplate: "{testDir}/screenshots/{projectName}/{arg}{ext}",
  retries: 0,
  forbidOnly: true,
  fullyParallel: true,
  // CI's runners have four CPUs; the local run stays inside its container's memory.
  workers: 4,
  reporter: process.env["CI"] === undefined ? "list" : [["list"], ["github"]],
  expect: {
    toHaveScreenshot: {
      // The same image draws the same pixels; the default per-pixel threshold absorbs
      // anti-aliasing noise, and no pixel beyond it may differ.
      maxDiffPixels: 0,
      animations: "disabled",
      caret: "hide",
      scale: "css",
    },
  },
  use: {
    baseURL: URL,
    locale: "en-GB",
    timezoneId: "UTC",
    reducedMotion: "reduce",
  },
  projects: [
    {
      name: "light",
      use: { ...devices["Desktop Chrome"], viewport: DESKTOP, colorScheme: "light" },
    },
    {
      name: "dark",
      use: { ...devices["Desktop Chrome"], viewport: DESKTOP, colorScheme: "dark" },
    },
    {
      name: "phone-light",
      grep: /@phone/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: PHONE,
        hasTouch: true,
        isMobile: true,
        colorScheme: "light",
      },
    },
    {
      name: "phone-dark",
      grep: /@phone/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: PHONE,
        hasTouch: true,
        isMobile: true,
        colorScheme: "dark",
      },
    },
  ],
  webServer: {
    command: "node node_modules/vite/bin/vite.js preview --port 4173 --strictPort",
    url: URL,
  },
});
