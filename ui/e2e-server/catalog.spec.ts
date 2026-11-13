import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  createEnvironment,
  createNamespace,
  createTable,
  freshPage,
  grantTableRead,
  invitedPerson,
  signInLocally,
  skipTheTour,
  uniqueName,
  violations,
} from "./helpers";

type Request = Parameters<typeof createEnvironment>[0];

/**
 * An environment (it starts with `bronze`, `silver` and `gold`) with `gold.sales.fact_sales` (a `store_id` column), `gold.dim_store` and an
 * empty `bronze`. The tables are created in this order, so Created lists `dim_store` first.
 *
 * @param request - The request context.
 * @returns The environment's name.
 */
async function lake(request: Request): Promise<string> {
  const environment = uniqueName("cat");
  await createEnvironment(request, environment);
  await createNamespace(request, environment, "gold.sales");
  await createTable(request, environment, "gold.sales", "fact_sales", ["id", "store_id", "amount"]);
  await createTable(request, environment, "gold", "dim_store", ["store_id", "name"]);
  return environment;
}

/**
 * Signs the bootstrap administrator in on Home without the welcome.
 *
 * @param browser - The browser.
 * @param request - The request context.
 * @returns The page.
 */
async function administrator(
  browser: Parameters<typeof freshPage>[0],
  request: Request,
): Promise<Page> {
  await skipTheTour(request);
  const page = await freshPage(browser);
  await signInLocally(page, request);
  return page;
}

/**
 * Waits for the welcome of a person with access who has never seen the tour, and skips it.
 *
 * @param page - The page, on Home.
 */
async function skipTheWelcome(page: Page): Promise<void> {
  const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
  await expect(welcome).toBeVisible();
  await welcome.getByRole("button", { name: "Skip" }).click();
  await expect(welcome).toBeHidden();
}

/**
 * The tree's link to a place of one environment, found by its address so that no other
 * environment's namespace of the same name and count can match.
 *
 * @param tree - The tree.
 * @param environment - The environment.
 * @param namespace - The dotted namespace.
 * @returns The link.
 */
function placeLink(tree: Locator, environment: string, namespace: string): Locator {
  return tree.locator(`a[href="/catalog?env=${environment}&ns=${namespace}"]`);
}

/**
 * The rows of the list that belong to an environment: the run shares one database, so a test
 * looks only at its own.
 *
 * @param page - The page.
 * @param environment - The environment.
 * @returns The rows.
 */
function rowsIn(page: Page, environment: string): Locator {
  return page.getByRole("region", { name: "Tables" }).locator("tbody tr").filter({
    hasText: environment,
  });
}

/**
 * The tree.
 *
 * @param page - The page.
 * @returns The tree.
 */
function treeOf(page: Page): Locator {
  return page.getByRole("treegrid", { name: "Environments and namespaces" });
}

test("an administrator drills down from All tables to gold.sales and back", async ({
  browser,
  request,
}) => {
  const environment = await lake(request);
  const page = await administrator(browser, request);

  await page.getByRole("link", { name: "Catalog", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeFocused();
  const tree = treeOf(page);
  await expect(tree.getByRole("link", { name: /^All tables, \d+ tables?$/ })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(rowsIn(page, environment)).toHaveCount(2);

  await tree.getByRole("link", { name: `${environment}, 2 tables` }).click();
  await expect(page).toHaveURL(new RegExp(`/catalog\\?env=${environment}$`));
  await expect(rowsIn(page, environment)).toHaveCount(2);
  await expect(page.getByRole("status")).toHaveText("2 tables");

  await placeLink(tree, environment, "gold").click();
  await expect(page).toHaveURL(new RegExp(`\\?env=${environment}&ns=gold$`));
  await expect(rowsIn(page, environment)).toHaveCount(2);

  // The namespaces start closed: open gold to reach sales.
  await expect(placeLink(tree, environment, "gold.sales")).toHaveCount(0);
  await placeLink(tree, environment, "gold").focus();
  await page.keyboard.press("ArrowRight");
  await placeLink(tree, environment, "gold.sales").click();
  await expect(page).toHaveURL(new RegExp(`\\?env=${environment}&ns=gold\\.sales$`));
  const only = rowsIn(page, environment);
  await expect(only).toHaveCount(1);
  await expect(only.getByRole("link", { name: "fact_sales" })).toHaveAttribute(
    "href",
    `/catalog/${environment}/gold.sales/fact_sales`,
  );
  await expect(only).toContainText("admin");
  await expect(only).toContainText(environment);

  await page.goBack();
  await expect(rowsIn(page, environment)).toHaveCount(2);
  await tree.getByRole("link", { name: /^All tables, \d+ tables?$/ }).click();
  await expect(page).toHaveURL(/\/catalog$/);
  await expect(rowsIn(page, environment)).toHaveCount(2);

  await only.getByRole("link", { name: "fact_sales" }).click();
  await expect(page).toHaveURL(new RegExp(`/catalog/${environment}/gold\\.sales/fact_sales$`));
  await page.context().close();
});

test("search finds a table by a column name and widens to all namespaces", async ({
  browser,
  request,
}) => {
  const environment = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`/catalog?env=${environment}&ns=gold.sales`);
  await expect(rowsIn(page, environment)).toHaveCount(1);
  await page.getByRole("searchbox").fill("store_id");
  await expect(page).toHaveURL(/q=store_id$/);
  const hit = rowsIn(page, environment);
  await expect(hit).toHaveCount(1);
  await expect(hit.getByRole("link", { name: "fact_sales" })).toBeVisible();
  await expect(hit).toContainText("Column store_id");
  await expect(page.getByRole("status")).toContainText("match");

  // Nothing in bronze: the way out keeps the text and drops the place.
  await page.goto(`/catalog?env=${environment}&ns=bronze&q=store_id`);
  await expect(page.getByText("No table in bronze matches “store_id”.")).toBeVisible();
  await page.getByRole("button", { name: "Search all namespaces" }).click();
  await expect(page).toHaveURL(/\/catalog\?q=store_id$/);
  await expect(rowsIn(page, environment)).toHaveCount(2);
  await expect(rowsIn(page, environment).filter({ hasText: "Column store_id" })).toHaveCount(2);
  await page.context().close();
});

test("sorting by a header and by Created is in the address and survives a reload", async ({
  browser,
  request,
}) => {
  const environment = await lake(request);
  const page = await administrator(browser, request);
  const names = async (): Promise<string[]> =>
    rowsIn(page, environment).locator("a.row-name").allTextContents();

  await page.goto(`/catalog?env=${environment}`);
  await expect.poll(names).toEqual(["dim_store", "fact_sales"]);

  await page.getByRole("button", { name: "Name", exact: true }).click();
  await expect(page).toHaveURL(/order=desc$/);
  await expect.poll(names).toEqual(["fact_sales", "dim_store"]);
  await page.reload();
  await expect.poll(names).toEqual(["fact_sales", "dim_store"]);
  await expect(page.getByRole("columnheader", { name: "Name" })).toHaveAttribute(
    "aria-sort",
    "descending",
  );

  await page.getByRole("button", { name: "Owner", exact: true }).click();
  await expect(page).toHaveURL(/sort=owner$/);
  await page.reload();
  await expect(page.getByRole("columnheader", { name: "Owner" })).toHaveAttribute(
    "aria-sort",
    "ascending",
  );

  // Created is not a column: only the menu sorts by it, newest first.
  await page.locator(".sort-select").first().getByRole("button").click();
  await page.getByRole("option", { name: "Created" }).click();
  await expect(page).toHaveURL(new RegExp(`\\?env=${environment}&sort=created$`));
  await expect.poll(names).toEqual(["dim_store", "fact_sales"]);
  await page.reload();
  await expect(page.locator(".sort-select").first().getByRole("button")).toContainText("Created");
  await expect.poll(names).toEqual(["dim_store", "fact_sales"]);
  await page.locator(".sort-select").nth(1).getByRole("button").click();
  await page.getByRole("option", { name: "Oldest first" }).click();
  await expect(page).toHaveURL(/sort=created&order=asc$/);
  await expect.poll(names).toEqual(["fact_sales", "dim_store"]);
  await page.context().close();
});

test("a reader granted one table sees only it, with its owner, and counts only it", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const environment = await lake(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    grantTableRead(request, name, environment, "gold.sales", "fact_sales"),
  );
  await skipTheWelcome(page);

  await page.getByRole("link", { name: "Catalog", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
  const tree = treeOf(page);
  await expect(tree.getByRole("link", { name: "All tables, 1 table" })).toBeVisible();
  await expect(tree.getByRole("link", { name: `${environment}, 1 table` })).toBeVisible();
  const rows = page.getByRole("region", { name: "Tables" }).locator("tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.getByRole("link", { name: "fact_sales" })).toBeVisible();
  await expect(rows).toContainText("admin");
  await expect(page.getByText("dim_store")).toHaveCount(0);
  await page.context().close();
});

test("a person with no access yet sees no Catalog", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { page } = await invitedPerson(browser, request, async () => {});
  await expect(page.getByText("You have no access yet; ask an administrator.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Catalog", exact: true })).toHaveCount(0);
  await page.goto("/catalog");
  await expect(
    page.getByRole("heading", { level: 1, name: "You cannot use Catalog" }),
  ).toBeVisible();
  await expect(page.getByText("Ask an administrator if you need it.")).toBeVisible();
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test(`axe finds nothing on the real list (${scheme}, ${String(width)} px)`, async ({
      browser,
      browserName,
      request,
    }) => {
      test.skip(browserName !== "chromium", "axe runs in Chromium only");
      const environment = await lake(request);
      await skipTheTour(request);
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);

      await page.goto(`/catalog?env=${environment}`);
      await expect(rowsIn(page, environment)).toHaveCount(2);
      expect(await violations(page), "the list").toEqual([]);
      await page.goto(`/catalog?env=${environment}&q=store_id`);
      await expect(rowsIn(page, environment).first()).toContainText("Column store_id");
      expect(await violations(page), "a search").toEqual([]);
      await page.goto(`/catalog?env=${environment}&ns=bronze`);
      await expect(page.getByText("No tables in bronze yet.")).toBeVisible();
      expect(await violations(page), "an empty namespace").toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        ),
      ).toBeLessThanOrEqual(0);
      await page.context().close();
    });
  }
}
