import { expect, type Page, test } from "@playwright/test";

import {
  ageThePasskeyTouch,
  createEnvironment,
  grantOn,
  grantRole,
  invitedPerson,
  lakeRoot,
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
 * Checks a page with axe in light and in dark.
 *
 * @param page - The page.
 * @param label - What the page is.
 */
async function axeInBothThemes(page: Page, label: string): Promise<void> {
  for (const scheme of ["light", "dark"] as const) {
    // Without motion, so the theme's fade is over before axe reads the colours.
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    expect(await violations(page), `${label}, ${scheme}`).toEqual([]);
  }
}

/**
 * Deletes the environment whose page is open, after typing its name.
 *
 * @param page - The page.
 * @param name - The environment's name.
 * @param purge - Whether its files go at the next clean-up.
 */
async function deleteFromItsPage(page: Page, name: string, purge: boolean): Promise<void> {
  await page.getByRole("button", { name: `Delete environment ${name}` }).click();
  const dialog = page.getByRole("dialog");
  if (purge) await dialog.getByText(/^Delete the files at the next/).click();
  await dialog.getByRole("textbox", { name: `Type ${name} to delete it` }).fill(name);
  await dialog.getByRole("button", { name: "Delete environment" }).click();
  await expect(page).toHaveURL(/\/settings\/environments$/);
}

test("an environment's life", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  test.setTimeout(90_000);
  const environment = uniqueName("e");
  const { page, name } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  // An old touch: creating asks for the passkey again before the request is sent.
  ageThePasskeyTouch(name);
  await page.goto("/settings/environments");
  await page.getByRole("button", { name: "Create environment" }).click();
  const create = page.getByRole("dialog");
  await create.getByRole("textbox", { name: "Name" }).fill(environment);
  await create.getByRole("textbox", { name: "Root" }).fill(`file://${lakeRoot(environment)}`);
  await create.getByRole("button", { name: "Create environment" }).click();
  await expect(page.getByRole("heading", { level: 1, name: environment })).toBeVisible();
  await expect(page.getByText(/Created\. Give people access under/)).toBeVisible();

  await page.getByRole("button", { name: `Set retention of ${environment}` }).click();
  const retention = page.getByRole("dialog");
  await retention.getByText("Tables expire after", { exact: true }).click();
  await retention.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("tables expire after 30 days")).toBeVisible();

  await page.getByRole("button", { name: `Protect ${environment}` }).click();
  await expect(page.getByRole("button", { name: `Unprotect ${environment}` })).toBeVisible();
  await expect(page.getByText("Protected environments cannot be deleted.")).toBeVisible();
  await page.getByRole("button", { name: `Unprotect ${environment}` }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Unprotect" }).click();
  await expect(page.getByRole("button", { name: `Protect ${environment}` })).toBeVisible();

  await deleteFromItsPage(page, environment, false);
  await expect(page.getByText(`Deleted ${environment}. It can be restored until`)).toBeVisible();
  const deleted = page.getByRole("region", { name: "Deleted environments" });
  await expect(deleted.getByText(environment)).toBeVisible();

  await page.getByRole("button", { name: `Restore ${environment}` }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Restore" }).click();
  await expect(page.getByText(`Restored ${environment}.`)).toBeVisible();
  const live = page.getByRole("region", { name: "Environments", exact: true });
  await live.getByRole("link", { name: environment }).click();
  await expect(page.getByRole("heading", { level: 1, name: environment })).toBeVisible();

  await deleteFromItsPage(page, environment, true);
  await expect(
    page.getByText(`Deleted ${environment}. Its files are deleted at the next clean-up.`),
  ).toBeVisible();
  expect(errors).toEqual([]);
  await page.context().close();
});

test("an environment manager on one environment", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("m");
  await createEnvironment(request, environment);
  // MANAGE_ENVIRONMENT does not read the environment; VIEWER does.
  const { page } = await invitedPerson(browser, request, async (person) => {
    await grantRole(request, person, "VIEWER", environment);
    await grantOn(request, person, "MANAGE_ENVIRONMENT", environment);
  });
  await skipTheWelcome(page);

  await page.goto("/settings/environments");
  await expect(page.getByRole("link", { name: environment })).toBeVisible();
  await expect(page.getByRole("button", { name: "Create environment" })).toHaveCount(0);
  await page.getByRole("link", { name: environment }).click();
  await expect(page.getByRole("heading", { level: 1, name: environment })).toBeVisible();
  await expect(page.getByRole("button", { name: `Set retention of ${environment}` })).toBeVisible();
  await expect(
    page.getByRole("button", { name: `Delete environment ${environment}` }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Usage" })).toBeVisible();
  await expect(page.getByRole("button", { name: `Protect ${environment}` })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Leftover directories" })).toHaveCount(0);
  await page.context().close();
});

test("leftover directories on a fresh environment, and axe", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("l");
  await createEnvironment(request, environment);
  const { page } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  await page.goto("/settings/environments");
  await expect(page.getByRole("link", { name: environment })).toBeVisible();
  await axeInBothThemes(page, "the list");
  await page.goto(`/settings/environments/${environment}`);
  await page.getByRole("button", { name: "Find" }).click();
  await expect(page.getByText("Nothing left over.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Usage" })).toBeVisible();
  await axeInBothThemes(page, "an environment's page");
  expect(errors).toEqual([]);
  await page.context().close();
});
