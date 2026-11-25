import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  administrator,
  ageThePasskeyTouch,
  createEnvironment,
  createNamespace,
  createTable,
  grantOn,
  invitedPerson,
  makeAdministrator,
  namespaceStatus,
  skipTheWelcome,
  uniqueName,
  violations,
  writeRawFile,
} from "./helpers";

/**
 * Opens the Catalog at a place and waits for its Actions button.
 *
 * @param page - The page.
 * @param environment - The environment.
 * @param namespace - The dotted namespace; the environment itself without.
 * @returns The place's Actions button.
 */
async function openPlace(page: Page, environment: string, namespace?: string): Promise<Locator> {
  await page.goto(
    `/catalog?env=${environment}${namespace === undefined ? "" : `&ns=${namespace}`}`,
  );
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
  const actions = page.getByRole("button", {
    name: `Actions for ${namespace ?? environment}`,
    exact: true,
  });
  await expect(actions).toBeVisible();
  return actions;
}

/**
 * Opens an item of the place menu.
 *
 * @param actions - The place's Actions button.
 * @param item - The item's name.
 */
async function choose(actions: Locator, item: string): Promise<void> {
  await actions.click();
  await actions.page().getByRole("menuitem", { name: item }).click();
}

/**
 * Presses a dialog's confirm button once it may be pressed: not while it waits to learn whether
 * the environment is protected.
 *
 * @param button - The confirm button.
 */
async function confirm(button: Locator): Promise<void> {
  await expect(button).toBeEnabled();
  await expect(button).not.toHaveAttribute("data-pending");
  await button.click();
}

/**
 * The note a create or a delete leaves above the result line.
 *
 * @param page - The page.
 * @param text - The whole sentence.
 * @returns The note.
 */
function placeNote(page: Page, text: string): Locator {
  return page.locator(".catalog-result").filter({ hasText: text });
}

test("an administrator with a passkey creates a namespace, sees it in the tree and deletes it", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const environment = uniqueName("plc");
  await createEnvironment(request, environment);
  const { page } = await invitedPerson(browser, request, (name) =>
    makeAdministrator(request, name),
  );
  await skipTheWelcome(page);

  await choose(await openPlace(page, environment), "New namespace…");
  const create = page.getByRole("dialog", { name: `New namespace in ${environment}` });
  await create.getByRole("textbox", { name: "Path" }).fill("landing");
  await confirm(create.getByRole("button", { name: "Create" }));

  await expect(page).toHaveURL(new RegExp(`/catalog\\?env=${environment}&ns=landing$`));
  await expect(placeNote(page, "Created namespace landing.")).toBeFocused();
  await expect(
    page.getByRole("treegrid").locator(`a[href="/catalog?env=${environment}&ns=landing"]`),
  ).toBeVisible();
  expect(await namespaceStatus(request, environment, "landing")).toBe(200);
  expect(await violations(page), "the note after a create").toEqual([]);

  await choose(
    page.getByRole("button", { name: "Actions for landing", exact: true }),
    "Delete namespace…",
  );
  const remove = page.getByRole("dialog", { name: "Delete namespace landing?" });
  expect(await violations(page), "the Delete namespace dialog").toEqual([]);
  // A passkey touch the server may ask for is answered by the virtual authenticator.
  await confirm(remove.getByRole("button", { name: "Delete the namespace" }));

  await expect(page).toHaveURL(new RegExp(`/catalog\\?env=${environment}$`));
  await expect(placeNote(page, "Deleted namespace landing.")).toBeFocused();
  await expect(
    page.getByRole("treegrid").locator(`a[href="/catalog?env=${environment}&ns=landing"]`),
  ).toHaveCount(0);
  expect(await namespaceStatus(request, environment, "landing")).toBe(404);
  expect(await violations(page), "the note after a delete").toEqual([]);
  await page.context().close();
});

test("Delete namespace after the passkey touch went stale asks for a new touch first and lands on the environment", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const environment = uniqueName("plc");
  await createEnvironment(request, environment);
  await createNamespace(request, environment, "landing");
  const { page, name } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);
  const sent: string[] = [];
  page.on("request", (sentRequest) => {
    const path = new URL(sentRequest.url()).pathname;
    if (sentRequest.method() !== "GET" && path.startsWith("/api/")) {
      sent.push(`${sentRequest.method()} ${path}`);
    }
  });
  ageThePasskeyTouch(name);

  await choose(await openPlace(page, environment, "landing"), "Delete namespace…");
  const remove = page.getByRole("dialog", { name: "Delete namespace landing?" });
  await confirm(remove.getByRole("button", { name: "Delete the namespace" }));

  await expect(page).toHaveURL(new RegExp(`/catalog\\?env=${environment}$`));
  await expect(placeNote(page, "Deleted namespace landing.")).toBeFocused();
  expect(await namespaceStatus(request, environment, "landing")).toBe(404);
  const deletion = `DELETE /api/v1/environments/${environment}/namespaces/landing`;
  // The stale touch is renewed before the deletion is sent (`guarded`), which is sent once.
  expect(sent.filter((line) => !line.endsWith("/recent"))).toEqual([
    "POST /api/v1/sign-in/step-up/options",
    "POST /api/v1/sign-in/step-up",
    deletion,
  ]);
  await page.context().close();
});

test("an administrator registers an external table over a CSV under raw/ and lands on its page with its columns", async ({
  browser,
  request,
}) => {
  const environment = uniqueName("plc");
  await createEnvironment(request, environment);
  writeRawFile(environment, "raw/orders/a.csv", "id,amount\n1,2.5\n");
  const page = await administrator(browser, request);

  await choose(await openPlace(page, environment, "bronze"), "New external table…");
  const dialog = page.getByRole("dialog", { name: "New external table in bronze" });
  await dialog.getByRole("textbox", { name: "Name" }).fill("orders");
  await dialog.getByRole("textbox", { name: "Location" }).fill("raw/orders/*.csv");
  // The format follows the pattern's ending until one is picked.
  await expect(dialog.getByRole("radio", { name: "CSV" })).toBeChecked();
  expect(await violations(page), "the filled form").toEqual([]);
  await confirm(dialog.getByRole("button", { name: "Register" }));

  await expect(page).toHaveURL(new RegExp(`/catalog/${environment}/bronze/orders$`));
  await expect(page.getByRole("heading", { level: 1, name: "orders" })).toBeVisible();
  await expect(dialog).toBeHidden();
  // The dialog that held the focus is gone; the table page's heading holds it, never the body.
  await expect(page.getByRole("heading", { level: 1, name: "orders" })).toBeFocused();

  await page
    .getByRole("navigation", { name: "Sections of the table" })
    .getByRole("link", { name: "Columns" })
    .click();
  await expect(page).toHaveURL(new RegExp(`/catalog/${environment}/bronze/orders/columns$`));
  const columns = page.getByRole("region", { name: "Table columns" });
  for (const column of ["id", "amount"]) {
    await expect(columns.getByRole("cell", { name: column, exact: true })).toBeVisible();
  }
  expect(await violations(page), "the Columns tab of the external table").toEqual([]);
  await page.context().close();
});

test("Browse… lists raw/ from the server, fills the location from a file, and the table registers", async ({
  browser,
  request,
}) => {
  const environment = uniqueName("plc");
  await createEnvironment(request, environment);
  writeRawFile(environment, "raw/returns/2026-11-01.csv", "id,amount\n1,2.5\n");
  writeRawFile(environment, "raw/returns/2026-11-02.csv", "id,amount\n2,3.5\n");
  const page = await administrator(browser, request);

  await choose(await openPlace(page, environment, "bronze"), "New external table…");
  const dialog = page.getByRole("dialog", { name: "New external table in bronze" });
  await dialog.getByRole("textbox", { name: "Name" }).fill("returns");
  await dialog.getByRole("button", { name: "Browse…" }).click();
  await dialog.getByRole("button", { name: "returns/" }).click();
  await expect(dialog.getByRole("cell", { name: "2026-11-02.csv" })).toBeVisible();
  expect(await violations(page), "Browse… in a folder").toEqual([]);
  await dialog.getByRole("button", { name: "Use this folder" }).click();
  await expect(dialog.getByRole("textbox", { name: "Location" })).toHaveValue("raw/returns/*.csv");
  await expect(dialog.getByRole("radio", { name: "CSV" })).toBeChecked();
  await confirm(dialog.getByRole("button", { name: "Register" }));

  await expect(page).toHaveURL(new RegExp(`/catalog/${environment}/bronze/returns$`));
  await choose(await openPlace(page, environment, "bronze"), "New external table…");
  const again = page.getByRole("dialog", { name: "New external table in bronze" });
  await again.getByRole("button", { name: "Browse…" }).click();
  await expect(again.getByText("in use by bronze.returns")).toBeVisible();
  await expect(again.getByRole("button", { name: "returns/" })).toBeDisabled();
  await page.context().close();
});

test("deleting a namespace that holds a table shows the server's sentence and the namespace stays", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "the virtual authenticator is CDP");
  const environment = uniqueName("plc");
  await createEnvironment(request, environment);
  await createTable(request, environment, "silver", "held", ["id"]);
  // The person sees silver and may delete it, but may not read its table: the tree counts none.
  const { page } = await invitedPerson(browser, request, async (name) => {
    await grantOn(request, name, "READ_ENVIRONMENT", environment);
    await grantOn(request, name, "READ_NAMESPACE", environment);
    await grantOn(request, name, "DROP_NAMESPACE", environment, "silver");
  });
  await skipTheWelcome(page);

  const actions = await openPlace(page, environment, "silver");
  await choose(actions, "Delete namespace…");
  const dialog = page.getByRole("dialog", { name: "Delete namespace silver?" });
  await confirm(dialog.getByRole("button", { name: "Delete the namespace" }));

  await expect(dialog.getByRole("alert")).toHaveText(
    "silver is not empty; drop what it holds first.",
  );
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/catalog\\?env=${environment}&ns=silver$`));
  expect(await namespaceStatus(request, environment, "silver")).toBe(200);
  expect(await violations(page), "the refused delete").toEqual([]);

  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  await expect(actions).toBeFocused();
  await page.context().close();
});
