import { expect, type Locator, type Page, test } from "@playwright/test";

import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const CHOICE = "lakana.catalog.admin";

/**
 * Opens the Catalog at an address and waits for its list, or for the words that stand for it.
 *
 * @param page - The page.
 * @param address - The address, such as `/catalog?q=store`.
 * @param waitFor - What shows that the list has loaded; the list itself by default.
 */
async function openCatalog(
  page: Page,
  address = "/catalog",
  waitFor: Locator = page.getByRole("region", { name: "Tables" }),
): Promise<void> {
  await page.goto(address);
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
  await expect(waitFor).toBeVisible();
}

/**
 * The "Sort by" button, which shows the chosen column, or the "Order" button after it.
 *
 * @param page - The page.
 * @param which - 0 for Sort by, 1 for Order.
 * @returns The button.
 */
function sortMenu(page: Page, which: 0 | 1): Locator {
  return page.locator(".sort-select").nth(which).getByRole("button");
}

/**
 * The page's address without the origin.
 *
 * @param page - The page.
 * @returns The path and the query.
 */
function where(page: Page): string {
  const url = new URL(page.url());
  return `${url.pathname}${url.search}`;
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`${theme}, ${density}`, () => {
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

      test("axe finds nothing on the list, the open Sort by, a search with matches, no hits and an empty namespace", async ({
        page,
      }) => {
        await openCatalog(page);
        await expect(page.getByRole("row")).not.toHaveCount(0);
        expect(await violations(page), "the list").toEqual([]);

        await sortMenu(page, 0).click();
        await expect(page.getByRole("listbox")).toBeVisible();
        expect(await violations(page), "Sort by open").toEqual([]);
        await page.keyboard.press("Escape");

        await openCatalog(page, "/catalog?q=store_id");
        await expect(page.getByText("Column store_id")).toBeVisible();
        expect(await violations(page), "a search with matches").toEqual([]);

        await openCatalog(page, "/catalog?q=nothing", page.getByText(/No table in .* matches/));
        expect(await violations(page), "no hits").toEqual([]);

        await openCatalog(
          page,
          "/catalog?env=prod&ns=bronze",
          page.getByText("No tables in bronze yet", { exact: false }),
        );
        expect(await violations(page), "an empty namespace").toEqual([]);
      });

      test("at least 24 px between the content of every two neighbouring cells", async ({
        page,
      }) => {
        await page.setViewportSize({ width: 1280, height: 800 });
        await openCatalog(page);
        const body = page.locator("tbody tr");
        await expect(body).toHaveCount(3);
        // The visible content of a cell is its text and its pictures; what is hidden for the eye
        // (the words for a screen reader) does not count. Each row gives the gaps between the
        // cells that hold content.
        const gaps = await page.evaluate(() => {
          const out: { row: number; cell: number; gap: number }[] = [];
          document.querySelectorAll("tbody tr").forEach((tr, row) => {
            const spans = [...tr.querySelectorAll("td")].map((td) => {
              const rects: DOMRect[] = [];
              const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
              for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
                const parent = node.parentElement;
                if (
                  (node.textContent ?? "").trim() !== "" &&
                  parent?.closest(".visually-hidden") === null
                ) {
                  const range = document.createRange();
                  range.selectNodeContents(node);
                  rects.push(range.getBoundingClientRect());
                }
              }
              td.querySelectorAll("svg, img").forEach((picture) => {
                rects.push(picture.getBoundingClientRect());
              });
              return rects.length === 0
                ? null
                : {
                    left: Math.min(...rects.map((rect) => rect.left)),
                    right: Math.max(...rects.map((rect) => rect.right)),
                  };
            });
            for (let cell = 0; cell + 1 < spans.length; cell += 1) {
              const here = spans[cell];
              const next = spans[cell + 1];
              if (here && next) {
                out.push({ row, cell, gap: next.left - here.right });
              }
            }
          });
          return out;
        });
        // Seven columns and the chevron: rows have cells with content in all but the last.
        expect(gaps.length).toBeGreaterThanOrEqual(3 * 6);
        const tightest = Math.min(...gaps.map((entry) => entry.gap));
        expect(
          tightest,
          JSON.stringify(gaps.filter((entry) => entry.gap < 24)),
        ).toBeGreaterThanOrEqual(24);
        // The rows hold "1.2M" and "83.9 MB", the widest numbers to sit against their neighbours.
        await expect(page.getByText("1.2M")).toBeVisible();
        await expect(page.getByText("83.9 MB")).toBeVisible();
      });
    });
  }
}

test.describe("the Catalog in real Chromium", () => {
  test.beforeEach(async ({ page }) => {
    await signInAsAdmin(page);
  });

  test("the tree works by keyboard and choosing a node keeps focus in the tree", async ({
    page,
  }) => {
    await openCatalog(page);
    const tree = page.getByRole("treegrid", { name: "Environments and namespaces" });
    await expect(tree).toBeVisible();
    const all = tree.getByRole("link", { name: "All tables, 4 tables" });
    await expect(all).toHaveAttribute("aria-current", "page");

    await tabTo(page, all);
    await page.keyboard.press("ArrowDown");
    const dev = tree.getByRole("link", { name: "dev, 1 table" });
    await expect(dev).toBeFocused();
    // Closing an environment hides its namespaces, opening it shows them again.
    const rows = tree.getByRole("row");
    await expect(rows).toHaveCount(6);
    await page.keyboard.press("ArrowLeft");
    await expect(rows).toHaveCount(5);
    await page.keyboard.press("ArrowRight");
    await expect(rows).toHaveCount(6);
    await page.keyboard.press("ArrowDown");
    const gold = tree.getByRole("link", { name: "gold, 1 table" });
    await expect(gold).toBeFocused();

    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\?env=dev&ns=gold$/);
    await expect(gold).toHaveAttribute("aria-current", "page");
    await expect(gold).toBeFocused();
    await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).not.toBeFocused();

    // One Tab leaves the tree for the search field.
    await page.keyboard.press("Tab");
    await expect(page.getByRole("searchbox")).toBeFocused();
  });

  test("the list, Sort by, the search field and Show more work by keyboard alone", async ({
    page,
  }) => {
    await openCatalog(page);
    const rows = page.getByRole("region", { name: "Tables" }).locator("tbody tr");
    await expect(rows).toHaveCount(3);

    // The search field: typing searches once typing pauses, Escape clears it.
    const search = page.getByRole("searchbox");
    await tabTo(page, search);
    await page.keyboard.type("store");
    await expect(page).toHaveURL(/\?q=store$/);
    await expect(page.getByText("Column store_id")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(search).toHaveValue("");
    await expect(page).toHaveURL(/\/catalog$/);
    await expect(rows).toHaveCount(3);

    // Sort by: Tab to it, Enter opens it, arrows move, Enter chooses.
    await page.keyboard.press("Tab");
    const sortBy = sortMenu(page, 0);
    await expect(sortBy).toBeFocused();
    await page.keyboard.press("Enter");
    const menu = page.getByRole("listbox");
    await expect(menu).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(menu).toBeHidden();
    await expect(page).toHaveURL(/\?sort=owner$/);
    await expect(sortMenu(page, 0)).toContainText("Owner");
    await expect(sortMenu(page, 0)).toBeFocused();

    // Order: Space opens it, Escape closes it and returns to the button.
    await page.keyboard.press("Tab");
    const order = sortMenu(page, 1);
    await expect(order).toContainText("A to Z");
    await expect(order).toBeFocused();
    await page.keyboard.press("Space");
    await expect(page.getByRole("listbox")).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\?sort=owner&order=desc$/);
    await expect(order).toContainText("Z to A");
    await expect(order).toBeFocused();

    // The list is a region the keyboard can scroll; its headers sort with Enter and Space.
    await page.keyboard.press("Tab");
    await expect(page.getByRole("region", { name: "Tables" })).toBeFocused();
    const heads = page.locator(".sort-head");
    await page.keyboard.press("Tab");
    await expect(heads.nth(0)).toBeFocused();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await expect(heads.nth(2)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\?sort=health$/);
    await expect(heads.nth(2)).toBeFocused();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Space");
    await expect(page).toHaveURL(/\?sort=rows$/);

    // A row opens by its link.
    const first = rows.first().getByRole("link");
    await tabTo(page, first);
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/catalog\/prod\/gold\/station_ranking$/);
    await page.goBack();

    // Show more adds the next page and puts the focus on the first new row.
    const more = page.getByRole("button", { name: "Show more" });
    await tabTo(page, more);
    await page.keyboard.press("Enter");
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(3).getByRole("link", { name: "late_arrival" })).toBeFocused();
    await expect(more).toHaveCount(0);
  });

  test("Back and Forward restore the namespace, the search and the sort", async ({ page }) => {
    await openCatalog(page);
    const tree = page.getByRole("treegrid", { name: "Environments and namespaces" });
    await tree.getByRole("link", { name: "prod, 3 tables" }).click();
    await expect(page).toHaveURL(/\?env=prod$/);
    await page.getByRole("searchbox").fill("store");
    await expect(page).toHaveURL(/\?env=prod&q=store$/);
    await sortMenu(page, 0).click();
    await page.getByRole("option", { name: "Rows" }).click();
    await expect(page).toHaveURL(/\?env=prod&q=store&sort=rows$/);

    const shown = async (): Promise<string[]> => [
      where(page),
      await page.getByRole("searchbox").inputValue(),
      (await tree.locator("[aria-current=page]").getAttribute("aria-label")) ?? "",
      (await sortMenu(page, 0).textContent()) ?? "",
    ];
    await expect
      .poll(shown)
      .toEqual(["/catalog?env=prod&q=store&sort=rows", "store", "prod, 3 tables", "Rows▾"]);

    await page.goBack();
    await expect
      .poll(shown)
      .toEqual(["/catalog?env=prod&q=store", "store", "prod, 3 tables", "Name▾"]);
    await page.goBack();
    await expect.poll(shown).toEqual(["/catalog", "", "All tables, 4 tables", "Name▾"]);
    await page.goForward();
    await expect
      .poll(shown)
      .toEqual(["/catalog?env=prod&q=store", "store", "prod, 3 tables", "Name▾"]);
    await page.goForward();
    await expect
      .poll(shown)
      .toEqual(["/catalog?env=prod&q=store&sort=rows", "store", "prod, 3 tables", "Rows▾"]);
  });

  test("no horizontal scroll at 320 px; the list scrolls in its own focusable region", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 320, height: 640 });
    await openCatalog(page);
    await expect(page.getByRole("row")).not.toHaveCount(0);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    const region = page.getByRole("region", { name: "Tables" });
    await expect(region).toHaveAttribute("tabindex", "0");
    const sizes = await region.evaluate((node) => [node.scrollWidth, node.clientWidth]);
    expect(sizes[0]).toBeGreaterThan(sizes[1] ?? 0);
    expect(await violations(page), "320 px").toEqual([]);
  });

  test("the screen asks only for the stubbed routes and logs no console error", async ({
    page,
  }) => {
    const errors = consoleErrors(page);
    const requests: string[] = [];
    page.on("request", (request) => requests.push(request.url()));

    await openCatalog(page);
    await page.getByRole("searchbox").fill("store_id");
    await expect(page.getByText("Column store_id")).toBeVisible();
    await openCatalog(page, "/catalog?env=staging", page.getByText(/no environment named staging/));
    await page.waitForLoadState("networkidle");

    const api = requests.filter((url) => url.includes("/api/")).map((url) => new URL(url).pathname);
    expect(api.length).toBeGreaterThan(0);
    expect(api.filter((path) => !STUBBED.includes(path))).toEqual([]);
    // The stubbed 404 of the unknown environment is the one answer the browser logs.
    expect(errors.filter((error) => !error.includes("404"))).toEqual([]);
  });

  test("hovering a time, a size or a health word reaches the text that explains it", async ({
    page,
  }) => {
    await openCatalog(page);
    const rows = page.locator("tbody tr");
    const time = rows.first().locator("time").first();
    const size = rows.first().locator("[title$=' bytes']");
    const health = rows.nth(1).locator(".health-text");
    for (const [what, target] of [
      ["time", time],
      ["size", size],
      ["health", health],
    ] as const) {
      // Playwright refuses to hover an element another one covers, so this fails if the row's
      // link overlay sits above the titled cell.
      await target.hover({ timeout: 2000 });
      const title = await target.getAttribute("title");
      expect(title, what).toBeTruthy();
      const box = await target.boundingBox();
      const atPointer = await page.evaluate(
        ([x, y]) =>
          document
            .elementFromPoint(x ?? 0, y ?? 0)
            ?.closest("[title]")
            ?.getAttribute("title"),
        [(box?.x ?? 0) + (box?.width ?? 0) / 2, (box?.y ?? 0) + (box?.height ?? 0) / 2],
      );
      expect(atPointer, what).toBe(title);
    }
    // The time's title is the absolute time, the same one a screen reader hears.
    const absolute = await time.getAttribute("title");
    await expect(
      rows
        .first()
        .locator(".visually-hidden")
        .filter({ hasText: absolute ?? "" })
        .first(),
    ).toHaveCount(1);
    await expect(health).toHaveAttribute(
      "title",
      "Check failed, and 2 more, listed on the table page",
    );
  });

  test("a plain click on a time, a size or the health word opens the table", async ({ page }) => {
    await openCatalog(page);
    const rows = page.locator("tbody tr");
    const cases = [
      ["time", rows.first().locator("time").first(), "/catalog/prod/gold/station_ranking"],
      ["size", rows.first().locator("[title$=' bytes']"), "/catalog/prod/gold/station_ranking"],
      ["health", rows.nth(1).locator(".health-text"), "/catalog/prod/gold.sales/fact_sales"],
    ] as const;
    for (const [what, target, path] of cases) {
      await openCatalog(page);
      await target.click();
      await expect(page, what).toHaveURL(new RegExp(`${path.replaceAll(".", "\\.")}$`));
    }
  });

  test("a drag that selects text in a time cell does not open the table", async ({ page }) => {
    await openCatalog(page);
    const time = page.locator("tbody tr").first().locator("time").first();
    await time.scrollIntoViewIfNeeded();
    const box = await time.boundingBox();
    expect(box).not.toBeNull();
    const y = (box?.y ?? 0) + (box?.height ?? 0) / 2;
    await page.mouse.move((box?.x ?? 0) + (box?.width ?? 0) - 1, y);
    const entries = await page.evaluate(() => window.history.length);
    await page.mouse.down();
    // Released left of the text, in the cell's padding: the click then lands on the cell, not on
    // the text, and reaches the row.
    await page.mouse.move((box?.x ?? 0) - 8, y, { steps: 8 });
    await page.mouse.up();
    expect(await page.evaluate(() => window.getSelection()?.toString() ?? "")).not.toBe("");
    // A navigation would push a history entry and change the address; give the router a few
    // frames to start one before asserting that none did.
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          let frames = 0;
          const tick = (): void => {
            if (++frames < 20) {
              requestAnimationFrame(tick);
            } else {
              resolve();
            }
          };
          requestAnimationFrame(tick);
        }),
    );
    expect(await page.evaluate(() => window.history.length)).toBe(entries);
    expect(where(page)).toBe("/catalog");
  });

  test("the Catalog opens the stored place on its first render, and Show all tables shows all tables", async ({
    page,
  }) => {
    await page.addInitScript(
      ([key, value]) => {
        if (window.localStorage.getItem(key ?? "") === null) {
          window.localStorage.setItem(key ?? "", value ?? "");
        }
      },
      [CHOICE, JSON.stringify({ environment: "prod", namespace: "gold" })],
    );
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    await page.getByRole("link", { name: "Catalog", exact: true }).click();
    await expect(page).toHaveURL(/\/catalog\?env=prod&ns=gold$/);
    const tree = page.getByRole("treegrid", { name: "Environments and namespaces" });
    await expect(tree.getByRole("link", { name: "gold, 3 tables" }).first()).toHaveAttribute(
      "aria-current",
      "page",
    );

    await openCatalog(page, "/catalog?env=staging", page.getByText(/no environment named staging/));
    await page.getByRole("link", { name: "Show all tables" }).click();
    await expect.poll(() => where(page)).toBe("/catalog");
    await expect(page.locator("tbody tr")).toHaveCount(3);
    await expect(
      page
        .getByRole("treegrid", { name: "Environments and namespaces" })
        .getByRole("link", { name: "All tables, 4 tables" }),
    ).toHaveAttribute("aria-current", "page");
  });
});
