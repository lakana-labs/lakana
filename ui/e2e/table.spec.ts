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

/** The tabs after Preview (References last, so the indices of the others hold), with what shows once each has read its data. */
const NEW_TABS = [
  ["/lineage", "region", "Lineage of fact_sales"],
  ["/health", "heading", "Checks"],
  ["/access", "heading", "Column masks"],
  ["/references", "heading", "This table references"],
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
 * Opens one of the Lineage, Health, Access and References tabs and waits until it shows what it read.
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
  const grid = page.getByRole("grid", { name: "Preview of fact_sales, rows" });
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

/** How a section under an h2 looks: the box around the heading and the room around it. */
interface PanelLook {
  readonly heading: string;
  readonly background: string;
  readonly ground: string;
  readonly border: string;
  readonly shadow: string;
  readonly headingSize: number;
  readonly bodySize: number;
  /** The space to the next section's box; null for the last one. */
  readonly gapAfter: number | null;
  /** The largest space between two neighbouring children inside the box. */
  readonly innerGap: number;
}

/**
 * Reads how each section under an h2 in the tab's content looks.
 *
 * @param page - The page, showing a tab of the table.
 * @returns One entry per h2 in the tab.
 */
function panelLooks(page: Page): Promise<PanelLook[]> {
  return page.evaluate(() => {
    const ground = getComputedStyle(document.body).backgroundColor;
    const bodySize = parseFloat(getComputedStyle(document.body).fontSize);
    const tab = document.querySelector(".table-tab");
    const boxes = [...(tab?.querySelectorAll("h2") ?? [])].map(
      (heading) => heading.closest("section") ?? heading.parentElement ?? heading,
    );
    return boxes.map((box, index) => {
      const style = getComputedStyle(box);
      const heading = box.querySelector("h2");
      const rect = box.getBoundingClientRect();
      const next = boxes[index + 1]?.getBoundingClientRect();
      const children = [...box.children].map((child) => child.getBoundingClientRect());
      let innerGap = 0;
      for (let i = 0; i + 1 < children.length; i += 1) {
        const here = children[i];
        const after = children[i + 1];
        if (here && after && after.top >= here.bottom) {
          innerGap = Math.max(innerGap, after.top - here.bottom);
        }
      }
      return {
        heading: heading?.textContent ?? "",
        background: style.backgroundColor,
        ground,
        border: `${style.borderTopWidth} ${style.borderTopStyle}`,
        shadow: style.boxShadow,
        headingSize: heading === null ? 0 : parseFloat(getComputedStyle(heading).fontSize),
        bodySize,
        gapAfter:
          next !== undefined && Math.abs(next.left - rect.left) < 1 ? next.top - rect.bottom : null,
        innerGap,
      };
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`the table page, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
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

    test("the seven tabs wrap at 320 px and every tab fits and passes axe", async ({ page }) => {
      await page.setViewportSize({ width: 320, height: 640 });
      await openTable(page);
      const tabs = page.getByRole("navigation", { name: "Sections of the table" });
      const boxes = await tabs
        .getByRole("link")
        .evaluateAll((links) => links.map((link) => link.getBoundingClientRect()));
      expect(boxes).toHaveLength(7);
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

    test("the sections of Overview, Lineage, References, Health and Access are flat panels; Columns has none", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 1920, height: 1080 });
      await openTable(page);
      await expect(page.getByText("Check unique_sale_id failed")).toBeVisible();
      const looks = [...(await panelLooks(page))];
      for (const entry of NEW_TABS) {
        await openNewTab(page, entry);
        const found = await panelLooks(page);
        expect(found.length, entry[0]).toBeGreaterThan(0);
        looks.push(...found);
        if (entry[0] === "/health") {
          // The Checks panel ends with a command and its empty copy hint: no more room below
          // the command than the panel's padding and the command line's own margin.
          const below = await page.evaluate(() => {
            const panel = document.querySelector(".health-section");
            const line = panel?.querySelector(".copy-line");
            const pad = panel === null ? 0 : parseFloat(getComputedStyle(panel).paddingBottom);
            return {
              gap:
                (panel?.getBoundingClientRect().bottom ?? 0) -
                (line?.getBoundingClientRect().bottom ?? 0),
              allowed: pad + 1 + 12,
            };
          });
          expect(below.gap, JSON.stringify(below)).toBeLessThanOrEqual(below.allowed);
        }
      }
      expect(looks.length).toBeGreaterThanOrEqual(9);
      for (const look of looks) {
        const said = JSON.stringify(look);
        expect(look.background, said).not.toBe(look.ground);
        expect(look.background, said).not.toBe("rgba(0, 0, 0, 0)");
        expect(look.border, said).toBe("1px solid");
        expect(look.shadow, said).toBe("none");
        // The section step (15 px, weight 500) over the 14 px body, as every panel's title (#782).
        expect(look.headingSize - look.bodySize, said).toBeGreaterThanOrEqual(1);
        if (look.gapAfter !== null) {
          expect(look.gapAfter, said).toBeGreaterThan(look.innerGap);
          expect(look.gapAfter, said).toBeGreaterThanOrEqual(16);
        }
      }

      await openTable(page, "/columns");
      const list = page.locator(".table-tab .list").first();
      await expect(list).toBeVisible();
      const framed = await list.evaluate((node) => node.closest(".flat-panel") !== null);
      expect(framed).toBe(false);
    });

    // Since the flat panels (decided 2026-11-14) Overview needs about 130 px more than 800, and a
    // 1280 × 800 window may scroll it; it must still fit a 1920 × 1080 window.
    test("Overview fits 1920 × 1080 without scrolling for the typical table", async ({ page }) => {
      await page.setViewportSize({ width: 1920, height: 1080 });
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
    // The row's number, then its first value.
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(inside).toHaveCount(1);
    expect(await inside.textContent()).not.toBe(first);
    await expect(page.locator(".preview-value")).toBeVisible();
    await expect(stops).toHaveCount(1);
    await page.keyboard.press("Tab");
    expect(await grid.evaluate((node) => node.contains(document.activeElement))).toBe(false);
  });

  test("the editor keeps the normal field spacing; an empty counter takes no room", async ({
    page,
  }) => {
    await openTable(page);
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Label" })).toBeFocused();
    const look = await page.evaluate(() => {
      const fields = [...document.querySelectorAll(".text-editor .field")];
      const first = fields[0];
      const second = fields[1];
      const counter = first?.querySelector("[role='status']");
      const last = first?.lastElementChild;
      const visible = [...(first?.children ?? [])].filter(
        (child) => getComputedStyle(child).position !== "absolute",
      );
      const end = visible.at(-1)?.getBoundingClientRect().bottom ?? 0;
      return {
        counter: counter?.textContent ?? null,
        counterHeight: counter?.getBoundingClientRect().height ?? -1,
        lastIsCounter: last === counter,
        space: (second?.getBoundingClientRect().top ?? 0) - end,
        margin: parseFloat(first === undefined ? "0" : getComputedStyle(first).marginBottom),
      };
    });
    // The counter is still there for screen readers, empty, and the next field follows after
    // the field's own margin, nothing more.
    expect(look.counter).toBe("");
    expect(look.lastIsCounter).toBe(true);
    expect(look.counterHeight).toBeLessThanOrEqual(1);
    expect(Math.abs(look.space - look.margin), `${String(look.space)} px`).toBeLessThanOrEqual(1);
  });

  test("every tab takes the whole width of the working area, Preview and Lineage too", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const widths: Record<string, number> = {};
    for (const tab of ["", "/columns"]) {
      await openTable(page, tab);
      widths[tab || "/"] = (await page.locator(".page").boundingBox())?.width ?? 0;
    }
    await openNewTab(page, NEW_TABS[1]);
    widths["/health"] = (await page.locator(".page").boundingBox())?.width ?? 0;
    const area = (await page.locator(".shell-main").boundingBox())?.width ?? 0;
    for (const [tab, width] of Object.entries(widths)) {
      expect(width, tab).toBeGreaterThan(area - 2 * 24 - 2);
    }
    await openTable(page, "/preview");
    await previewGrid(page);
    const preview = page.getByRole("region", { name: "Preview of fact_sales", exact: true });
    const main = (await page.locator(".shell-main").boundingBox())?.width ?? 0;
    // The window less the sidebar and the main area's own 24 px gutter on both sides.
    expect((await preview.boundingBox())?.width ?? 0).toBeGreaterThan(main - 2 * 24 - 2);
    await openNewTab(page, NEW_TABS[0]);
    const drawing = page.getByRole("region", { name: "Lineage of fact_sales", exact: true });
    expect((await drawing.boundingBox())?.width ?? 0).toBeGreaterThan(main - 2 * 24 - 2);
  });

  test("the preview grid is dense, aligns numbers, keeps its header in view and numbers its rows", async ({
    page,
  }) => {
    // A page of 60 rows, so the grid scrolls under its header.
    const rows = Array.from({ length: 60 }, (_, index) => [
      String(index * 1000 + 7),
      "****",
      `note ${String(index)}`,
      null,
    ]);
    await page.route(
      (url) => url.pathname === `${RUN}/result`,
      (route) =>
        route.fulfill({
          json: {
            columns: [
              { name: "sale_id", type: "int64" },
              { name: "customer_email", type: "string" },
              { name: "note", type: "string" },
              { name: "receipt", type: "binary" },
            ],
            columns_cut: 0,
            masked_columns: [],
            masked_columns_cut: 0,
            rows,
            row_cut: [],
            row_count: 60,
            next_offset: null,
            duration_ms: 800,
            preview: { table: "gold.sales.fact_sales" },
          },
        }),
    );
    await openTable(page);
    await page.evaluate(
      ([key, value]) => {
        window.localStorage.setItem(key ?? "", value ?? "");
      },
      [KEY, JSON.stringify({ theme: "dark", density: "roomy", language: "en" })],
    );
    await openTable(page, "/preview");
    const grid = await previewGrid(page);
    await expect(grid.getByRole("rowheader")).toHaveCount(60);
    // One density: 24 px rows, whatever an older version stored.
    const height = (await grid.locator("tbody tr").first().boundingBox())?.height ?? 0;
    expect(Math.abs(height - 24), `row ${String(height)}`).toBeLessThan(1.5);

    // Numbers: right-aligned in tabular figures, the header too; text stays on the left.
    const looks = await grid.evaluate((node) => {
      const look = (cell: Element | null | undefined): string[] => {
        const style = cell === null || cell === undefined ? null : getComputedStyle(cell);
        return [style?.textAlign ?? "", style?.fontVariantNumeric ?? ""];
      };
      const row = node.querySelector("tbody tr");
      return {
        numberHead: look(node.querySelectorAll("thead th")[1]),
        numberCell: look(row?.children[1]),
        textCell: look(row?.children[2]),
      };
    });
    expect(looks.numberHead).toEqual(["right", "tabular-nums"]);
    expect(looks.numberCell).toEqual(["right", "tabular-nums"]);
    expect(looks.textCell[0]).not.toBe("right");

    // The header row stays at the top of the region while the rows scroll under it.
    const region = page.getByRole("region", { name: "Preview of fact_sales", exact: true });
    const tops = await region.evaluate((node) => {
      node.scrollTop = 400;
      const head = node.querySelector("thead th");
      return {
        scrolled: node.scrollTop,
        region: node.getBoundingClientRect().top + node.clientTop,
        head: head?.getBoundingClientRect().top ?? 0,
      };
    });
    expect(tops.scrolled).toBeGreaterThan(0);
    expect(Math.abs(tops.head - tops.region)).toBeLessThan(1);

    // The row numbers: a muted gutter as narrow as its widest number, the row header, and never
    // part of copied cells.
    const gutter = grid.getByRole("rowheader").first();
    await expect(gutter).toHaveText("1");
    const narrow = await grid.evaluate((node) => {
      const cell = node.querySelector("tbody tr:last-child .row-number");
      const probe = document.createElement("span");
      probe.textContent = "00";
      cell?.append(probe);
      const digits = probe.getBoundingClientRect().width;
      probe.remove();
      return { width: cell?.getBoundingClientRect().width ?? 0, digits };
    });
    expect(narrow.width, `${String(narrow.width)} px`).toBeLessThanOrEqual(
      narrow.digits + 2 * 8 + 2,
    );
    expect(await gutter.evaluate((node) => getComputedStyle(node).userSelect)).toBe("none");
    const copied = await grid.evaluate((node) => {
      const body = node.querySelector("tbody");
      const range = document.createRange();
      if (body !== null) {
        range.selectNodeContents(body);
      }
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      return selection?.toString() ?? "";
    });
    expect(copied).toContain("note 0");
    expect(copied).toContain("7");
    expect(copied.split("\n")[0]?.trim().startsWith("7")).toBe(true);
    // A cell the arrow keys move to is never hidden under the sticky header (WCAG 2.4.11): put
    // row 20 right under the header, so row 19 hides behind it, then press ArrowUp.
    await grid.getByText("note 19", { exact: true }).click();
    await region.evaluate((node) => {
      const head = node.querySelector("thead th")?.getBoundingClientRect().height ?? 0;
      const row = [...node.querySelectorAll("tbody tr")][19];
      const top = row?.getBoundingClientRect().top ?? 0;
      node.scrollTop += top - (node.getBoundingClientRect().top + node.clientTop) - head;
    });
    await page.keyboard.press("ArrowUp");
    await expect(grid.locator(":focus")).toHaveText("note 18");
    const shown = await region.evaluate((node) => {
      const head = node.querySelector("thead th")?.getBoundingClientRect().bottom ?? 0;
      const focused = document.activeElement?.getBoundingClientRect().top ?? 0;
      return { head, focused };
    });
    expect(
      shown.focused,
      `${String(shown.focused)} under ${String(shown.head)}`,
    ).toBeGreaterThanOrEqual(shown.head - 0.5);

    // SQL NULL is a marker that does not look like text, and reads "SQL null".
    const marker = grid.locator("tbody tr").first().locator(".value-marker");
    await expect(marker.locator("[aria-hidden='true']")).toHaveText("null");
    await expect(grid.getByRole("gridcell", { name: "SQL null" }).first()).toBeVisible();
    const looks2 = await grid.evaluate((node) => {
      const row = node.querySelector("tbody tr");
      const text = row?.children[3];
      const nullMark = row?.querySelector(".value-marker");
      const style = (one: Element | null | undefined): string[] => {
        const css = one === null || one === undefined ? null : getComputedStyle(one);
        return [css?.fontStyle ?? "", css?.backgroundColor ?? "", css?.color ?? ""];
      };
      return { text: style(text), marker: style(nullMark) };
    });
    expect(looks2.marker).not.toEqual(looks2.text);
    expect(looks2.marker[0]).toBe("italic");
    expect(await violations(page), "the dense grid, dark, compact").toEqual([]);
  });

  for (const width of [1280, 1920]) {
    test(`at ${String(width)} px no word in Health's tables breaks inside, and nothing says null`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      await openNewTab(page, NEW_TABS[1]);
      const signals = page.getByRole("region", { name: "Health signals" });
      await expect(signals).toBeVisible();
      const broken = await page.evaluate(() => {
        const found: string[] = [];
        const lines = (node: Node, start: number, end: number): number => {
          const range = document.createRange();
          range.setStart(node, start);
          range.setEnd(node, end);
          const tops = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
          return tops.size;
        };
        // Text for screen readers only is not on any line.
        const shown = (cell: Element): Text[] => {
          const texts: Text[] = [];
          const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
          for (let text = walker.nextNode(); text !== null; text = walker.nextNode()) {
            if (text.parentElement?.closest(".visually-hidden") === null) {
              texts.push(text as Text);
            }
          }
          return texts;
        };
        const signalTable = document.querySelector("[aria-label='Health signals'] table");
        // In the signals: a signal's kind, its effect and when it last fired.
        const short = new Set([0, 5, 6]);
        for (const cell of document.querySelectorAll(".health-tab th, .health-tab td")) {
          // Headers and the short columns stay on one line.
          const whole =
            cell.tagName === "TH" ||
            (cell.closest("table") === signalTable &&
              short.has((cell as HTMLTableCellElement).cellIndex));
          const texts = shown(cell);
          const tops = new Set(
            texts.flatMap((text) => {
              const range = document.createRange();
              range.selectNodeContents(text);
              return [...range.getClientRects()].map((rect) => Math.round(rect.top));
            }),
          );
          if (whole && tops.size > 1) {
            found.push(`on two lines: ${cell.textContent}`);
          }
          for (const text of texts) {
            const value = text.textContent;
            for (const word of value.matchAll(/\S+/g)) {
              if (lines(text, word.index, word.index + word[0].length) > 1) {
                found.push(`broken: ${word[0]}`);
              }
            }
          }
        }
        return found;
      });
      expect(broken).toEqual([]);
      const observed = await signals.locator("tbody tr td:nth-child(4)").allTextContents();
      expect(observed.length).toBeGreaterThan(1);
      for (const text of observed) {
        expect(text).not.toMatch(/null/i);
      }
      expect(observed).toContain("54 minutes since the last write");
      expect(observed).toContain("0 of 3 values empty");
      // The band in words too, never a raw number of seconds or microseconds.
      const bands = await signals.locator("tbody tr td:nth-child(5)").allTextContents();
      expect(bands).toContain("up to 24 hours");
      expect(bands).toContain("up to 2.0 % empty");
      for (const text of bands) {
        expect(text).not.toMatch(/\d{7,}|null|high |low /);
      }
      // Every effect is one of the interface's words.
      const effects = await signals.locator("tbody tr td:nth-child(6)").allTextContents();
      for (const effect of effects) {
        expect(["Warns", "Blocks publication", "Off"]).toContain(effect);
      }
    });
  }

  test("at 320 px Health's signals scroll in their own focusable region", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 640 });
    await openNewTab(page, NEW_TABS[1]);
    const signals = page.getByRole("region", { name: "Health signals" });
    await expect(signals).toHaveAttribute("tabindex", "0");
    const sizes = await signals.evaluate((node) => [node.scrollWidth, node.clientWidth]);
    expect(sizes[0]).toBeGreaterThan(sizes[1] ?? 0);
    expect(await sidewaysOverflow(page)).toBeLessThanOrEqual(0);
    expect(await violations(page), "Health at 320 px").toEqual([]);
  });

  test("rows of Columns, Health and Access are compact: one row height", async ({ page }) => {
    const heights: Record<string, number[]> = {};
    /**
     * Records the height of the first row of the tab's first list.
     *
     * @param name - The tab's name.
     */
    const measure = async (name: string): Promise<void> => {
      const box = await page.locator(".list tbody tr").first().boundingBox();
      (heights[name] ??= []).push(box?.height ?? 0);
    };
    await openTable(page);
    await page.evaluate(
      ([key, value]) => {
        window.localStorage.setItem(key ?? "", value ?? "");
      },
      [KEY, JSON.stringify({ theme: "light", language: "en" })],
    );
    await openTable(page, "/columns");
    await measure("Columns");
    for (const entry of NEW_TABS.slice(1, 3)) {
      await openNewTab(page, entry);
      await measure(entry[0]);
    }
    expect(Object.keys(heights)).toHaveLength(3);
    // At least the 32 px row; a second line adds two dense lines' worth at most.
    for (const [name, [height]] of Object.entries(heights)) {
      expect(height ?? 0, name).toBeGreaterThanOrEqual(31.5);
      expect(height ?? 0, name).toBeLessThan(44);
    }
  });

  test("at 320 px a status word in Health and Access stays on one line", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 640 });
    for (const entry of NEW_TABS.slice(1, 3)) {
      await openNewTab(page, entry);
      const words = page.locator(".table-tab .glyph-word");
      await expect(words.first()).toBeVisible();
      const heights = await words.evaluateAll((nodes) =>
        nodes.map((node) => [
          node.textContent,
          node.getBoundingClientRect().height,
          parseFloat(getComputedStyle(node).lineHeight),
        ]),
      );
      expect(heights.length, entry[0]).toBeGreaterThan(0);
      for (const [word, height, line] of heights) {
        expect(height, `${entry[0]} ${String(word)}`).toBeLessThanOrEqual(Number(line) + 1);
      }
      // A list that does not scroll in its own region stays inside its panel.
      const outside = await page.evaluate(() =>
        [...document.querySelectorAll(".flat-panel .list")]
          .filter((list) => list.closest(".list-scroll") === null)
          .map((list) => {
            const panel = list.closest(".flat-panel");
            const right =
              (panel?.getBoundingClientRect().right ?? 0) -
              parseFloat(panel === null ? "0" : getComputedStyle(panel).paddingRight);
            return list.getBoundingClientRect().right - right;
          })
          .filter((over) => over > 0.5),
      );
      expect(outside, entry[0]).toEqual([]);
    }
    // The checks keep their own, narrower minimum, whichever stylesheet loaded first.
    await openNewTab(page, NEW_TABS[1]);
    const checks = page.locator(".checks-scroll .list");
    await expect(checks).toBeVisible();
    expect(await checks.evaluate((node) => getComputedStyle(node).minWidth)).toBe("480px");
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
      const region = page.getByRole("region", { name, exact: true });
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
      "#",
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
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
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

  for (const [width, height] of [
    [1920, 1080],
    [1280, 800],
  ] as const) {
    test(`at ${String(width)} × ${String(height)} lineage boxes fit their text and the full screen fills the window`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      // A long asset name and a long project, upstream of the table.
      const root = {
        id: "table:9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
        kind: "table",
        name: "gold.sales.fact_sales",
        distance: 0,
      };
      const asset = {
        id: "asset:analytics-platform-weather-stations/gold.station_ranking_by_region_and_month",
        kind: "asset",
        name: "gold.station_ranking_by_region_and_month",
        distance: 1,
        repository: "analytics-platform-weather-stations",
      };
      await page.route(
        (url) =>
          url.pathname === "/api/v1/environments/prod/graph/lineage" &&
          url.searchParams.get("direction") === "up",
        (route) =>
          route.fulfill({
            json: {
              nodes: [root, asset],
              edges: [{ kind: "builds", source: asset.id, target: root.id }],
              next_cursor: null,
            },
          }),
      );
      await openNewTab(page, NEW_TABS[0]);

      /**
       * Reads every box of a drawing: its width, its lines and how far each line reaches.
       *
       * @param scope - The drawing's region.
       * @returns One entry per box.
       */
      const boxes = (
        scope: Locator,
      ): Promise<{ width: number; title: string; lines: [string, number][] }[]> =>
        scope.evaluate((node) =>
          [...node.querySelectorAll<SVGSVGElement>("svg.lineage-box")].map((box) => ({
            width: Number(box.getAttribute("width")),
            title: box.querySelector("title")?.textContent ?? "",
            lines: [...box.querySelectorAll<SVGTextElement>("text")].map(
              (text) =>
                [
                  text.textContent,
                  (text.x.baseVal[0]?.value ?? 0) + text.getComputedTextLength(),
                ] as [string, number],
            ),
          })),
        );
      const check = async (scope: Locator, where: string): Promise<void> => {
        const drawn = await boxes(scope);
        expect(drawn.length, where).toBeGreaterThan(2);
        for (const box of drawn) {
          expect(box.width, `${where}: ${box.title}`).toBeGreaterThanOrEqual(200);
          expect(box.width, `${where}: ${box.title}`).toBeLessThanOrEqual(320);
          for (const [text, reach] of box.lines) {
            // No line runs past its box's right edge less the text's own margin.
            expect(reach, `${where}: "${text}"`).toBeLessThanOrEqual(box.width - 10 + 0.5);
          }
        }
        const long = drawn.find((box) => box.title.includes("station_ranking"));
        expect(long?.width, where).toBe(320);
        expect(long?.lines[1]?.[0].endsWith("…"), where).toBe(true);
        expect(long?.title, where).toContain(
          "gold.station_ranking_by_region_and_month · analytics-platform-weather-stations",
        );
        // A name that fits a wider box is whole: the asset that builds daily_sales.
        const whole = drawn.find((box) => box.title.includes("gold.sales.daily_sales · analytics"));
        expect(whole?.lines[1]?.[0], where).toBe("asset gold.sales.daily_sales · analytics");
      };
      await check(
        page.getByRole("region", { name: "Lineage of fact_sales", exact: true }),
        "the tab",
      );

      await page.getByRole("button", { name: "Full screen" }).click();
      const dialog = page.getByRole("dialog", { name: "Lineage of fact_sales" });
      const full = dialog.getByRole("region", { name: "Lineage of fact_sales, full screen" });
      await expect(full).toBeVisible();
      await check(full, "full screen");
      // The dialog covers the window, inside it, with a small margin; Close stays in view.
      const modal = await page.locator(".lineage-dialog").boundingBox();
      expect(modal).not.toBeNull();
      const box = modal ?? { x: 0, y: 0, width: 0, height: 0 };
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.x).toBeLessThanOrEqual(24);
      expect(box.y).toBeLessThanOrEqual(24);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      expect(box.y + box.height).toBeLessThanOrEqual(height);
      expect(box.x + box.width).toBeGreaterThanOrEqual(width - 24);
      expect(box.y + box.height).toBeGreaterThanOrEqual(height - 24);
      const close = await dialog.getByRole("button", { name: "Close", exact: true }).boundingBox();
      expect((close?.y ?? height) + (close?.height ?? 0)).toBeLessThanOrEqual(height);
      expect(await sidewaysOverflow(page)).toBeLessThanOrEqual(0);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
    });
  }

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

test.describe("Maintain now and Retention in Chromium", () => {
  const origin = "http://localhost:4173";
  const table = "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";

  test("Maintain now queues maintenance and names the query and its command; axe is clean", async ({
    page,
  }) => {
    await signInAsAdmin(page);
    await page.route(
      (url) => url.pathname === `${table}/maintenance`,
      (route) => route.fulfill({ status: 202, json: { query_id: "q-77" } }),
    );

    await page.goto(`${origin}${TABLE}`);
    await page.getByRole("button", { name: "Actions" }).click();
    await page.getByRole("menuitem", { name: "Maintain now…" }).click();
    const dialog = page.getByRole("dialog", { name: "Maintain gold.sales.fact_sales now" });
    await expect(dialog.getByRole("textbox", { name: /Keep snapshots younger/ })).toHaveValue("7");
    expect(await violations(page), "the Maintain now form").toEqual([]);
    await dialog.getByRole("button", { name: "Maintain now" }).click();

    await expect(dialog.getByText("Maintenance is queued as query q-77.")).toBeVisible();
    await expect(dialog.getByText("lakana query status 'q-77' --env 'prod'")).toBeVisible();
    expect(await violations(page), "the queued dialog").toEqual([]);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.getByRole("button", { name: "Actions" })).toBeFocused();
  });

  test("Retention shows the rule, saves another and shows its sentence; axe is clean", async ({
    page,
  }) => {
    await signInAsAdmin(page);
    const retention = {
      name: "fact_sales",
      namespace: "gold.sales",
      uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
      source: "table",
      days: 30,
      on: null,
      never: false,
      set_by: "ana",
      set_at: "2026-11-01T08:00:00Z",
      last_activity_at: "2026-11-12T06:00:00Z",
      expires_at: "2026-12-12T06:00:00Z",
      kept: null,
    };
    let body: unknown = null;
    await page.route(
      (url) => url.pathname === `${table}/retention`,
      async (route) => {
        if (route.request().method() === "PUT") {
          body = route.request().postDataJSON();
          return route.fulfill({
            json: { ...retention, days: null, never: true, expires_at: null },
          });
        }
        return route.fulfill({ json: retention });
      },
    );

    await page.goto(`${origin}${TABLE}`);
    await page.getByRole("button", { name: "Actions" }).click();
    await page.getByRole("menuitem", { name: "Retention…" }).click();
    const dialog = page.getByRole("dialog", { name: "Retention of gold.sales.fact_sales" });
    await expect(
      dialog.getByText("30 days after its last activity, set for the table by ana"),
    ).toBeVisible();
    expect(await violations(page), "the Retention form").toEqual([]);
    await dialog.getByText("On a day").click();
    expect(await violations(page), "the day field").toEqual([]);
    await dialog.getByText("Never").click();
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect(dialog.getByText("never, set for the table by ana")).toBeVisible();
    expect(body).toEqual({ never: true });
    expect(await violations(page), "the saved retention").toEqual([]);
  });
});
