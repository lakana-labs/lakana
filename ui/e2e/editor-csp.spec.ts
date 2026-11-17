import { expect, type Page, test } from "@playwright/test";

import { consoleErrors, signInAsAdmin } from "./session";

// CodeMirror's styles come from style-mod at run time. Inside the editor's open shadow root they
// mount as a constructed stylesheet (adoptedStyleSheets), which the content security policy does
// not govern; a <style> element with that text would be refused by style-src. This runs on the
// production build, under the policy, in every engine (Open point 1 of the Queries plan).

/**
 * Records every content security policy violation of the page, from before its first script.
 *
 * @param page - The page.
 */
async function recordViolations(page: Page): Promise<void> {
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
}

/**
 * The violations recorded so far.
 *
 * @param page - The page.
 * @returns Each violation's directive, blocked URI and sample.
 */
async function violations(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { cspViolations: string[] }).cspViolations);
}

/**
 * Opens Home, then the Queries screen from the sidebar, and waits for the editor.
 *
 * @param page - The page.
 * @returns The number of `<style>` elements in the document before the editor mounted.
 */
async function openQueries(page: Page): Promise<number> {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  const styles = await page.evaluate(() => document.querySelectorAll("style").length);
  await page
    .getByRole("navigation", { name: "Areas" })
    .getByRole("link", { name: "Queries" })
    .click();
  await expect(page.getByRole("textbox", { name: "SQL" })).toBeVisible();
  return styles;
}

test("the editor is styled under the content security policy, without a <style> element", async ({
  page,
}) => {
  await recordViolations(page);
  await signInAsAdmin(page);
  const errors = consoleErrors(page);

  const styles = await openQueries(page);
  const editor = page.locator(".cm-editor");

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
  expect(await violations(page)).toEqual([]);
  expect(errors).toEqual([]);
});

// CodeMirror's lint base theme draws the error underline and the gutter marker with data: images,
// which img-src refuses; the Still Water theme replaces them. Until the screen shows a run's error
// (a later task), no diagnostic can be drawn through the interface, so this puts elements with
// lint's own class names where CodeMirror draws them (the marker in the lint gutter, the range
// under the editor) and lets the editor's adopted sheets style them under the real policy.
test("the error underline and the gutter marker draw without an image", async ({ page }) => {
  await recordViolations(page);
  await signInAsAdmin(page);
  const errors = consoleErrors(page);
  await openQueries(page);
  const editor = page.locator(".cm-editor");

  // The base theme's data: rules are mounted, so the replacement is what is being tested.
  const rules = await editor.evaluate((node) =>
    (node.getRootNode() as ShadowRoot).adoptedStyleSheets
      .flatMap((sheet) => [...sheet.cssRules])
      .map((rule) => rule.cssText),
  );
  expect(
    rules.some((rule) => rule.includes("cm-lint-marker-error") && rule.includes("data:")),
  ).toBe(true);

  await editor.evaluate((node) => {
    const cell =
      node.querySelector(".cm-gutter-lint .cm-gutterElement") ??
      node.querySelector(".cm-gutter-lint");
    const marker = document.createElement("div");
    marker.className = "cm-lint-marker cm-lint-marker-error";
    marker.dataset["probe"] = "marker";
    cell?.append(marker);
    const range = document.createElement("span");
    range.className = "cm-lintRange cm-lintRange-error";
    range.dataset["probe"] = "range";
    range.textContent = "gold";
    node.append(range);
  });
  const marker = page.locator("[data-probe=marker]");
  const range = page.locator("[data-probe=range]");
  await expect(marker).toBeVisible();
  await expect(range).toBeVisible();
  // Two frames, so the engine has resolved the styles and asked for any image.
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }),
  );

  const box = await marker.boundingBox();
  expect(box?.width).toBeGreaterThan(0);
  expect(box?.height).toBeGreaterThan(0);
  const markerStyle = await marker.evaluate((node) => {
    const style = getComputedStyle(node);
    return { content: style.content, background: style.backgroundColor };
  });
  expect(markerStyle.content).not.toContain("url(");
  expect(markerStyle.background).not.toBe("rgba(0, 0, 0, 0)");
  const rangeStyle = await range.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      image: style.backgroundImage,
      line: style.textDecorationLine,
      wavy: style.textDecorationStyle,
    };
  });
  expect(rangeStyle).toEqual({ image: "none", line: "underline", wavy: "wavy" });
  expect(await violations(page)).toEqual([]);
  expect(errors).toEqual([]);
});
