import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  addAdminPasskey,
  MANAGE_STUBBED,
  ORIGIN,
  stubGitOwned,
  stubManagement,
  stubStepUpDrop,
} from "./manage";
import { consoleErrors, tabTo, TABLE_PATH, violations } from "./session";

const KEY = "lakana.preferences";
const TABLE = "/catalog/prod/gold.sales/fact_sales";
const DELETE_TABLE = `DELETE ${TABLE_PATH}`;

/** The three lists the catalog adds, with the region each shows once it has read its rows. */
const VIEWS = [
  ["dropped", "Dropped tables"],
  ["expiring", "Expiring tables"],
  ["orphans", "Orphans"],
] as const;

/**
 * Opens the table's page and waits for its heading and the Actions button.
 *
 * @param page - The page.
 * @param tab - The tab's path after the table; Overview by default.
 * @returns The Actions button.
 */
async function openTable(page: Page, tab = ""): Promise<Locator> {
  await page.goto(`${TABLE}${tab}`);
  await expect(page.getByRole("heading", { level: 1, name: "fact_sales" })).toBeVisible();
  const actions = page.getByRole("button", { name: "Actions" });
  await expect(actions).toBeVisible();
  return actions;
}

/**
 * Opens one of the catalog's views of `prod` and waits for its region.
 *
 * @param page - The page.
 * @param view - The view and its region's name.
 * @param extra - More of the address's query, such as `&ns=gold.sales`.
 */
async function openView(page: Page, view: (typeof VIEWS)[number], extra = ""): Promise<void> {
  await page.goto(`/catalog?env=prod${extra}&view=${view[0]}`);
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
  await expect(page.getByRole("region", { name: view[1] })).toBeVisible();
}

/**
 * Opens a menu item of the Actions menu.
 *
 * @param page - The page.
 * @param item - The item's name.
 */
async function chooseAction(page: Page, item: string): Promise<void> {
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

/**
 * How far the page is wider than the window.
 *
 * @param page - The page.
 * @returns The overflow in pixels; zero or less when nothing scrolls sideways.
 */
function sidewaysOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`table actions and views, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await stubManagement(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on the open menu, each dialog, each view and the References tab", async ({
        page,
      }) => {
        const actions = await openTable(page);
        await actions.click();
        await expect(page.getByRole("menu")).toBeVisible();
        expect(await violations(page), "the Actions menu").toEqual([]);
        await page.keyboard.press("Escape");

        await chooseAction(page, "Drop…");
        const drop = page.getByRole("dialog", { name: "Drop gold.sales.fact_sales?" });
        await expect(drop.getByRole("button", { name: "Drop the table" })).toBeEnabled();
        expect(await violations(page), "the Drop dialog").toEqual([]);
        await drop.getByRole("button", { name: "Cancel" }).click();

        await chooseAction(page, "Maintain now…");
        const maintain = page.getByRole("dialog", { name: "Maintain gold.sales.fact_sales now" });
        await expect(
          maintain.getByRole("textbox", { name: /Keep snapshots younger/ }),
        ).toBeVisible();
        expect(await violations(page), "the Maintain now form").toEqual([]);
        await maintain.getByRole("button", { name: "Maintain now" }).click();
        await expect(maintain.getByText("Maintenance is queued as query q-77.")).toBeVisible();
        expect(await violations(page), "the queued maintenance").toEqual([]);
        await maintain.getByRole("button", { name: "Close" }).click();

        await chooseAction(page, "Retention…");
        const retention = page.getByRole("dialog", { name: "Retention of gold.sales.fact_sales" });
        await expect(retention.getByText(/30 days after its last activity/)).toBeVisible();
        expect(await violations(page), "the Retention form").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(retention).toBeHidden();

        for (const view of VIEWS) {
          await openView(page, view);
          expect(await violations(page), `the ${view[0]} view`).toEqual([]);
        }
        await openView(page, VIEWS[0]);
        await page.getByRole("button", { name: "Undrop gold.sales.fact_returns" }).click();
        await expect(page.getByRole("status").filter({ hasText: "Restored" })).toBeVisible();
        expect(await violations(page), "the Dropped view after an Undrop").toEqual([]);

        await openTable(page, "/references");
        await expect(
          page.getByRole("heading", { level: 2, name: "This table references" }),
        ).toBeVisible();
        expect(await violations(page), "the References tab").toEqual([]);
      });

      test("axe finds nothing on the open menu and the Rebuild dialog of a table git defines", async ({
        page,
      }) => {
        await stubGitOwned(page);
        const actions = await openTable(page);
        await actions.click();
        await expect(page.getByRole("menuitem", { name: "Drop…" })).toHaveAttribute(
          "aria-disabled",
          "true",
        );
        expect(await violations(page), "the open menu of a git-owned table").toEqual([]);
        await page.keyboard.press("Escape");

        await chooseAction(page, "Rebuild from git…");
        const dialog = page.getByRole("dialog", {
          name: "Rebuild gold.sales.fact_sales from git?",
        });
        await expect(dialog.getByRole("textbox", { name: /Why it is rebuilt/ })).toBeVisible();
        expect(await violations(page), "the Rebuild dialog").toEqual([]);
        await dialog.getByRole("textbox", { name: /Why it is rebuilt/ }).fill("a bad load");
        await dialog.getByRole("button", { name: "Rebuild" }).click();
        await expect(dialog.getByText(/queued run run-42 of analytics/)).toBeVisible();
        expect(await violations(page), "the rebuilt dialog").toEqual([]);
      });

      test("the menu, each dialog and each view work by keyboard alone; focus returns to Actions or lands on the result line", async ({
        page,
      }) => {
        const actions = await openTable(page);
        for (const [item, title] of [
          ["Maintain now…", "Maintain gold.sales.fact_sales now"],
          ["Retention…", "Retention of gold.sales.fact_sales"],
          ["Drop…", "Drop gold.sales.fact_sales?"],
        ] as const) {
          await tabTo(page, actions);
          await page.keyboard.press("Enter");
          const entry = page.getByRole("menuitem", { name: item });
          await expect(entry).toBeVisible();
          for (let step = 0; step < 4; step += 1) {
            if (await entry.evaluate((node) => node === document.activeElement)) {
              break;
            }
            await page.keyboard.press("ArrowDown");
          }
          await expect(entry).toBeFocused();
          await page.keyboard.press("Enter");
          const dialog = page.getByRole("dialog", { name: title });
          await expect(dialog).toBeVisible();
          await page.keyboard.press("Escape");
          await expect(dialog).toBeHidden();
          await expect(actions).toBeFocused();
        }

        await page.goto("/catalog?env=prod");
        const views = page.getByRole("navigation", { name: "Views of the catalog" });
        await expect(views).toBeVisible();
        await tabTo(page, views.getByRole("link", { name: "Dropped" }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/view=dropped/);
        await expect(page.getByRole("region", { name: "Dropped tables" })).toBeVisible();
        await tabTo(page, page.getByRole("button", { name: "Undrop gold.sales.fact_returns" }));
        await page.keyboard.press("Enter");
        const result = page.locator(".catalog-result", { hasText: "Restored" });
        await expect(result).toContainText("Restored gold.sales.fact_returns.");
        await expect(result).toBeFocused();

        await tabTo(page, views.getByRole("link", { name: "Expiring" }));
        await page.keyboard.press("Enter");
        await expect(page.getByRole("region", { name: "Expiring tables" })).toBeVisible();
        await tabTo(page, views.getByRole("link", { name: "Orphans" }));
        await page.keyboard.press("Enter");
        await expect(page.getByRole("region", { name: "Orphans" })).toBeVisible();
        await tabTo(page, page.getByRole("button", { name: "Drop silver.clean" }));
        await page.keyboard.press("Enter");
        const dialog = page.getByRole("dialog", { name: "Drop silver.clean?" });
        await expect(dialog).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
      });

      test("a git-owned table shows Drop disabled with its sentence, and a screen reader hears it on opening", async ({
        page,
      }) => {
        await stubGitOwned(page);
        const actions = await openTable(page);
        await actions.click();
        const menu = page.getByRole("menu");
        await expect(menu).toBeVisible();
        const note = await menu.getAttribute("aria-describedby");
        expect(note, "the menu is described").not.toBeNull();
        const sentence = page.locator(`[id="${note ?? ""}"]`);
        await expect(sentence).toContainText("models/gold/fact_sales.sql");
        await expect(sentence).toContainText("Remove it there and deploy");
        await expect(page.getByRole("menuitem", { name: "Drop…" })).toHaveAttribute(
          "aria-disabled",
          "true",
        );
        await expect(page.getByRole("menuitem", { name: "Rebuild from git…" })).toBeVisible();
      });

      test("no horizontal scroll at 320 px in each view; the lists scroll in their own focusable region", async ({
        page,
      }) => {
        await page.setViewportSize({ width: 320, height: 640 });
        for (const view of VIEWS) {
          await openView(page, view);
          expect(await sidewaysOverflow(page), `the ${view[0]} view`).toBeLessThanOrEqual(0);
          const region = page.getByRole("region", { name: view[1] });
          await expect(region).toHaveAttribute("tabindex", "0");
          const sizes = await region.evaluate((node) => [node.scrollWidth, node.clientWidth]);
          expect(sizes[0], `${view[0]} scrolls in its region`).toBeGreaterThan(sizes[1] ?? 0);
          expect(await violations(page), `the ${view[0]} view at 320 px`).toEqual([]);
        }
        await openTable(page, "/references");
        await expect(page.getByRole("heading", { level: 2, name: "Referenced by" })).toBeVisible();
        expect(await sidewaysOverflow(page), "References").toBeLessThanOrEqual(0);
        expect(await violations(page), "References at 320 px").toEqual([]);
      });

      test("the views ask only for stubbed routes and log no console error", async ({ page }) => {
        const errors = consoleErrors(page);
        const requests: string[] = [];
        page.on("request", (request) => requests.push(request.url()));

        await openTable(page);
        await chooseAction(page, "Maintain now…");
        await expect(page.getByRole("dialog", { name: /Maintain/ })).toBeVisible();
        await page.keyboard.press("Escape");
        await chooseAction(page, "Retention…");
        await expect(page.getByText(/30 days after its last activity/)).toBeVisible();
        await page.keyboard.press("Escape");
        for (const view of VIEWS) {
          await openView(page, view, view[0] === "dropped" ? "&ns=gold.sales" : "");
        }
        await openTable(page, "/references");
        await expect(
          page.getByRole("heading", { level: 2, name: "This table references" }),
        ).toBeVisible();
        await page.waitForLoadState("networkidle");

        const api = requests
          .filter((url) => url.includes("/api/"))
          .map((url) => new URL(url).pathname);
        expect(api.length).toBeGreaterThan(0);
        expect(api.filter((path) => !MANAGE_STUBBED.includes(path))).toEqual([]);
        expect(errors).toEqual([]);
      });

      test("Back and Forward move between the views, keeping the place", async ({ page }) => {
        await page.goto("/catalog?env=prod&ns=gold.sales");
        const views = page.getByRole("navigation", { name: "Views of the catalog" });
        await expect(views).toBeVisible();
        await views.getByRole("link", { name: "Dropped" }).click();
        await expect(page).toHaveURL(/env=prod&ns=gold\.sales&view=dropped/);
        await expect(page.getByRole("region", { name: "Dropped tables" })).toBeVisible();
        await views.getByRole("link", { name: "Expiring" }).click();
        await expect(page).toHaveURL(/env=prod&ns=gold\.sales&view=expiring/);
        await expect(page.getByRole("region", { name: "Expiring tables" })).toBeVisible();
        await views.getByRole("link", { name: "Orphans" }).click();
        await expect(page.getByRole("region", { name: "Orphans" })).toBeVisible();

        await page.goBack();
        await expect(page).toHaveURL(/view=expiring/);
        await expect(page.getByRole("region", { name: "Expiring tables" })).toBeVisible();
        await page.goBack();
        await expect(page).toHaveURL(/view=dropped/);
        await expect(page.getByRole("region", { name: "Dropped tables" })).toBeVisible();
        await page.goBack();
        await expect(page).toHaveURL(/\/catalog\?env=prod&ns=gold\.sales$/);
        await page.goForward();
        await expect(page).toHaveURL(/view=dropped/);
        await page.goForward();
        await page.goForward();
        await expect(page).toHaveURL(/view=orphans/);
        await expect(page.getByRole("region", { name: "Orphans" })).toBeVisible();
      });
    });
  }
}

test.describe("Drop with the passkey in real Chromium", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "the virtual authenticator is CDP");

  test("Drop asks for the passkey touch, sends again and lands on Dropped with the table", async ({
    page,
    context,
  }) => {
    await stubManagement(page);
    await addAdminPasskey(context, page);
    const sent = await stubStepUpDrop(page);

    await page.goto(`${ORIGIN}${TABLE}`);
    await chooseAction(page, "Drop…");
    const dialog = page.getByRole("dialog", { name: "Drop gold.sales.fact_sales?" });
    await dialog.getByRole("button", { name: "Drop the table" }).click();

    await expect(page).toHaveURL(`${ORIGIN}/catalog?env=prod&ns=gold.sales&view=dropped`);
    await expect(page.getByText("Dropped gold.sales.fact_sales.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Undrop gold.sales.fact_sales" })).toBeVisible();
    expect(sent).toEqual([
      DELETE_TABLE,
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      DELETE_TABLE,
    ]);
  });
});
