/**
 * The content security policy's violations of a page, recorded from before its first script:
 * the production build runs under the policy, and the Queries screen's editor must draw within
 * it (stubbed and against a real server).
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

/**
 * Records every content security policy violation of the page, from before its first script.
 *
 * @param page - The page.
 */
export async function recordCspViolations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const seen: string[] = [];
    Object.assign(window, { cspViolations: seen });
    document.addEventListener(
      "securitypolicyviolation",
      (event) => {
        seen.push(
          `${event.violatedDirective}: ${event.blockedURI} ${event.sample} at ${event.sourceFile}:${String(event.lineNumber)}:${String(event.columnNumber)}`,
        );
      },
      true,
    );
  });
}

/**
 * The content security policy violations recorded so far by {@link recordCspViolations}.
 *
 * @param page - The page.
 * @returns Each violation's directive, blocked URI and sample.
 */
export async function cspViolations(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { cspViolations: string[] }).cspViolations);
}
