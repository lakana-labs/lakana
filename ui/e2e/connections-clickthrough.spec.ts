/**
 * The click-through checks of the Connections screen that the review of M7 parts 4–6 made by
 * hand: where the focus goes as secret keys are added up to the limit and removed again, and the
 * focus ring on every Tab stop of the list, a connection's page and the form. The stubs are those
 * of `connections.ts`.
 *
 * @packageDocumentation
 */
import { expect, type Page, test } from "@playwright/test";

import { EXPORTS, stubConnections } from "./connections";
import { expectRingOnEveryStop } from "./focus-ring";

/**
 * Whether the focus is inside the form of Add or Edit.
 *
 * @param page - The page.
 * @returns True when it is.
 */
function focusInForm(page: Page): Promise<boolean> {
  return page.evaluate(() => document.activeElement?.closest("form") !== null);
}

test.describe("the Connections screen, checked by clicking through", () => {
  test.beforeEach(async ({ page }) => {
    await stubConnections(page);
  });

  test("each Add a key moves the focus to the new name, the sixteenth leaves it in the form with the limit's sentence, and Remove keeps it nearby", async ({
    page,
  }) => {
    await page.goto("/connections/new");
    await page.getByRole("radiogroup", { name: "Kind" }).getByText("TCP", { exact: true }).click();
    for (let row = 1; row <= 16; row += 1) {
      await page.getByRole("button", { name: "Add a key" }).press("Enter");
      await expect(page.getByRole("textbox", { name: `Key ${String(row)} name` })).toBeFocused();
    }
    await expect(page.getByRole("button", { name: "Add a key" })).toHaveCount(0);
    await expect(page.getByText("A connection holds at most 16 secret keys.")).toBeVisible();
    expect(await focusInForm(page), "after the sixteenth key").toBe(true);

    await page.getByRole("button", { name: "Remove key 3" }).press("Enter");
    await expect(page.getByRole("textbox", { name: /^Key \d+ name$/u })).toHaveCount(15);
    expect(await focusInForm(page), "after Remove").toBe(true);
    await expect(page.getByRole("button", { name: "Add a key" })).toBeFocused();
  });

  test("every Tab stop draws a focus ring on the list, a connection's page and the form", async ({
    page,
  }) => {
    await page.goto("/connections");
    await expect(page.getByRole("link", { name: EXPORTS, exact: true })).toBeVisible();
    await expectRingOnEveryStop(page, 10);

    await page.getByRole("link", { name: EXPORTS, exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: EXPORTS })).toBeVisible();
    // From the top of the page, as a person who reloads it.
    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: EXPORTS })).toBeVisible();
    await expectRingOnEveryStop(page, 4);

    await page.goto("/connections/new");
    await page.getByRole("radiogroup", { name: "Kind" }).getByText("TCP", { exact: true }).click();
    await page.getByRole("button", { name: "Add a key" }).click();
    await page.getByRole("textbox", { name: "Name", exact: true }).focus();
    await expectRingOnEveryStop(page, 6);
  });
});
