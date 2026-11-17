/**
 * How a page looks without colour, for screenshots that show a state is not told by colour
 * alone (WCAG 1.4.1). The contrast numbers are checked by Vitest in
 * `src/status/colour-vision.test.ts`; these are for the eye.
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

// The filter is set on the root element's own style through the CSSOM, which the content
// security policy of the production build allows; an added <style> element it refuses.

/**
 * Shows the whole page in greyscale.
 *
 * @param page - The page.
 */
export async function showInGreyscale(page: Page): Promise<void> {
  await page.evaluate(() => {
    document.documentElement.style.filter = "grayscale(1)";
  });
}

/**
 * Shows the page in colour again, after {@link showInGreyscale}.
 *
 * @param page - The page.
 */
export async function showInColour(page: Page): Promise<void> {
  await page.evaluate(() => {
    document.documentElement.style.filter = "";
  });
}
