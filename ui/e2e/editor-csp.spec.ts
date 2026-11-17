import { expect, test } from "@playwright/test";

import { consoleErrors, signInAsAdmin } from "./session";

// CodeMirror's styles come from style-mod at run time. Inside the editor's open shadow root they
// mount as a constructed stylesheet (adoptedStyleSheets), which the content security policy does
// not govern; a <style> element with that text would be refused by style-src. This runs on the
// production build, under the policy, in every engine (Open point 1 of the Queries plan).
test("the editor is styled under the content security policy, without a <style> element", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const seen: string[] = [];
    Object.assign(window, { cspViolations: seen });
    document.addEventListener(
      "securitypolicyviolation",
      (event) => {
        seen.push(`${event.violatedDirective}: ${event.blockedURI} ${event.sample}`);
      },
      true,
    );
  });
  await signInAsAdmin(page);
  const errors = consoleErrors(page);

  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  const styles = await page.evaluate(() => document.querySelectorAll("style").length);

  await page
    .getByRole("navigation", { name: "Areas" })
    .getByRole("link", { name: "Queries" })
    .click();
  const editor = page.locator(".cm-editor");
  await expect(page.getByRole("textbox", { name: "SQL" })).toBeVisible();

  expect(await editor.evaluate((node) => getComputedStyle(node).display)).toBe("flex");
  expect(
    await editor.evaluate((node) => (node.getRootNode() as ShadowRoot).adoptedStyleSheets.length),
  ).toBeGreaterThan(0);
  expect(
    await editor.evaluate(
      (node) => (node.getRootNode() as ShadowRoot).querySelectorAll("style").length,
    ),
  ).toBe(0);
  expect(await page.evaluate(() => document.querySelectorAll("style").length)).toBe(styles);
  expect(
    await page.evaluate(() => (window as unknown as { cspViolations: string[] }).cspViolations),
  ).toEqual([]);
  expect(errors).toEqual([]);
});
