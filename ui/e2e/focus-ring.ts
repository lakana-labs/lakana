/**
 * A keyboard walk that checks the focus ring: Tab through a page and, at each stop, require that
 * the focused element draws a ring a person can see (an outline or a box shadow, on the element
 * or on the pseudo-element a custom control draws its ring on).
 *
 * @packageDocumentation
 */
import { expect, type Page } from "@playwright/test";

/** What the walk found at one Tab stop. */
interface Stop {
  /** The element, as `tag.class "text"`. */
  readonly where: string;
  /** Whether it draws a ring. */
  readonly ring: boolean;
  /** Whether the focus left the page for the browser's own controls. */
  readonly body: boolean;
}

/**
 * Reads the focused element and whether it draws a ring. The SQL editor's text box lives in a
 * shadow root; its ring is the editor's own outline, which it draws a frame after the focus
 * arrives.
 *
 * @param page - The page.
 * @returns The stop.
 */
function readStop(page: Page): Promise<Stop> {
  return page.evaluate(async () => {
    // The editor marks itself focused on the next frame, after the focus event.
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    let active: Element | null = document.activeElement;
    while (active?.shadowRoot?.activeElement) {
      active = active.shadowRoot.activeElement;
    }
    if (active === null || active === document.body) {
      return { where: "body", ring: false, body: true };
    }
    const drawn = (style: CSSStyleDeclaration): boolean =>
      (style.outlineStyle !== "none" && Number.parseFloat(style.outlineWidth) > 0) ||
      style.boxShadow !== "none";
    const editor = active.closest(".cm-editor");
    const target = editor ?? active;
    const ring =
      drawn(getComputedStyle(target)) ||
      drawn(getComputedStyle(target, "::before")) ||
      drawn(getComputedStyle(target, "::after"));
    const text = target.textContent.trim().slice(0, 40);
    const name = `${target.tagName.toLowerCase()}.${[...target.classList].join(".")}`;
    return { where: `${name} "${text}"`, ring, body: false };
  });
}

/**
 * Tabs from where the focus is to the end of the page (where the focus leaves it for the
 * browser's own controls) and requires a visible ring at every stop on the way.
 *
 * @param page - The page.
 * @param least - How many stops the page has at least, so a walk that ends early fails.
 */
export async function expectRingOnEveryStop(page: Page, least: number): Promise<void> {
  const missing: string[] = [];
  let stops = 0;
  for (; stops < 200; stops += 1) {
    await page.keyboard.press("Tab");
    const stop = await readStop(page);
    if (stop.body) {
      break;
    }
    if (!stop.ring) {
      missing.push(stop.where);
    }
  }
  expect(stops, "Tab stops before the end of the page").toBeGreaterThanOrEqual(least);
  expect(missing, "Tab stops without a visible focus ring").toEqual([]);
}
