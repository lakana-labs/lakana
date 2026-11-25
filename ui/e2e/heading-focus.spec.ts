import { expect, type Page, test } from "@playwright/test";

import { SHOTS } from "./visual/shots";

// #821: a heading the interface focuses by script (a sign-in step's, a page's after a navigation,
// a panel's after an action) shows the focus ring, since no pointer was used. The ring stands
// clear of every line of text around the heading.

/** Screens with headings focused by script: every kind of them, one screen each at least. */
const SCREENS = [
  "sign-in",
  "sign-in-passkeys-only",
  "sign-in-oidc-finishing",
  "sign-in-oidc-refused",
  "sign-in-oidc-confirming",
  "sign-in-oidc-confirm-refused",
  "sign-in-invite",
  "sign-in-recovery",
  "sign-in-link-used",
  "sign-in-cli",
  "sign-in-device",
  "home",
  "home-no-access",
  "catalog",
  "table-overview",
  "table-health",
  "table-lineage",
  "jobs",
  "run",
  "connection",
  "settings-environments",
  "settings-repository",
  "settings-people",
  "settings-person",
  "settings-group",
  "settings-grants",
  "settings-what-i-may-do",
  "settings-tokens",
  "account",
] as const;

/** A heading whose ring touches text: where, and the text it touches. */
interface Touch {
  readonly heading: string;
  readonly text: string;
}

/**
 * Focuses each heading the interface may focus by script (`tabindex="-1"`) and returns those
 * whose focus ring (the box grown by the outline's offset and width) meets a line of text
 * outside the heading. Fails when a heading shows no ring at all, which would make the check
 * pass for nothing.
 *
 * @param page - The page.
 * @returns The headings whose ring touches text, and how many headings were checked.
 */
function ringsOverText(page: Page): Promise<{ checked: number; touches: Touch[] }> {
  return page.evaluate(async () => {
    const frame = (): Promise<unknown> =>
      new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    // The lines of text outside a heading, read at the moment its ring is: a block that loads
    // later (Health, a browser's passkeys) moves the lines under it.
    const linesOutside = (heading: HTMLElement): { text: string; rects: DOMRect[] }[] => {
      const lines: { text: string; rects: DOMRect[] }[] = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
        const text = node.textContent?.trim() ?? "";
        if (text === "" || heading.contains(node)) {
          continue;
        }
        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = [...range.getClientRects()].filter((rect) => rect.width > 0);
        lines.push({ text: text.slice(0, 40), rects });
      }
      return lines;
    };
    const headings = [
      ...document.querySelectorAll<HTMLElement>(
        "h1[tabindex='-1'], h2[tabindex='-1'], h3[tabindex='-1']",
      ),
    ].filter((heading) => heading.getClientRects().length > 0);
    const touches: { heading: string; text: string }[] = [];
    for (const heading of headings) {
      heading.focus();
      await frame();
      const style = getComputedStyle(heading);
      if (!heading.matches(":focus-visible") || style.outlineStyle === "none") {
        touches.push({ heading: heading.textContent.trim(), text: "(no ring shown)" });
        continue;
      }
      const grow = Number.parseFloat(style.outlineOffset) + Number.parseFloat(style.outlineWidth);
      const box = heading.getBoundingClientRect();
      const ring = {
        left: box.left - grow,
        right: box.right + grow,
        top: box.top - grow,
        bottom: box.bottom + grow,
      };
      for (const { text, rects } of linesOutside(heading)) {
        const meets = rects.some(
          (rect) =>
            rect.left < ring.right &&
            rect.right > ring.left &&
            rect.top < ring.bottom &&
            rect.bottom > ring.top,
        );
        if (meets) {
          touches.push({ heading: heading.textContent.trim().slice(0, 40), text });
        }
      }
    }
    return { checked: headings.length, touches };
  });
}

for (const name of SCREENS) {
  test(`${name}: a heading focused by script keeps its ring clear of the text around it`, async ({
    page,
  }) => {
    const shot = SHOTS.find((each) => each.name === name);
    if (shot === undefined) {
      throw new Error(`no shot ${name}`);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await shot.stub(page);
    await page.goto(shot.path);
    await expect(shot.ready(page)).toBeVisible();
    // A loading step shows its status before its heading: wait for the heading as well.
    await expect(
      page.locator("h1[tabindex='-1'], h2[tabindex='-1'], h3[tabindex='-1']").first(),
    ).toBeVisible();
    // Measured in the bundled fonts, not the fallback they swap from (as the visual suite does).
    await page.evaluate(() => document.fonts.ready);

    const { checked, touches } = await ringsOverText(page);
    expect(checked, "headings that take the focus by script").toBeGreaterThan(0);
    expect(touches).toEqual([]);
  });
}
