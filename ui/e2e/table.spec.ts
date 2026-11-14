import { expect, type Locator, type Page, test } from "@playwright/test";

import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const TABLE = "/catalog/prod/gold.sales/fact_sales";
const RUN = "/api/v1/environments/prod/queries/5b0e7c1a-3d2f-4e6a-9b8c-7d6e5f4a3b2c";

/**
 * Opens a tab of the table's page and waits for its heading and its tabs.
 *
 * @param page - The page.
 * @param tab - The tab's path after the table, such as `/columns`; Overview by default.
 */
async function openTable(page: Page, tab = ""): Promise<void> {
  await page.goto(`${TABLE}${tab}`);
  await expect(page.getByRole("heading", { level: 1, name: "fact_sales" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Sections of the table" })).toBeVisible();
}

/** The tabs after Preview, with what shows once each has read its data. */
const NEW_TABS = [
  ["/lineage", "region", "Lineage of fact_sales"],
  ["/health", "heading", "Checks"],
  ["/access", "heading", "Column masks"],
] as const;

/**
 * The tab's link name: "Lineage" for `/lineage`.
 *
 * @param entry - The tab.
 * @returns The name.
 */
function tabName(entry: (typeof NEW_TABS)[number]): string {
  return entry[0].charAt(1).toUpperCase() + entry[0].slice(2);
}

/**
 * Opens one of the Lineage, Health and Access tabs and waits until it shows what it read.
 *
 * @param page - The page.
 * @param entry - The tab, the role of what to wait for and its name.
 */
async function openNewTab(page: Page, entry: (typeof NEW_TABS)[number]): Promise<void> {
  const [tab, role, name] = entry;
  await openTable(page, tab);
  await expect(page.getByRole(role, { name, exact: true })).toBeVisible();
  if (tab === "/access") {
    await expect(page.getByRole("cell", { name: "md5(customer_email)" })).toBeVisible();
  }
}

/**
 * The preview's grid, once the rows have been read.
 *
 * @param page - The page.
 * @returns The grid.
 */
async function previewGrid(page: Page): Promise<Locator> {
  const grid = page.getByRole("grid", { name: "The first rows of fact_sales" });
  await expect(grid).toBeVisible();
  return grid;
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
    test.describe(`the table page, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await signInAsAdmin(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on Overview, Columns, Preview, the editor and the file dialog", async ({
        page,
      }) => {
        for (const tab of ["", "/columns", "/preview"]) {
          await openTable(page, tab);
          if (tab === "/preview") {
            const grid = await previewGrid(page);
            // The Value panel, with a long note that scrolls in it.
            await grid.getByText(/Delivered to the back door/).click();
            await expect(page.getByRole("region", { name: "note, row 1" })).toBeVisible();
          }
          expect(await violations(page), `the table page${tab}`).toEqual([]);
        }

        for (const entry of NEW_TABS) {
          await openNewTab(page, entry);
          expect(await violations(page), `the table page${entry[0]}`).toEqual([]);
        }

        await openTable(page);
        await page.getByRole("button", { name: "Edit", exact: true }).click();
        await expect(page.getByRole("textbox", { name: "Label" })).toBeFocused();
        expect(await violations(page), "the editor on Overview").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeFocused();

        await openTable(page, "/columns");
        await page.getByRole("button", { name: "Edit store_id" }).click();
        await expect(page.getByRole("textbox", { name: "Label" })).toBeFocused();
        expect(await violations(page), "the editor under a column").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(page.getByRole("button", { name: "Edit store_id" })).toBeFocused();

        await openTable(page);
        const path = page.getByRole("button", { name: /models\/gold\/fact_sales\.sql/ });
        await path.click();
        const dialog = page.getByRole("dialog", { name: "Where fact_sales is defined" });
        await expect(dialog.getByRole("region", { name: "The file's text" })).toBeVisible();
        expect(await violations(page), "the file dialog").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
        await expect(path).toBeFocused();
      });

      test("the six tabs wrap at 320 px and every tab fits and passes axe", async ({ page }) => {
        await page.setViewportSize({ width: 320, height: 640 });
        await openTable(page);
        const tabs = page.getByRole("navigation", { name: "Sections of the table" });
        const boxes = await tabs
          .getByRole("link")
          .evaluateAll((links) => links.map((link) => link.getBoundingClientRect()));
        expect(boxes).toHaveLength(6);
        // Wrapped: more than one line, and every tab inside the window.
        expect(new Set(boxes.map((box) => Math.round(box.top))).size).toBeGreaterThan(1);
        expect(Math.max(...boxes.map((box) => box.right))).toBeLessThanOrEqual(320);
        expect(Math.min(...boxes.map((box) => box.left))).toBeGreaterThanOrEqual(0);
        for (const entry of NEW_TABS) {
          await openNewTab(page, entry);
          expect(await sidewaysOverflow(page), `${entry[0]} sideways`).toBeLessThanOrEqual(0);
          expect(await violations(page), `${entry[0]} at 320 px`).toEqual([]);
        }
      });

      test("Overview fits 1280 × 800 without scrolling for the typical table", async ({ page }) => {
        await page.setViewportSize({ width: 1280, height: 800 });
        await openTable(page);
        await expect(
          page.getByRole("button", { name: /models\/gold\/fact_sales\.sql/ }),
        ).toBeVisible();
        await expect(page.getByText("Check unique_sale_id failed")).toBeVisible();
        const room = await page.evaluate(() => {
          const scrolling = [document.documentElement, ...document.querySelectorAll("main, body")];
          return scrolling.map((node) => node.scrollHeight - node.clientHeight);
        });
        expect(Math.max(...room), JSON.stringify(room)).toBeLessThanOrEqual(0);
      });
    });
  }
}

test.describe("the table page in real Chromium", () => {
  test.beforeEach(async ({ page }) => {
    await signInAsAdmin(page);
  });

  test("the tabs, Edit, Save, the file dialog and Preview's grid work by keyboard alone", async ({
    page,
  }) => {
    await openTable(page);
    const tabs = page.getByRole("navigation", { name: "Sections of the table" });
    await tabTo(page, tabs.getByRole("link", { name: "Columns", exact: true }));
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/fact_sales\/columns$/);
    await expect(page.getByRole("region", { name: "Table columns" })).toBeVisible();
    await page.keyboard.press("Shift+Tab");
    await tabTo(page, tabs.getByRole("link", { name: "Overview" }));
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/fact_sales$/);

    // Edit, type, Save: the stubbed table answers the change.
    const edit = page.getByRole("button", { name: "Edit", exact: true });
    await tabTo(page, edit);
    await page.keyboard.press("Enter");
    await expect(page.getByRole("textbox", { name: "Label" })).toBeFocused();
    await page.keyboard.press("Tab");
    const description = page.getByRole("textbox", { name: "Description" });
    await expect(description).toBeFocused();
    await page.keyboard.type(" Edited by keyboard.");
    const saved = page.waitForRequest(
      (request) => request.method() === "PATCH" && request.url().includes("/tables/fact_sales"),
    );
    await tabTo(page, page.getByRole("button", { name: "Save", exact: true }));
    await page.keyboard.press("Enter");
    expect((await saved).postDataJSON()).toMatchObject({
      description: expect.stringContaining("Edited by keyboard."),
    });
    await expect(edit).toBeVisible();

    // The file dialog opens with Enter, and Escape returns the focus to the path.
    const path = page.getByRole("button", { name: /models\/gold\/fact_sales\.sql/ });
    await tabTo(page, path);
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Where fact_sales is defined" });
    await expect(dialog.getByRole("region", { name: "The file's text" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(path).toBeFocused();

    // The grid is one Tab stop, and the arrow keys move inside it.
    await openTable(page, "/preview");
    const grid = await previewGrid(page);
    // The grid itself is the stop; focus moves to a cell inside it, and no cell is a stop.
    const stops = page.locator("[role=grid][tabindex='0'], [role=grid] [tabindex='0']");
    await expect(stops).toHaveCount(1);
    for (let step = 0; step < 40; step += 1) {
      if (await grid.evaluate((node) => node.contains(document.activeElement))) {
        break;
      }
      await page.keyboard.press("Tab");
    }
    const inside = grid.locator(":focus");
    await expect(inside).toHaveCount(1);
    const first = await inside.textContent();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowRight");
    await expect(inside).toHaveCount(1);
    expect(await inside.textContent()).not.toBe(first);
    await expect(page.locator(".preview-value")).toBeVisible();
    await expect(stops).toHaveCount(1);
    await page.keyboard.press("Tab");
    expect(await grid.evaluate((node) => node.contains(document.activeElement))).toBe(false);
  });

  test("no horizontal scroll at 320 px on every tab; wide tables scroll in their own focusable region", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 320, height: 640 });
    for (const tab of ["", "/columns", "/preview"]) {
      await openTable(page, tab);
      if (tab === "/preview") {
        await previewGrid(page);
      }
      expect(await sidewaysOverflow(page), `the page${tab}`).toBeLessThanOrEqual(0);
    }
    for (const entry of NEW_TABS) {
      await openNewTab(page, entry);
      expect(await sidewaysOverflow(page), `the page${entry[0]}`).toBeLessThanOrEqual(0);
    }
    // The drawing is wider than 320 px; it scrolls in its own focusable region.
    await openNewTab(page, NEW_TABS[0]);
    const drawing = page.getByRole("region", { name: "Lineage of fact_sales", exact: true });
    await expect(drawing).toHaveAttribute("tabindex", "0");
    const drawn = await drawing.evaluate((node) => [node.scrollWidth, node.clientWidth]);
    expect(drawn[0], "the lineage drawing").toBeGreaterThan(drawn[1] ?? 0);
    for (const [tab, name] of [
      ["/columns", "Table columns"],
      ["/preview", "Preview of fact_sales"],
    ] as const) {
      await openTable(page, tab);
      const region = page.getByRole("region", { name });
      await expect(region).toBeVisible();
      // Columns' region is the stop; Preview's is reached through the grid's one stop inside it.
      await expect(tab === "/columns" ? region : region.locator("[tabindex='0']")).toHaveAttribute(
        "tabindex",
        "0",
      );
      const sizes = await region.evaluate((node) => [node.scrollWidth, node.clientWidth]);
      expect(sizes[0], name).toBeGreaterThan(sizes[1] ?? 0);
      expect(await violations(page), `${name} at 320 px`).toEqual([]);
    }
  });

  test("the file view shows markup as text and marks a bidi character", async ({ page }) => {
    const errors = consoleErrors(page);
    await openTable(page);
    await page.getByRole("button", { name: /models\/gold\/fact_sales\.sql/ }).click();
    const text = page.getByRole("region", { name: "The file's text" });
    await expect(text).toContainText('<img src=x onerror="window.pwned = 1"> <b>bold</b>');
    await expect(text.locator("img, b")).toHaveCount(0);
    await expect(text.locator(".char-mark")).toHaveText("U+202E");
    expect(await page.evaluate(() => "pwned" in window)).toBe(false);
    expect(errors).toEqual([]);
  });

  test("a preview waits, shows its rows and masked columns, and cancels when the tab is left", async ({
    page,
  }) => {
    // The default stub is queued at the first poll and done at the second.
    await openTable(page, "/preview");
    await expect(page.getByRole("status")).toContainText("Queued");
    const grid = await previewGrid(page);
    await expect(page.getByRole("status")).toContainText("2 rows · read as you");
    await expect(page.getByText("Masked for you: customer_email")).toBeVisible();
    await expect(grid.getByRole("columnheader")).toHaveText([
      "sale_id",
      "customer_email",
      "note",
      "receipt",
    ]);
    await expect(grid.getByText("binary, 12 bytes")).toBeVisible();

    // A run that never leaves the queue is cancelled when the person goes to another tab.
    await page.route(
      (url) => url.pathname === RUN,
      (route) => route.fulfill({ json: { query_id: RUN.split("/").at(-1), status: "QUEUED" } }),
    );
    await openTable(page, "/preview");
    await expect(page.getByRole("status")).toContainText("Queued");
    const cancelled = page.waitForRequest(
      (request) => request.method() === "POST" && request.url().endsWith(`${RUN}/cancel`),
    );
    await page.getByRole("link", { name: "Columns", exact: true }).click();
    await cancelled;
    await expect(page.getByRole("region", { name: "Table columns" })).toBeVisible();
  });

  test("hovering a cell changes nothing and a click shows the cell whole in the Value panel", async ({
    page,
  }) => {
    await openTable(page, "/preview");
    const grid = await previewGrid(page);
    const hint = page.getByText(/Move to a cell with the arrow keys/);
    await expect(hint).toBeVisible();
    await grid.getByText("****").first().hover();
    await expect(hint).toBeVisible();

    await grid.getByText("****").first().click();
    const masked = page.getByRole("region", { name: "customer_email, row 1" });
    await expect(masked).toContainText("****");
    await grid.getByText(/Delivered to the back door/).click();
    const whole = page.getByRole("region", { name: "note, row 1" });
    await expect(whole).toContainText("and more and more and more");
    await expect(whole).toContainText("U+202E");
    await grid.getByText("binary, 12 bytes").click();
    await expect(page.getByRole("region", { name: "receipt, row 1" })).toContainText(
      "binary, 12 bytes",
    );
  });

  test("a row of the Catalog list and a Home link open the table page", async ({ page }) => {
    await page.goto("/catalog?env=prod&ns=gold.sales");
    await page.getByRole("link", { name: "fact_sales" }).click();
    await expect(page).toHaveURL(new RegExp(`${TABLE}$`));
    await expect(page.getByRole("heading", { level: 1, name: "fact_sales" })).toBeFocused();

    await page.goto("/");
    const recent = page.getByRole("link", { name: /station_ranking/ }).first();
    await expect(recent).toHaveAttribute("href", "/catalog/prod/gold/station_ranking");
    await recent.click();
    await expect(page).toHaveURL(/\/catalog\/prod\/gold\/station_ranking$/);
    await expect(page.getByRole("heading", { level: 1, name: "station_ranking" })).toBeVisible();
  });

  test("the page asks only for the stubbed routes and logs no console error", async ({ page }) => {
    const errors = consoleErrors(page);
    const requests: string[] = [];
    page.on("request", (request) => requests.push(request.url()));
    await page.setViewportSize({ width: 320, height: 640 });

    await openTable(page);
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Label" })).toBeFocused();
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: /models\/gold\/fact_sales\.sql/ }).click();
    await expect(page.getByRole("region", { name: "The file's text" })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.getByRole("link", { name: "Columns", exact: true }).click();
    await expect(page.getByRole("region", { name: "Table columns" })).toBeVisible();
    await page.getByRole("link", { name: "Preview", exact: true }).click();
    await previewGrid(page);
    for (const entry of NEW_TABS) {
      await page.getByRole("link", { name: tabName(entry), exact: true }).click();
      await expect(page.getByRole(entry[1], { name: entry[2], exact: true })).toBeVisible();
    }
    await page.waitForLoadState("networkidle");

    const api = requests.filter((url) => url.includes("/api/")).map((url) => new URL(url).pathname);
    expect(api.length).toBeGreaterThan(0);
    expect(api.filter((path) => !STUBBED.includes(path))).toEqual([]);
    expect(errors).toEqual([]);
  });
  test("Lineage, Health and Access work by keyboard alone; focus lands on the named targets", async ({
    page,
  }) => {
    await openTable(page);
    const tabs = page.getByRole("navigation", { name: "Sections of the table" });
    await tabTo(page, tabs.getByRole("link", { name: "Lineage", exact: true }));
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/fact_sales\/lineage$/);
    const region = page.getByRole("region", { name: "Lineage of fact_sales", exact: true });
    await expect(region).toBeVisible();

    // The step button goes when nothing is further up: the focus goes to the drawing's region.
    await tabTo(page, page.getByRole("button", { name: "One step up" }));
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/lineage\?up=2$/);
    await expect(page.getByRole("button", { name: "One step up" })).toHaveCount(0);
    await expect(region).toBeFocused();
    // The list names a table the drawing's links skip, and Tab reaches it.
    const list = page.getByRole("list", { name: "Lineage as a list" });
    await tabTo(page, list.getByRole("link", { name: "gold.sales.daily_sales" }));

    await tabTo(page, tabs.getByRole("link", { name: "Health", exact: true }));
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/fact_sales\/health$/);
    await expect(page.getByRole("heading", { level: 2, name: "Checks" })).toBeVisible();
    await tabTo(page, page.getByRole("button", { name: "Copy command" }));

    await tabTo(page, tabs.getByRole("link", { name: "Access", exact: true }));
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/fact_sales\/access$/);
    await expect(page.getByRole("heading", { level: 2, name: "Column masks" })).toBeVisible();
    await tabTo(page, page.getByRole("link", { name: "kim.schulz" }).first());
  });

  test("the full-screen lineage opens, passes axe and returns focus", async ({ page }) => {
    await openNewTab(page, NEW_TABS[0]);
    const open = page.getByRole("button", { name: "Full screen" });
    await open.click();
    const dialog = page.getByRole("dialog", { name: "Lineage of fact_sales" });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("svg.lineage-full")).toBeVisible();
    expect(await violations(page), "the full-screen lineage").toEqual([]);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(open).toBeFocused();

    // The Close button does the same.
    await open.click();
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();
    await expect(open).toBeFocused();
  });

  test("the full-screen lineage keeps the drawing's natural size and scrolls", async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 700 });
    await openTable(page, "/lineage?up=3&down=3");
    await expect(
      page.getByRole("region", { name: "Lineage of fact_sales", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Full screen" }).click();
    const dialog = page.getByRole("dialog", { name: "Lineage of fact_sales" });
    const region = dialog.getByRole("region", { name: "Lineage of fact_sales, full screen" });
    await expect(region).toHaveAttribute("tabindex", "0");
    const sizes = await region.evaluate((node) => {
      const svg = node.querySelector("svg.lineage-full");
      const wanted = Number(svg?.getAttribute("width"));
      return {
        rendered: svg?.getBoundingClientRect().width ?? 0,
        wanted,
        scrollWidth: node.scrollWidth,
        clientWidth: node.clientWidth,
      };
    });
    expect(sizes.wanted).toBeGreaterThan(600);
    expect(sizes.rendered).toBeGreaterThanOrEqual(sizes.wanted);
    expect(sizes.scrollWidth).toBeGreaterThan(sizes.clientWidth);
    expect(await violations(page), "the full-screen region").toEqual([]);
  });

  test("Back and Forward move between tabs and lineage depths", async ({ page }) => {
    await openNewTab(page, NEW_TABS[0]);
    await page.getByRole("button", { name: "One step up" }).click();
    await expect(page).toHaveURL(/\/lineage\?up=2$/);
    await expect(page.getByRole("button", { name: "One step up" })).toHaveCount(0);
    await page.getByRole("link", { name: "Health", exact: true }).click();
    await expect(page).toHaveURL(/\/fact_sales\/health$/);
    await expect(page.getByRole("heading", { level: 2, name: "Checks" })).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/\/lineage\?up=2$/);
    await expect(page.getByRole("region", { name: "Lineage of fact_sales" })).toBeVisible();
    await expect(page.getByRole("button", { name: "One step up" })).toHaveCount(0);
    await page.goBack();
    await expect(page).toHaveURL(/\/lineage$/);
    await expect(page.getByRole("button", { name: "One step up" })).toBeVisible();
    await page.goForward();
    await expect(page).toHaveURL(/\/lineage\?up=2$/);
    await page.goForward();
    await expect(page).toHaveURL(/\/fact_sales\/health$/);
  });

  test("a click on a table in the drawing opens its page without a page load", async ({ page }) => {
    // The table's page is not stubbed: the frame is what shows that the router took the click.
    await page.route(
      (url) => url.pathname.endsWith("/namespaces/gold.sales/tables/daily_sales"),
      (route) => route.fulfill({ status: 404, json: { detail: "No such table." } }),
    );
    await openNewTab(page, NEW_TABS[0]);
    await page.evaluate(() => {
      (window as unknown as { loadMarker: number }).loadMarker = 1;
    });
    const box = page.locator(
      'svg.lineage-graph a[href="/catalog/prod/gold.sales/daily_sales"] svg.lineage-box',
    );
    await expect(box).toHaveCount(1);
    // Jobs and connections are not links.
    await expect(
      page.locator(
        "svg.lineage-graph a svg.lineage-job, svg.lineage-graph a svg.lineage-connection",
      ),
    ).toHaveCount(0);
    await box.click();
    await expect(page).toHaveURL(/\/catalog\/prod\/gold\.sales\/daily_sales$/);
    await expect(page.getByRole("heading", { level: 1, name: "daily_sales" })).toBeVisible();
    expect(
      await page.evaluate(() => (window as unknown as { loadMarker?: number }).loadMarker),
    ).toBe(1);
  });

  test("Readers › opens Health at its readers", async ({ page }) => {
    await openTable(page);
    await page.getByRole("link", { name: "Readers ›" }).click();
    await expect(page).toHaveURL(/\/fact_sales\/health#readers$/);
    const heading = page.getByRole("heading", { level: 2, name: "Readers" });
    await expect(heading).toBeFocused();
    await expect(page.getByRole("link", { name: "kim.schulz" })).toBeVisible();
    await expect(page.getByText("12 reads")).toBeVisible();
  });

  test("Health details › and Lineage › open their tabs", async ({ page }) => {
    await openTable(page);
    await page.getByRole("link", { name: "Health details ›" }).click();
    await expect(page).toHaveURL(/\/fact_sales\/health$/);
    await expect(page.getByRole("heading", { level: 2, name: "Checks" })).toBeVisible();
    await page.goBack();
    await page.getByRole("link", { name: "Lineage ›" }).click();
    await expect(page).toHaveURL(/\/fact_sales\/lineage$/);
    await expect(page.getByRole("region", { name: "Lineage of fact_sales" })).toBeVisible();
  });
});
