import { readFile } from "node:fs/promises";

import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  ageThePasskeyTouch,
  applyAccessFile,
  createEnvironment,
  createRowFilter,
  createTable,
  grantInstanceAction,
  invitedPerson,
  makeAdministrator,
  skipTheWelcome,
  uniqueName,
  violations,
} from "./helpers";

/**
 * Records the console errors of a page.
 *
 * @param page - The page.
 * @returns The list, filled as errors are logged.
 */
function consoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

/**
 * Creates an environment with the table `gold.stations` (id, region, email).
 *
 * @param request - The request context.
 * @returns The environment's name.
 */
async function stationsIn(request: Parameters<typeof createEnvironment>[0]): Promise<string> {
  const environment = uniqueName("e");
  await createEnvironment(request, environment);
  await createTable(request, environment, "gold", "stations", ["id", "region", "email"]);
  return environment;
}

/**
 * Fills Add filter or mask and creates the policy; the passkey touch is answered by the virtual
 * authenticator.
 *
 * @param page - The page, on Row filters & masks.
 * @param fields - What to fill.
 * @param fields.admin - Whether the person is an administrator, who is offered the names.
 * @param fields.mask - Whether it is a column mask.
 * @param fields.who - Who it is for.
 * @param fields.environment - The environment.
 * @param fields.column - A mask's column.
 * @param fields.expression - The expression.
 */
async function addPolicy(
  page: Page,
  fields: {
    admin: boolean;
    mask: boolean;
    who: string;
    environment: string;
    column?: string;
    expression: string;
  },
): Promise<void> {
  await page.getByRole("button", { name: "Add filter or mask" }).click();
  const dialog = page.getByRole("dialog", { name: "Add filter or mask" });
  if (fields.mask) await dialog.getByText("Column mask", { exact: true }).click();
  // The Name is a name picker (#802): an administrator picks who from the suggestions; anyone
  // else may not list principals, and the name is sent as typed.
  await dialog.getByRole("combobox", { name: "Name", exact: true }).fill(fields.who);
  if (fields.admin) {
    await page.getByRole("option", { name: `${fields.who}, Person` }).click();
  }
  // A person who may list no environment types its name; anyone else chooses it.
  const typed: Locator = dialog.getByRole("textbox", { name: "Environment" });
  const environment: Locator = dialog.getByRole("button", { name: /Environment$/ });
  await expect(typed.or(environment)).toBeVisible();
  const listed = !(await typed.isVisible());
  if (!listed) {
    await typed.fill(fields.environment);
  } else if (!(await environment.textContent())?.includes(fields.environment)) {
    await environment.click();
    await page.getByRole("option", { name: fields.environment }).click();
  }
  await dialog.getByRole("textbox", { name: "Namespace" }).fill("gold");
  // The table field is a name picker (#784): with a listed environment it suggests the tables
  // the person may read there, and choosing one fills in its namespace; with a typed one it asks
  // nothing and the name is sent as typed.
  await dialog.getByRole("combobox", { name: "Table", exact: true }).fill("stations");
  if (listed) {
    await page.getByRole("option", { name: "stations, gold" }).click();
    await expect(dialog.getByRole("textbox", { name: "Namespace" })).toHaveValue("gold");
  }
  if (fields.column !== undefined) {
    await dialog.getByRole("textbox", { name: "Column" }).fill(fields.column);
  }
  await dialog.getByRole("textbox", { name: "Expression" }).fill(fields.expression);
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(dialog).toBeHidden();
}

/**
 * Deletes a policy by its Delete button, confirming in its dialog.
 *
 * @param page - The page.
 * @param button - The Delete button's name.
 * @param confirm - The dialog's confirm button's name.
 */
async function deletePolicy(page: Page, button: RegExp, confirm: string): Promise<void> {
  await page.getByRole("button", { name: button }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: confirm }).click();
  await expect(dialog).toBeHidden();
}

test("an administrator filters rows and masks a column", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = await stationsIn(request);
  const { page, name } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  await page.goto(`/settings/access/filters-and-masks?environment=${environment}`);
  await expect(page.getByText("No row filter matches these filters.")).toBeVisible();
  await addPolicy(page, {
    admin: true,
    mask: false,
    who: name,
    environment,
    expression: "region = 'EU'",
  });
  const filters = page.getByRole("region", { name: "Row filters" });
  await expect(filters.getByText("region = 'EU'")).toBeVisible();
  await expect(page.getByText("Row filter created.")).toBeVisible();
  await addPolicy(page, {
    admin: true,
    mask: true,
    who: name,
    environment,
    column: "email",
    expression: "'***'",
  });
  const masks = page.getByRole("region", { name: "Column masks" });
  await expect(masks.getByText("'***'")).toBeVisible();
  // The Table filter suggests the tables there by namespace and part of the name (#802).
  await page
    .getByRole("combobox", { name: "Table (namespace.table)", exact: true })
    .fill("gold.stat");
  await page.getByRole("option", { name: "gold.stations" }).click();
  await expect(page).toHaveURL(/[?&]table=gold\.stations(&|$)/);
  await expect(filters.getByText("region = 'EU'")).toBeVisible();

  await page.goto(`/catalog/${environment}/gold/stations/access`);
  await expect(page.getByText("region = 'EU'")).toBeVisible();
  await expect(page.getByText("'***'")).toBeVisible();
  await page.getByRole("link", { name: "Manage row filters and masks" }).click();
  await expect(page).toHaveURL(/\/settings\/access\/filters-and-masks\?environment=/);

  // An old touch: each deletion asks for the passkey again before it is sent.
  ageThePasskeyTouch(name);
  await deletePolicy(page, /^Delete the row filter on /, "Delete the row filter");
  await expect(page.getByText("No row filter matches these filters.")).toBeVisible();
  ageThePasskeyTouch(name);
  await deletePolicy(page, /^Delete the mask of email/, "Delete the column mask");
  await expect(page.getByText("No column mask matches these filters.")).toBeVisible();
  expect(errors).toEqual([]);
  await page.context().close();
});

test("a person with MANAGE_POLICY alone", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = await stationsIn(request);
  const { page, name } = await invitedPerson(browser, request, (person) =>
    grantInstanceAction(request, person, "MANAGE_POLICY"),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);
  const asked: string[] = [];
  page.on("request", (made) => asked.push(new URL(made.url()).pathname));

  await page.goto("/settings/access");
  await expect(page).toHaveURL(/\/settings\/access\/filters-and-masks$/);
  await expect(
    page.getByRole("navigation", { name: "Settings places" }).getByRole("link"),
  ).toHaveText(["Status", "Row filters & masks", "What I may do"]);
  // Without READ_TABLE the server cannot check a column for them: a filter that reads none.
  await page.goto("/settings/access/filters-and-masks");
  await addPolicy(page, {
    admin: false,
    mask: false,
    who: name,
    environment,
    expression: "has_group('analysts')",
  });
  await expect(
    page.getByRole("region", { name: "Row filters" }).getByText("has_group('analysts')"),
  ).toBeVisible();
  await deletePolicy(
    page,
    new RegExp(`^Delete the row filter on ${environment} .* for ${name}$`),
    "Delete the row filter",
  );
  await expect(page.getByText("has_group('analysts')")).toHaveCount(0);

  await page.goto("/settings/access/access-file");
  await expect(
    page.getByRole("heading", { level: 1, name: "You cannot use Access file" }),
  ).toBeVisible();
  expect(asked.filter((path) => /^\/api\/v1\/access\/(status|export)/.test(path))).toEqual([]);
  // The Name asked for no principal or group, which they may not list (no refusal is logged).
  expect(asked.filter((path) => /^\/api\/v1\/(principals|groups)$/.test(path))).toEqual([]);
  expect(errors).toEqual([]);
  await page.context().close();
});

test("a managed environment is read-only", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = await stationsIn(request);
  const { page, name } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await createRowFilter(request, {
    principal: name,
    group: null,
    environment,
    namespace: "gold",
    table: "stations",
    expression: "region = 'EU'",
  });
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  await page.goto("/settings/access/access-file");
  await page.getByRole("button", { name: "Download access file" }).click();
  const dialog = page.getByRole("dialog", { name: "Download access file" });
  await dialog.getByText("Some environments").click();
  await dialog.getByText(environment, { exact: true }).click();
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download" }).click();
  const saved = await download;
  expect(saved.suggestedFilename()).toBe("access.yaml");
  const text = await readFile(await saved.path(), "utf8");
  expect(text).toContain(environment);
  await applyAccessFile(request, text);

  await page.goto(`/settings/access/filters-and-masks?environment=${environment}`);
  const filters = page.getByRole("region", { name: "Row filters" });
  await expect(filters.getByText("region = 'EU'")).toBeVisible();
  await expect(filters).toContainText("⌂ Git");
  await expect(filters.getByRole("button", { name: /^Delete/ })).toHaveCount(0);

  await page.goto("/settings/access/access-file");
  const scopes = page.getByRole("region", { name: "Managed scopes" });
  await expect(scopes.getByRole("row").filter({ hasText: environment })).toContainText("In sync");
  expect(errors).toEqual([]);
  await page.context().close();
});

for (const theme of ["light", "dark"] as const) {
  test(`axe on Row filters & masks and Access file, ${theme}`, async ({
    browser,
    browserName,
    request,
  }) => {
    test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
    const { page } = await invitedPerson(browser, request, (person) =>
      makeAdministrator(request, person),
    );
    await skipTheWelcome(page);
    await page.emulateMedia({ colorScheme: theme });

    await page.goto("/settings/access/filters-and-masks");
    await expect(page.getByRole("heading", { level: 2, name: "Column masks" })).toBeVisible();
    await expect(page.getByText("Reading the column masks…")).toBeHidden();
    expect(await violations(page), "Row filters & masks").toEqual([]);

    await page.goto("/settings/access/access-file");
    await expect(page.getByRole("heading", { level: 2, name: "Plan and apply" })).toBeVisible();
    await expect(page.getByText("Reading the access files' status…")).toBeHidden();
    expect(await violations(page), "Access file").toEqual([]);
    await page.context().close();
  });
}
