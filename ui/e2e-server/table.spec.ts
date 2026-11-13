import { expect, type Page, test } from "@playwright/test";

import {
  administrator,
  createEnvironment,
  createNamespace,
  createTable,
  freshPage,
  grantTableRead,
  invitedPerson,
  queryStatus,
  signInLocally,
  skipTheTour,
  skipTheWelcome,
  uniqueName,
  violations,
} from "./helpers";

type Request = Parameters<typeof createEnvironment>[0];

/**
 * A new environment with `gold.sales.fact_sales`, so that each test reads its own data on the
 * run's one shared server.
 *
 * @param request - The request context.
 * @returns The environment's name and the table page's address.
 */
async function lake(request: Request): Promise<{ environment: string; address: string }> {
  const environment = uniqueName("tbl");
  await createEnvironment(request, environment);
  await createNamespace(request, environment, "gold.sales");
  await createTable(request, environment, "gold.sales", "fact_sales", ["id", "store_id", "amount"]);
  return { environment, address: `/catalog/${environment}/gold.sales/fact_sales` };
}

/**
 * The tab links under the heading.
 *
 * @param page - The page.
 * @returns The navigation.
 */
function tabsOf(page: Page): ReturnType<Page["getByRole"]> {
  return page.getByRole("navigation", { name: "Sections of the table" });
}

/**
 * The page's overflow to the side.
 *
 * @param page - The page.
 * @returns Pixels; zero or less when nothing scrolls sideways.
 */
function sidewaysOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

test("an administrator opens a table from the list and sees Overview, Columns and Preview", async ({
  browser,
  request,
}) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`/catalog?env=${environment}&ns=gold.sales`);
  await page.locator(`a[href="${address}"]`).click();
  await expect(page).toHaveURL(new RegExp(`${address}$`));
  await expect(page.getByRole("heading", { level: 1, name: "fact_sales" })).toBeFocused();
  await expect(tabsOf(page).getByRole("link")).toHaveText(["Overview", "Columns", "Preview"]);
  await expect(page.getByRole("heading", { level: 2, name: "Facts" })).toBeVisible();
  await expect(page.getByText("No description yet.")).toBeVisible();
  // A table the API created is defined by no project in Git, so there is no file to show.
  await expect(page.getByText("Defined in")).toHaveCount(0);

  await tabsOf(page).getByRole("link", { name: "Columns" }).click();
  await expect(page).toHaveURL(new RegExp(`${address}/columns$`));
  const columns = page.getByRole("region", { name: "Table columns" });
  for (const column of ["id", "store_id", "amount"]) {
    await expect(columns.getByRole("cell", { name: column, exact: true })).toBeVisible();
  }

  // No worker runs here, so the preview stays queued.
  await tabsOf(page).getByRole("link", { name: "Preview" }).click();
  await expect(page).toHaveURL(new RegExp(`${address}/preview$`));
  await expect(page.getByRole("status")).toContainText("Queued");
  await page.context().close();
});

test("an administrator edits the description, sees Edited here, and goes back to the text from Git", async ({
  browser,
  request,
}) => {
  const { address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(address);
  await expect(page.getByText("No description yet.")).toBeVisible();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("textbox", { name: "Description" }).fill("One row per sale.");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("One row per sale.")).toBeVisible();
  await expect(page.getByText("Edited here", { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.getByText("One row per sale.")).toBeVisible();
  await expect(page.getByText("Edited here", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Back to the text from Git" }).click();
  await expect(page.getByText("Edited here", { exact: true })).toHaveCount(0);
  await expect(page.getByText("One row per sale.")).toHaveCount(0);
  await expect(page.getByText("No description yet.")).toBeVisible();
  await page.context().close();
});

test("a reader sees owner and last read but no Edit", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    grantTableRead(request, name, environment, "gold.sales", "fact_sales"),
  );
  await skipTheWelcome(page);

  await page.goto(address);
  await expect(page.getByRole("heading", { level: 1, name: "fact_sales" })).toBeVisible();
  const facts = page.locator(".facts");
  await expect(facts.locator(".fact").filter({ hasText: "Owner" })).toContainText("admin");
  await expect(facts.locator(".fact").filter({ hasText: "Last read" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await tabsOf(page).getByRole("link", { name: "Columns" }).click();
  await expect(page.getByRole("region", { name: "Table columns" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Edit / })).toHaveCount(0);
  await page.context().close();
});

test("a reader without the right to run queries sees the Preview line and no preview is sent", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    grantTableRead(request, name, environment, "gold.sales", "fact_sales"),
  );
  await skipTheWelcome(page);
  const sent: string[] = [];
  page.on("request", (sentRequest) => {
    if (sentRequest.method() === "POST" && sentRequest.url().includes("/preview")) {
      sent.push(sentRequest.url());
    }
  });

  await page.goto(`${address}/preview`);
  await expect(
    page.getByText(
      `Preview runs a query as you, which needs the right to run queries in ${environment}.`,
      { exact: false },
    ),
  ).toBeVisible();
  await expect(page.getByRole("grid")).toHaveCount(0);
  await page.waitForLoadState("networkidle");
  expect(sent).toEqual([]);
  await page.context().close();
});

test("leaving the Preview tab cancels the queued preview", async ({ browser, request }) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);
  const queued = page.waitForResponse(
    (response) => response.request().method() === "POST" && response.url().endsWith("/preview"),
  );

  await page.goto(`${address}/preview`);
  await expect(page.getByRole("status")).toContainText("Queued");
  const { query_id: queryId } = (await (await queued).json()) as { query_id: string };
  expect(await queryStatus(request, environment, queryId)).toBe("QUEUED");

  await tabsOf(page).getByRole("link", { name: "Overview" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Facts" })).toBeVisible();
  await expect.poll(() => queryStatus(request, environment, queryId)).toBe("CANCELLED");
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test(`axe finds nothing on Overview and Columns (${scheme}, ${String(width)} px)`, async ({
      browser,
      browserName,
      request,
    }) => {
      test.skip(browserName !== "chromium", "axe runs in Chromium only");
      const { address } = await lake(request);
      await skipTheTour(request);
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);

      await page.goto(address);
      await expect(page.getByRole("heading", { level: 2, name: "Facts" })).toBeVisible();
      expect(await violations(page), "Overview").toEqual([]);
      expect(await sidewaysOverflow(page), "Overview, sideways").toBeLessThanOrEqual(0);
      await page.goto(`${address}/columns`);
      await expect(page.getByRole("region", { name: "Table columns" })).toBeVisible();
      expect(await violations(page), "Columns").toEqual([]);
      expect(await sidewaysOverflow(page), "Columns, sideways").toBeLessThanOrEqual(0);
      await page.context().close();
    });
  }
}
