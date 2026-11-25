import { expect, test } from "@playwright/test";

import { SHOTS } from "./shots";

// Screenshots of every screen of the interface in light and dark, compared with the committed
// baselines in screenshots/ (docs/agent/testing.md, "Visual regression"). Run only inside the
// pinned Playwright image (`npm --prefix ui run visual`), so the fonts and the rendering are the
// same on every machine; `npm --prefix ui run visual:update` writes new baselines, which are
// reviewed like code.

/** The moment every screen is shown at: after the stubs' times, so "ago" reads the same. */
const NOW = Date.parse("2026-11-18T10:30:00Z");

// The connection stubs write their times from the clock of this process ("an hour ago");
// fixed, they give the same answer on every run, as the browser's fixed clock does.
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args: unknown[]) {
    if (args.length === 0) {
      super(NOW);
    } else {
      // @ts-expect-error -- Date's overloads take what the caller gave.
      super(...args);
    }
  }

  static override now(): number {
    return NOW;
  }
}
globalThis.Date = FixedDate as DateConstructor;

for (const shot of SHOTS) {
  // The phone projects of playwright.visual.config.ts take only the shots tagged @phone, the
  // desktop ones every shot but those tagged @phone-only.
  const tag =
    shot.phoneOnly === true ? ["@phone", "@phone-only"] : shot.phone === true ? ["@phone"] : [];
  test(shot.name, { tag }, async ({ page }) => {
    await page.clock.setFixedTime(NOW);
    await shot.stub(page);
    await page.goto(shot.path);
    await expect(shot.ready(page)).toBeVisible({ timeout: 10_000 });
    if (shot.act !== undefined) {
      await shot.act(page);
    }
    // What loads on demand after the data (a place's Actions, a chunk) is in the picture too.
    if (shot.waits !== true) {
      await page.waitForLoadState("networkidle");
    }
    // The fonts are part of the picture.
    await page.evaluate(() => document.fonts.ready);
    // Paint the whole page once more: a heading focused by script while the page was drawn
    // sometimes leaves one pixel of its focus ring behind in dark, which a full repaint clears.
    await page.evaluate(async () => {
      const frames = (): Promise<void> =>
        new Promise((done) => {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              done();
            });
          });
        });
      document.documentElement.style.opacity = "0.999";
      await frames();
      document.documentElement.style.opacity = "";
      await frames();
    });
    await expect(page).toHaveScreenshot(`${shot.name}.png`, { fullPage: true });
  });
}
