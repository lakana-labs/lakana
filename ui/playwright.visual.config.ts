import { defineConfig, devices } from "@playwright/test";

const URL = "http://127.0.0.1:4173";

/**
 * The desktop width every screen is taken at, the main target (still-water.md). Tall, so the
 * sidebar reaches the foot of a long page and a page's lower half is in the picture.
 */
const DESKTOP = { width: 1280, height: 1800 };

/** A phone's width, for the main places only. */
const PHONE = { width: 390, height: 844 };

/** The screenshots; the projects below say which shots each takes. */
const SCREENS = "screens.visual.ts";

/** How every picture is taken and compared. */
const SCREENSHOT = {
  // No pixel may differ by more than a YIQ delta of 14 (pixelmatch's 35,215 × 0.02²): the
  // default threshold, 0.2 (a delta of 1,409), let a swapped colour token pass (#506), the
  // smallest swap of Still Water's tokens is a delta of 37, and the same page drawn twice
  // differs by up to 10 (rounded corners by one level, a pixel beside a focused heading in dark).
  maxDiffPixels: 0,
  threshold: 0.02,
  animations: "disabled",
  caret: "hide",
  scale: "css",
} as const;

/** A phone's pictures leave out its fading overlay scrollbars (e2e/visual/phone.css). */
const PHONE_EXPECT = {
  toHaveScreenshot: { ...SCREENSHOT, stylePath: "e2e/visual/phone.css" },
};

/** What only a phone shows (its Menu): the desktop projects leave it out. */
const PHONE_ONLY = /@phone-only/;

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
  expect: { toHaveScreenshot: SCREENSHOT },
  use: {
    baseURL: URL,
    locale: "en-GB",
    timezoneId: "UTC",
    reducedMotion: "reduce",
  },
  projects: [
    {
      name: "light",
      testMatch: SCREENS,
      grepInvert: PHONE_ONLY,
      use: { ...devices["Desktop Chrome"], viewport: DESKTOP, colorScheme: "light" },
    },
    {
      name: "dark",
      testMatch: SCREENS,
      grepInvert: PHONE_ONLY,
      use: { ...devices["Desktop Chrome"], viewport: DESKTOP, colorScheme: "dark" },
    },
    {
      name: "phone-light",
      testMatch: SCREENS,
      grep: /@phone/,
      expect: PHONE_EXPECT,
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
      testMatch: SCREENS,
      grep: /@phone/,
      expect: PHONE_EXPECT,
      use: {
        ...devices["Desktop Chrome"],
        viewport: PHONE,
        hasTouch: true,
        isMobile: true,
        colorScheme: "dark",
      },
    },
    // Every picture on disk belongs to a shot: no browser, once per run.
    { name: "baselines", testMatch: "baselines.visual.ts" },
  ],
  webServer: {
    command: "node node_modules/vite/bin/vite.js preview --port 4173 --strictPort",
    url: URL,
  },
});
