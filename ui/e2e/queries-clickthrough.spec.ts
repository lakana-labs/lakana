/**
 * The click-through checks of the Queries screen that the review of M7 parts 4–6 made by hand:
 * the layout, the editor's help with catalog names, the Value panel, a large result at 320 px,
 * the name of a saved query pasted from two lines, the remembered library filter, the dialogs'
 * sentences, a saved query read while offline, the keyboard's focus and ring, and the chunks
 * each step loads. The stubs are those of `queries.ts`, with the catalog of `dev` beside them.
 *
 * @packageDocumentation
 */
import { expect, type Locator, type Page, test } from "@playwright/test";

import { expectRingOnEveryStop } from "./focus-ring";
import { LONG_SQL, RUN_ID, SALES_SQL, stubQueries, WEEKLY } from "./queries";

/** The column the editor's help explains. */
const COLUMN = { name: "avg_temp_c", type: "double", description: "The average in °C." };

/** The table of the catalog stub. */
const TABLE = { namespace: "silver", name: "station_temperatures" };

/** The rows a large result has, more than a spreadsheet holds. */
const LARGE = 1_100_000;

/**
 * Opens the Queries screen at an address and waits for the editor.
 *
 * @param page - The page.
 * @param path - The address.
 * @returns The editor's text box.
 */
async function openQueries(page: Page, path = "/queries"): Promise<Locator> {
  await page.goto(path);
  const editor = page.getByRole("textbox", { name: "SQL" });
  await expect(editor).toBeVisible();
  return editor;
}

/**
 * Chooses an environment in the picker.
 *
 * @param page - The page.
 * @param name - The environment.
 */
async function choose(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name: /^Environment: / }).click();
  await page.getByRole("option", { name: new RegExp(`^${name}`, "u") }).click();
  await expect(
    page.getByRole("button", { name: new RegExp(`^Environment: ${name}`, "u") }),
  ).toBeVisible();
}

/**
 * Empties the editor and inserts a text without opening the completion list.
 *
 * @param page - The page.
 * @param editor - The editor's text box.
 * @param sql - The text, on one line.
 */
async function writeSql(page: Page, editor: Locator, sql: string): Promise<void> {
  await editor.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Backspace");
  await page.keyboard.insertText(sql);
}

/**
 * The element that holds the focus, through the editor's shadow root, as `tag.class`.
 *
 * @param page - The page.
 * @returns Its tag, its classes and its text.
 */
function focused(page: Page): Promise<string> {
  return page.evaluate(() => {
    let active: Element | null = document.activeElement;
    while (active?.shadowRoot?.activeElement) {
      active = active.shadowRoot.activeElement;
    }
    return active === null
      ? "none"
      : `${active.tagName.toLowerCase()}.${[...active.classList].join(".")} ${active.textContent.trim().slice(0, 30)}`;
  });
}

/**
 * Whether the focus is inside the page's main region, below the sidebar.
 *
 * @param page - The page.
 * @returns True when it is.
 */
function focusInMain(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.querySelector("main")?.contains(document.activeElement) === true,
  );
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

/**
 * Answers the reads of the editor's catalog in `dev`: the tree with one namespace, its one table
 * and the table's columns, one with a description.
 *
 * @param page - The page.
 */
async function stubCatalog(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/v1/tables/tree",
    (route) =>
      route.fulfill({
        json: {
          count: 1,
          environments: [
            { name: "dev", count: 1, namespaces: [{ path: TABLE.namespace, count: 1 }] },
          ],
        },
      }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/tables",
    (route) =>
      route.fulfill({
        json: { items: [{ ...TABLE, environment: "dev" }], next_cursor: null },
      }),
  );
  await page.route(
    (url) =>
      url.pathname ===
      `/api/v1/environments/dev/namespaces/${TABLE.namespace}/tables/${TABLE.name}`,
    (route) =>
      route.fulfill({
        json: {
          ...TABLE,
          schema: [
            { name: "station_id", type: "string", description: null },
            { name: COLUMN.name, type: COLUMN.type, description: COLUMN.description },
          ],
        },
      }),
  );
}

/**
 * Answers the succeeding run in `dev` as a result of {@link LARGE} rows of six wide columns, a
 * hundred rows a page.
 *
 * @param page - The page.
 */
async function stubLargeResult(page: Page): Promise<void> {
  const at = `/api/v1/environments/dev/queries/${RUN_ID}`;
  const columns = ["station", "measured_at", "temperature", "humidity", "pressure", "comment"];
  await page.route(
    (url) => url.pathname === at,
    (route) =>
      route.fulfill({
        json: {
          query_id: RUN_ID,
          origin: "written",
          sql: "SELECT * FROM big",
          status: "SUCCEEDED",
          error_message: null,
          error_cause: null,
          error_line: null,
          error_column: null,
          result_rows: LARGE,
          result_bytes: 40_000_000,
          created_at: "2026-11-17T09:00:00Z",
          started_at: "2026-11-17T09:00:01Z",
          finished_at: "2026-11-17T09:00:05Z",
          waiting_for: null,
        },
      }),
  );
  await page.route(
    (url) => url.pathname === `${at}/result`,
    (route) => {
      const offset = Number(new URL(route.request().url()).searchParams.get("offset") ?? "0");
      const rows = Array.from({ length: 100 }, (_, index) =>
        columns.map((column) => `${column} of row ${String(offset + index + 1)}`),
      );
      return route.fulfill({
        json: {
          columns: columns.map((name) => ({ name, type: "string" })),
          columns_cut: 0,
          masked_columns: [],
          masked_columns_cut: 0,
          rows,
          row_cut: [],
          row_count: LARGE,
          next_offset: offset + 100,
          duration_ms: 4000,
          preview: null,
        },
      });
    },
  );
}

test.describe("the Queries screen, checked by clicking through", () => {
  test.beforeEach(async ({ page }) => {
    await stubQueries(page);
    await stubCatalog(page);
  });

  test("the toolbar stands above the editor and the library beside it, with every control and the run keys", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    const editor = await openQueries(page);
    const toolbar = page.locator(".queries-toolbar");
    await expect(toolbar.locator(".queries-name")).toHaveText("Untitled query");
    for (const name of ["Save…", "Format", "Environment: prod, protected", "Run"]) {
      await expect(toolbar.getByRole("button", { name })).toBeVisible();
    }
    await expect(toolbar).toContainText(/Run(Ctrl ↵|⌘↵)/u);
    const library = page.getByRole("complementary", { name: "Saved and recent queries" });
    await expect(library.getByRole("button", { name: "Show: All environments" })).toBeVisible();
    await expect(library.getByRole("heading", { level: 2 })).toHaveText(["Saved", "Recent"]);
    await expect(library.getByRole("link", { name: WEEKLY.name })).toBeVisible();

    const [side, bar, box] = await Promise.all([
      library.boundingBox(),
      toolbar.boundingBox(),
      page.locator(".sql-editor").boundingBox(),
    ]);
    expect(side && box && side.x + side.width <= box.x, "the library left of the editor").toBe(
      true,
    );
    expect(bar && box && bar.y + bar.height <= box.y, "the toolbar above the editor").toBe(true);
    // The editor takes the room of the screen's column to the right of the library.
    const screen = await page.locator(".queries").boundingBox();
    expect(box && screen && screen.x + screen.width - (box.x + box.width) <= 2).toBe(true);
    await expect(editor).toBeVisible();
  });

  test("after a namespace and a dot the editor offers its tables, after an alias and a dot the columns, and pointing at a column explains it", async ({
    page,
  }) => {
    const editor = await openQueries(page);
    await choose(page, "dev");
    await writeSql(page, editor, "SELECT * FROM ");
    await page.keyboard.type("silver.");
    const list = page.locator(".cm-tooltip-autocomplete");
    await expect(list.getByRole("option", { name: TABLE.name })).toBeVisible();
    await page.keyboard.press("Enter");
    await page.keyboard.type(" t WHERE t.avg");
    await expect(list.getByRole("option", { name: new RegExp(COLUMN.name, "u") })).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(page.locator(".cm-content")).toHaveText(
      `SELECT * FROM silver.${TABLE.name} t WHERE t.${COLUMN.name}`,
    );

    // Pointing at the column's name shows its type and its description.
    const spot = await page.evaluate((name) => {
      const content = document
        .querySelector(".sql-editor")
        ?.shadowRoot?.querySelector(".cm-content");
      const walker = document.createTreeWalker(content ?? document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
        const at = (node.textContent ?? "").indexOf(name);
        if (at >= 0) {
          const range = document.createRange();
          range.setStart(node, at + 2);
          range.setEnd(node, at + 3);
          const rect = range.getBoundingClientRect();
          return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
        }
      }
      return null;
    }, COLUMN.name);
    expect(spot).not.toBeNull();
    await page.mouse.move(spot?.x ?? 0, spot?.y ?? 0);
    const hover = page.locator(".cm-tooltip-hover");
    await expect(hover).toContainText(`${COLUMN.name} · ${COLUMN.type}`);
    await expect(hover).toContainText(COLUMN.description);
  });

  test("a column's name without its table offers and explains the column too", async ({ page }) => {
    test.skip(
      true,
      "The editor completes and explains a column only after its table or alias and a dot: https://github.com/lakana-labs/lakana/issues/737",
    );
    const editor = await openQueries(page);
    await choose(page, "dev");
    await writeSql(page, editor, `SELECT * FROM silver.${TABLE.name} WHERE `);
    await page.keyboard.type("avg");
    await expect(
      page.locator(".cm-tooltip-autocomplete").getByRole("option", { name: COLUMN.name }),
    ).toBeVisible();
  });

  test("the arrow keys move between cells and the Value panel shows the cell's whole value", async ({
    page,
  }) => {
    const editor = await openQueries(page);
    await writeSql(page, editor, SALES_SQL);
    await page.keyboard.press("ControlOrMeta+Enter");
    const grid = page.getByRole("grid", { name: /^Result of Query /u });
    await expect(grid).toBeVisible();
    await grid.getByRole("gridcell", { name: "Berlin" }).click();
    await expect(page.getByRole("region", { name: "store, row 1" })).toContainText("Berlin");
    await page.keyboard.press("ArrowDown");
    await expect(page.getByRole("region", { name: "store, row 2" })).toContainText("Hamburg");
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("region", { name: "amount, row 2" })).toContainText("980.00");
  });

  test("a result larger than a spreadsheet at 320 px: Show 100 more, the limit line and the downloads wrap, and only the grid scrolls sideways", async ({
    page,
  }) => {
    await stubLargeResult(page);
    await page.setViewportSize({ width: 320, height: 640 });
    const editor = await openQueries(page);
    await choose(page, "dev");
    await writeSql(page, editor, "SELECT * FROM big");
    await page.keyboard.press("ControlOrMeta+Enter");
    const grid = page.getByRole("grid", { name: /^Result of Query /u });
    await expect(grid).toBeVisible();
    // A header row and a hundred rows, then a hundred more for each press.
    await expect(grid.getByRole("row")).toHaveCount(101);
    for (let press = 0; press < 9; press += 1) {
      await page.getByRole("button", { name: "Show 100 more" }).click();
      await expect(grid.getByRole("row")).toHaveCount(101 + 100 * (press + 1));
    }
    await expect(page.getByRole("button", { name: "Show 100 more" })).toHaveCount(0);
    await expect(
      page.getByText("Showing the first 1,000 of 1.1M rows; download the whole result."),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Download spreadsheet" })).toHaveCount(0);
    await expect(
      page.getByText("Too many rows for a spreadsheet (more than 1,048,575); download CSV."),
    ).toBeVisible();

    expect(await sidewaysOverflow(page), "the page").toBeLessThanOrEqual(0);
    const scroll = page.locator(".data-grid-scroll");
    expect(
      await scroll.evaluate((node) => node.scrollWidth > node.clientWidth),
      "the grid is wider than its region",
    ).toBe(true);
    // The grid itself is the region's tab stop, and its arrow keys scroll the region.
    expect(await scroll.evaluate((node) => getComputedStyle(node).overflowX)).toMatch(
      /^(auto|scroll)$/u,
    );
    await page.getByRole("button", { name: "Download CSV" }).focus();
    for (
      let step = 0;
      step < 10 && !(await grid.evaluate((node) => node.contains(document.activeElement)));
      step += 1
    ) {
      await page.keyboard.press("Tab");
    }
    expect(await grid.evaluate((node) => node.contains(document.activeElement))).toBe(true);
    for (let step = 0; step < 6; step += 1) {
      await page.keyboard.press("ArrowRight");
    }
    await expect.poll(() => scroll.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
    for (const line of [
      page.getByText(/^Showing the first 1,000/u),
      page.getByText(/^Too many rows for a spreadsheet/u),
    ]) {
      const box = await line.boundingBox();
      expect(
        box && box.x >= 0 && box.x + box.width <= 320,
        "the line wraps inside the window",
      ).toBe(true);
    }
  });

  test("a name pasted from two lines becomes one line with a space", async ({
    page,
    browserName,
  }) => {
    test.skip(browserName !== "chromium", "the clipboard's permissions are Chromium's");
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await openQueries(page);
    await page.getByRole("button", { name: "Save…" }).click();
    const name = page.getByRole("dialog", { name: "Save the query" }).getByRole("textbox", {
      name: "Name",
    });
    await name.click();
    await page.evaluate(() => navigator.clipboard.writeText("Warmest stations\nby day"));
    await page.keyboard.press("ControlOrMeta+V");
    await expect(name).toHaveValue("Warmest stations by day");
  });

  test("the library's filter applies to both lists, survives a reload and a trip to Home, and All environments lists every one again", async ({
    page,
  }) => {
    await openQueries(page);
    const saved = page.getByRole("region", { name: "Saved queries" });
    await expect(saved.getByRole("link", { name: WEEKLY.name })).toBeVisible();
    await expect(saved.getByRole("link", { name: "Stores by region" })).toBeVisible();

    await page.getByRole("button", { name: "Show: All environments" }).click();
    await page.getByRole("option", { name: "dev", exact: true }).click();
    const devOnly = async (): Promise<void> => {
      await expect(page.getByRole("button", { name: "Show: dev" })).toBeVisible();
      await expect(saved.getByRole("link", { name: "Stores by region" })).toBeVisible();
      await expect(saved.getByRole("link", { name: WEEKLY.name })).toHaveCount(0);
      await expect(saved.getByRole("row").filter({ hasText: "Stores by region" })).toContainText(
        "Environment dev",
      );
    };
    await devOnly();

    await page.reload();
    await devOnly();
    await page
      .getByRole("navigation", { name: "Areas" })
      .getByRole("link", { name: "Home" })
      .click();
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    await page
      .getByRole("navigation", { name: "Areas" })
      .getByRole("link", { name: "Queries" })
      .click();
    await devOnly();

    await page.getByRole("button", { name: "Show: dev" }).click();
    await page.getByRole("option", { name: "All environments" }).click();
    await expect(saved.getByRole("link", { name: WEEKLY.name })).toBeVisible();
    await expect(saved.getByRole("link", { name: "Stores by region" })).toBeVisible();
  });

  test("the Save dialog asks who may run the query, and the Delete dialog says what stays", async ({
    page,
  }) => {
    await openQueries(page, `/queries/prod/saved/${WEEKLY.id}`);
    await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);
    await page.getByRole("button", { name: "Save…" }).click();
    const save = page.getByRole("dialog", { name: "Save the query" });
    const who = save.getByRole("radiogroup", { name: "Who may run it" });
    await expect(who.getByRole("radio")).toHaveCount(2);
    await expect(who.getByRole("radio", { name: "Only me" })).toBeChecked();
    await expect(
      who.getByRole("radio", { name: "Everyone who can run queries in prod" }),
    ).not.toBeChecked();
    await expect(save).toContainText("It is saved in prod.");
    await expect(save.getByRole("button")).toHaveText(["Save", "Save as new", "Cancel"]);
    await save.getByRole("button", { name: "Cancel" }).click();

    await page.getByRole("button", { name: "Delete…" }).click();
    const remove = page.getByRole("dialog", { name: `Delete the saved query ${WEEKLY.name}?` });
    await expect(remove).toContainText("The SQL stays in the editor, and its runs stay in Recent.");
  });

  test("a saved query read while offline offers Try again and a new query, and Try again opens it once online", async ({
    page,
  }) => {
    let offline = true;
    await page.route(
      (url) => url.pathname === `/api/v1/environments/prod/saved-queries/${WEEKLY.id}`,
      (route) => (offline ? route.abort("internetdisconnected") : route.fallback()),
    );
    await openQueries(page, `/queries/prod/saved/${WEEKLY.id}`);
    const alert = page.getByRole("alert");
    await expect(alert).toContainText(
      "Lakana could not be reached. Check that the server is running and your network works, then try again.",
    );
    await expect(alert.getByRole("link", { name: "Start a new query" })).toBeVisible();

    offline = false;
    await alert.getByRole("button", { name: "Try again" }).click();
    await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);
    await expect(page.getByRole("alert")).toHaveCount(0);
  });

  test("by keyboard: Tab leaves the editor, Enter in Name saves, Escape closes Delete, and after each the focus is where it was", async ({
    page,
  }) => {
    await page.goto("/queries");
    await expect(page.getByRole("textbox", { name: "SQL" })).toBeVisible();
    // Into the library and open a saved query with Enter.
    const link = page.getByRole("link", { name: WEEKLY.name });
    for (
      let step = 0;
      step < 40 && !(await link.evaluate((n) => n === document.activeElement));
      step += 1
    ) {
      await page.keyboard.press("Tab");
    }
    await page.keyboard.press("Enter");
    await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);

    // Tab reaches the editor and leaves it again without writing a tab.
    for (let step = 0; step < 40 && !(await focused(page)).includes("cm-content"); step += 1) {
      await page.keyboard.press("Tab");
    }
    await page.keyboard.press("End");
    await page.keyboard.insertText(" ");
    const text = await page.locator(".cm-content").textContent();
    await page.keyboard.press("Tab");
    expect(await focused(page)).not.toContain("cm-content");
    expect(await page.locator(".cm-content").textContent()).toBe(text);

    // Save… and Enter in Name: the dialog closes, the focus is back on Save…, and Tab goes on.
    const saveButton = page.getByRole("button", { name: "Save…" });
    await saveButton.focus();
    await page.keyboard.press("Enter");
    const save = page.getByRole("dialog", { name: "Save the query" });
    const name = save.getByRole("textbox", { name: "Name" });
    for (
      let step = 0;
      step < 10 && !(await name.evaluate((n) => n === document.activeElement));
      step += 1
    ) {
      await page.keyboard.press("Tab");
    }
    await expect(name).toBeFocused();
    await page.keyboard.press("End");
    await page.keyboard.type(" (edited)");
    await page.keyboard.press("Enter");
    await expect(save).toBeHidden();
    await expect(page.locator(".queries-name")).toHaveText(`${WEEKLY.name} (edited)`);
    await expect(saveButton).toBeFocused();
    await page.keyboard.press("Tab");
    expect(await focusInMain(page), "the next Tab after Save").toBe(true);

    // Delete… and Escape: the focus is back on Delete….
    const deleteButton = page.getByRole("button", { name: "Delete…" });
    await deleteButton.focus();
    await page.keyboard.press("Enter");
    const remove = page.getByRole("dialog", { name: /^Delete the saved query / });
    await expect(remove).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(remove).toBeHidden();
    await expect(deleteButton).toBeFocused();
    await page.keyboard.press("Tab");
    expect(await focusInMain(page), "the next Tab after Delete").toBe(true);

    // A run cancelled with its button: the focus is on Run, and Tab goes on.
    const editor = page.getByRole("textbox", { name: "SQL" });
    await writeSql(page, editor, LONG_SQL);
    await page.keyboard.press("ControlOrMeta+Enter");
    const cancel = page.getByRole("button", { name: "Cancel" });
    await cancel.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".queries-run-line")).toHaveText(/Cancelled/u);
    await expect(page.getByRole("button", { name: "Run" })).toBeFocused();
    await page.keyboard.press("Tab");
    expect(await focusInMain(page), "the next Tab after a cancel").toBe(true);
  });

  test("every Tab stop draws a focus ring, and at 320 px the folded library closes on a choice with the focus kept", async ({
    page,
  }) => {
    await openQueries(page);
    await expect(page.getByRole("link", { name: WEEKLY.name })).toBeVisible();
    await expectRingOnEveryStop(page, 15);

    await page.setViewportSize({ width: 320, height: 640 });
    await openQueries(page);
    await page.getByRole("button", { name: "Queries" }).click();
    const fold = page.getByRole("dialog", { name: "Saved and recent queries" });
    await fold.getByRole("link", { name: WEEKLY.name }).click();
    await expect(fold).toBeHidden();
    await expect(page.locator(".queries-name")).toHaveText(WEEKLY.name);
    expect(await focusInMain(page), "the focus stays in the screen").toBe(true);
  });

  test("opening Queries loads its screen's chunk, and the first Format loads the formatter", async ({
    page,
  }) => {
    const scripts: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "script") {
        scripts.push(new URL(request.url()).pathname);
      }
    });
    const chunk = (name: string) => (path: string) =>
      new RegExp(`/${name}-[^/]+\\.js$`, "u").test(path);

    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    for (const name of ["QueriesScreen", "SqlEditor", "formatSql"]) {
      expect(scripts.filter(chunk(name)), `Home loads no ${name}`).toEqual([]);
    }
    await page
      .getByRole("navigation", { name: "Areas" })
      .getByRole("link", { name: "Queries" })
      .click();
    const editor = page.getByRole("textbox", { name: "SQL" });
    await expect(editor).toBeVisible();
    expect(scripts.filter(chunk("QueriesScreen")), "Queries").toHaveLength(1);
    expect(scripts.filter(chunk("SqlEditor")), "Queries").toHaveLength(1);
    expect(scripts.filter(chunk("formatSql")), "before Format").toEqual([]);

    await writeSql(page, editor, "select 1 as one");
    await page.getByRole("button", { name: "Format" }).click();
    await expect(page.locator(".cm-content")).toContainText("SELECT");
    expect(scripts.filter(chunk("formatSql")), "the first Format").toHaveLength(1);
    await page.getByRole("button", { name: "Format" }).click();
    expect(scripts.filter(chunk("formatSql")), "the second Format").toHaveLength(1);
  });
});
