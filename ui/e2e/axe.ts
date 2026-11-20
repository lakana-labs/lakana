/**
 * axe as every Playwright test runs it: the WCAG 2.2 AA tags, without React Aria's live
 * announcer, and in Firefox without ARIA element reflection.
 *
 * @packageDocumentation
 */
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

/** What axe found. */
type AxeResults = Awaited<ReturnType<AxeBuilder["analyze"]>>;

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

// React Aria's live announcer (a pending button speaks through it) keeps its message for 7 s and
// names it by the ids of the button's parts, which may be gone by then; it is the library's
// transient node, not the page.
const LIVE_ANNOUNCER = "[data-live-announcer]";

// The properties of ARIA element reflection, one for each attribute that names other elements by
// id. axe 4.14 reads them before the attributes, and Playwright's Firefox (159) crashes the page on
// such a read for an element React rendered ("frame.evaluate: Page crashed" on
// `button.ariaLabelledByElements`, #700). Answering null sends axe to the attributes, which say
// the same here: the interface sets no element references from script.
const REFLECTED = [
  "ariaActionsElements",
  "ariaActiveDescendantElement",
  "ariaControlsElements",
  "ariaDescribedByElements",
  "ariaDetailsElements",
  "ariaErrorMessageElements",
  "ariaFlowToElements",
  "ariaLabelledByElements",
  "ariaOwnsElements",
];

/**
 * Runs axe over the page with the WCAG 2.2 AA tags, without the live announcer. In Firefox the
 * page's element reflection answers null first (see `REFLECTED`).
 *
 * @param page - The page to check.
 * @returns What axe found.
 */
export async function runAxe(page: Page): Promise<AxeResults> {
  if (page.context().browser()?.browserType().name() === "firefox") {
    await page.evaluate((names) => {
      for (const name of names) {
        if (name in Element.prototype) {
          Object.defineProperty(Element.prototype, name, { configurable: true, get: () => null });
        }
      }
    }, REFLECTED);
  }
  return new AxeBuilder({ page }).withTags(WCAG).exclude(LIVE_ANNOUNCER).analyze();
}
