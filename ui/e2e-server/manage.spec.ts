import { expect, test } from "@playwright/test";

import {
  administrator,
  createEnvironment,
  createNamespace,
  createTable,
  invitedPerson,
  makeAdministrator,
  NO_PASSKEY,
  queryStatus,
  recordOrphanedVersion,
  skipTheWelcome,
  tableStatus,
  uniqueName,
  violations,
} from "./helpers";

type Request = Parameters<typeof createEnvironment>[0];

/**
 * A new environment with `gold.sales.fact_sales`, so that each test acts on its own data on the
 * run's one shared server.
 *
 * @param request - The request context.
 * @returns The environment's name and the table page's address.
 */
async function lake(request: Request): Promise<{ environment: string; address: string }> {
  const environment = uniqueName("mng");
  await createEnvironment(request, environment);
  await createNamespace(request, environment, "gold.sales");
  await createTable(request, environment, "gold.sales", "fact_sales", ["id", "store_id", "amount"]);
  return { environment, address: `/catalog/${environment}/gold.sales/fact_sales` };
}

test("an administrator with a passkey drops a table, finds it in Dropped and restores it", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const { environment, address } = await lake(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    makeAdministrator(request, name),
  );
  await skipTheWelcome(page);

  await page.goto(address);
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Drop…" }).click();
  const dialog = page.getByRole("dialog", { name: "Drop gold.sales.fact_sales?" });
  expect(await violations(page), "the Drop dialog").toEqual([]);
  // The passkey touch the server asks for is answered by the virtual authenticator.
  await dialog.getByRole("button", { name: "Drop the table" }).click();

  await expect(page).toHaveURL(
    new RegExp(`/catalog\\?env=${environment}&ns=gold\\.sales&view=dropped$`),
  );
  await expect(page.getByText("Dropped gold.sales.fact_sales.")).toBeVisible();
  expect(await tableStatus(request, environment, "gold.sales", "fact_sales")).toBe(404);
  expect(await violations(page), "the Dropped view").toEqual([]);

  await page.getByRole("button", { name: "Undrop gold.sales.fact_sales" }).click();
  await expect(page.getByText("Restored gold.sales.fact_sales.")).toBeVisible();
  expect(await tableStatus(request, environment, "gold.sales", "fact_sales")).toBe(200);
  await page.context().close();
});

test("Retention on tomorrow's day puts the table in Expiring with its rule", async ({
  browser,
  request,
}) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);
  const tomorrow = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString().slice(0, 10);

  await page.goto(address);
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Retention…" }).click();
  const dialog = page.getByRole("dialog", { name: `Retention of gold.sales.fact_sales` });
  await dialog.getByText("On a day").click();
  await dialog.getByRole("textbox", { name: "Day" }).fill(tomorrow);
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog.getByText(/set for the table by admin/)).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();

  await page.goto(`/catalog?env=${environment}&view=expiring`);
  const row = page.getByRole("row").filter({ hasText: "fact_sales" });
  await expect(row).toBeVisible();
  await expect(row).toContainText("set for the table by admin");
  expect(await violations(page), "the Expiring view").toEqual([]);
  await page.context().close();
});

test("Maintain now queues a maintenance query", async ({ browser, request }) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(address);
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Maintain now…" }).click();
  const dialog = page.getByRole("dialog", { name: "Maintain gold.sales.fact_sales now" });
  await dialog.getByRole("button", { name: "Maintain now" }).click();
  const queued = dialog.getByText(/^Maintenance is queued as query /);
  await expect(queued).toBeVisible();
  const id = /query (\S+)\.$/.exec((await queued.textContent()) ?? "")?.[1];
  expect(id).toBeDefined();
  // No worker runs here, so the query stays queued.
  expect(await queryStatus(request, environment, id ?? "")).toBe("QUEUED");
  await page.context().close();
});

test("the References tab of a table without a model says none are declared", async ({
  browser,
  request,
}) => {
  const { address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(`${address}/references`);
  await expect(
    page.getByText("The table's header declares no references and none point here."),
  ).toBeVisible();
  expect(await violations(page), "the References tab").toEqual([]);
  await page.context().close();
});

test("a local administrator without a passkey cannot drop: the dialog says why and the table stays", async ({
  browser,
  request,
}) => {
  const { environment, address } = await lake(request);
  const page = await administrator(browser, request);

  await page.goto(address);
  await page.getByRole("button", { name: "Actions" }).click();
  await page.getByRole("menuitem", { name: "Drop…" }).click();
  const dialog = page.getByRole("dialog", { name: "Drop gold.sales.fact_sales?" });
  await dialog.getByRole("button", { name: "Drop the table" }).click();
  // The session has no passkey, so there is nothing to touch: the server's 409 NoPasskeyError,
  // in the interface's words for it.
  await expect(dialog.getByRole("alert")).toContainText(NO_PASSKEY);
  await expect(dialog).toBeVisible();
  expect(await tableStatus(request, environment, "gold.sales", "fact_sales")).toBe(200);
  await page.context().close();
});

test("Orphans lists a table no project builds any more, with Drop", async ({
  browser,
  request,
}) => {
  const { environment } = await lake(request);
  await createTable(request, environment, "silver", "old_totals", ["id"]);
  await recordOrphanedVersion(request, environment, uniqueName("analytics"), ["silver.old_totals"]);
  const page = await administrator(browser, request);

  await page.goto(`/catalog?env=${environment}&view=orphans`);
  const row = page.getByRole("row").filter({ hasText: "old_totals" });
  await expect(row).toBeVisible();
  await expect(row.getByRole("button", { name: "Drop silver.old_totals" })).toBeVisible();
  expect(await violations(page), "the Orphans view").toEqual([]);
  await page.context().close();
});
