import { expect, type Page, test } from "@playwright/test";

import {
  administrator,
  auditEventCount,
  createEnvironment,
  createNamespace,
  createTable,
  freshPage,
  grantTableRead,
  invitedPerson,
  queryStatus,
  recordRead,
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

test("an administrator opens a table from the list and sees its six tabs", async ({
  browser,
  request,
}) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`/catalog?env=${environment}&ns=gold.sales`);
  await page.locator(`a[href="${address}"]`).click();
  await expect(page).toHaveURL(new RegExp(`${address}$`));
  await expect(page.getByRole("heading", { level: 1, name: "fact_sales" })).toBeFocused();
  await expect(tabsOf(page).getByRole("link")).toHaveText([
    "Overview",
    "Columns",
    "Preview",
    "Lineage",
    "Health",
    "Access",
  ]);
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
    test(`axe finds nothing on every tab (${scheme}, ${String(width)} px)`, async ({
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
      for (const [tab, heading] of [
        ["/lineage", "Nothing upstream or downstream."],
        ["/health", "Checks"],
        ["/access", "Column masks"],
      ] as const) {
        await page.goto(`${address}${tab}`);
        await expect(
          tab === "/lineage"
            ? page.getByText(heading)
            : page.getByRole("heading", { level: 2, name: heading }),
        ).toBeVisible();
        if (tab === "/access") {
          // Every level has answered, so axe sees the finished tab.
          await expect(page.getByText("No column masks on this table.")).toBeVisible();
          await expect(page.getByText("Reading the grants…")).toHaveCount(0);
        }
        expect(await violations(page), tab).toEqual([]);
        expect(await sidewaysOverflow(page), `${tab}, sideways`).toBeLessThanOrEqual(0);
      }
      await page.context().close();
    });
  }
}

test("an administrator sees who has access, by name", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  const { page: reader, name } = await invitedPerson(browser, request, (person) =>
    grantTableRead(request, person, environment, "gold.sales", "fact_sales"),
  );
  const page = await administrator(browser, request);

  await page.goto(`${address}/access`);
  const who = page
    .locator("section.access-section")
    .filter({ has: page.getByRole("heading", { level: 2, name: "Who has access" }) })
    .first();
  await expect(who.getByRole("heading", { level: 3, name: "On this table" })).toBeVisible();
  const row = who.getByRole("row").filter({ has: page.getByRole("link", { name, exact: true }) });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("READ_TABLE");
  await expect(row.getByRole("link", { name, exact: true })).toHaveAttribute(
    "href",
    new RegExp(`${encodeURIComponent(name)}$`),
  );
  // The owner's grant at the instance is not listed, and the tab says so.
  await expect(page.getByText("Grants on the whole instance")).toBeVisible();
  await page.context().close();
  await reader.context().close();
});

test("a reader's Access tab says read yes, write and drop no, with the next step", async ({
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
  const asked: string[] = [];
  page.on("request", (sent) => {
    if (/\/(grants|row-filters|column-masks)\b/.test(new URL(sent.url()).pathname)) {
      asked.push(sent.url());
    }
  });

  await page.goto(`${address}/access`);
  const mine = page.getByRole("table").first();
  const answer = (action: string): ReturnType<Page["getByRole"]> =>
    mine.getByRole("row").filter({ has: page.getByRole("cell", { name: action, exact: true }) });
  await expect(answer("Read")).toContainText("Allowed");
  await expect(answer("Write")).toContainText("Not allowed");
  await expect(answer("Drop")).toContainText("Not allowed");
  await expect(answer("Edit texts")).toContainText("Not allowed");
  // The next step is a sentence, never an empty cell.
  await expect(answer("Write").getByRole("cell").last()).toHaveText(/\S{3,}/);
  await expect(answer("Edit texts").getByRole("cell").last()).toContainText("Ask the owner");
  // Only an administrator sees, and asks for, who has access.
  await expect(page.getByRole("heading", { level: 2, name: "Who has access" })).toHaveCount(0);
  await page.waitForLoadState("networkidle");
  expect(asked).toEqual([]);
  await page.context().close();
});

test("a reader's Health tab asks without readers", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  const { page, name } = await invitedPerson(browser, request, (person) =>
    grantTableRead(request, person, environment, "gold.sales", "fact_sales"),
  );
  await skipTheWelcome(page);
  await recordRead(request, environment, "gold.sales", "fact_sales", name);
  const asked: string[] = [];
  page.on("request", (sent) => {
    if (new URL(sent.url()).pathname.endsWith("/tables/fact_sales/health")) {
      asked.push(sent.url());
    }
  });

  await page.goto(`${address}/health`);
  await expect(page.getByRole("heading", { level: 2, name: "Checks" })).toBeVisible();
  // Everyone may see how many people read the table; only an administrator sees who.
  await expect(page.getByText("1 person read it in the last 7 days.")).toBeVisible();
  await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
  expect(asked.length).toBeGreaterThan(0);
  for (const url of asked) {
    expect(new URL(url).searchParams.get("readers"), url).toBe("false");
  }
  await page.context().close();
});

test("an administrator's Health read writes one ACTIVITY_VIEWED entry", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  const { page: reader, name } = await invitedPerson(browser, request, (person) =>
    grantTableRead(request, person, environment, "gold.sales", "fact_sales"),
  );
  await recordRead(request, environment, "gold.sales", "fact_sales", name);
  const page = await administrator(browser, request);
  const before = await auditEventCount(request, environment, "ACTIVITY_VIEWED");
  expect(before).toBe(0);
  const asked: string[] = [];
  page.on("request", (sent) => {
    if (new URL(sent.url()).pathname.endsWith("/tables/fact_sales/health")) {
      asked.push(sent.url());
    }
  });

  // Overview reads the health without the readers: nothing is recorded.
  await page.goto(address);
  await expect(page.getByRole("heading", { level: 2, name: "Facts" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(await auditEventCount(request, environment, "ACTIVITY_VIEWED")).toBe(before);

  await page.goto(`${address}/health#readers`);
  await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
  expect(asked.some((url) => new URL(url).searchParams.get("readers") !== "false")).toBe(true);
  await expect
    .poll(() => auditEventCount(request, environment, "ACTIVITY_VIEWED"))
    .toBe(before + 1);
  await page.context().close();
  await reader.context().close();
});

test("lineage of a table no project defines says nothing is there", async ({
  browser,
  request,
}) => {
  const { address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`${address}/lineage`);
  await expect(page.getByText("Nothing upstream or downstream.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Full screen" })).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.context().close();
});
