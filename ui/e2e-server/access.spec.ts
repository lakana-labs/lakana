import { expect, type Page, test } from "@playwright/test";

import {
  createEnvironment,
  grantRole,
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
 * Sets the colour scheme the page is drawn in.
 *
 * @param page - The page.
 * @param theme - Light or dark.
 */
async function drawIn(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.emulateMedia({ colorScheme: theme });
}

test("an administrator makes a group, fills it and grants it a role", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("acc");
  await createEnvironment(request, environment);
  const { page, name } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);
  const group = uniqueName("g");

  await page.goto("/settings/access/groups");
  await page.getByRole("button", { name: "Create group" }).click();
  const create = page.getByRole("dialog", { name: "Create group" });
  await create.getByRole("textbox", { name: "Name" }).fill(group);
  await create.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("link", { name: group })).toBeVisible();

  await page.getByRole("link", { name: group }).click();
  await expect(page.getByRole("heading", { level: 1, name: group })).toBeVisible();
  // The field suggests the person from the server's list of principals (#784).
  await page.getByRole("combobox", { name: "Person or service account" }).fill(name);
  await page.getByRole("option", { name: `${name}, Person` }).click();
  await expect(page.getByRole("combobox", { name: "Person or service account" })).toHaveValue(name);
  // The passkey touch is asked for first, answered by the virtual authenticator.
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const members = page.getByRole("region", { name: `Members of ${group}` });
  await expect(members.getByRole("link", { name })).toBeVisible();

  await page.getByRole("link", { name: "Manage these grants" }).click();
  await expect(page).toHaveURL(new RegExp(`/settings/access/grants\\?group=${group}$`));
  await page.getByRole("button", { name: "Add grant" }).click();
  const add = page.getByRole("dialog", { name: "Add grant" });
  await expect(add.getByRole("radio", { name: "A group" })).toBeChecked();
  await expect(add.getByRole("combobox", { name: "Name" })).toHaveValue(group);
  await add.getByRole("button", { name: /Role$/ }).click();
  await page.getByRole("option", { name: /^ANALYST/ }).click();
  await add.getByRole("button", { name: /Environment$/ }).click();
  await page.getByRole("option", { name: environment }).click();
  await add.getByRole("button", { name: "Grant" }).click();
  await expect(add).toBeHidden();
  const grants = page.getByRole("region", { name: "Grants" });
  await expect(grants.getByRole("row").nth(1)).toContainText(`${group}GroupANALYST${environment}`);

  await page.goto(`/settings/access/groups/${group}`);
  await expect(page.getByRole("region", { name: `Grants of ${group}` })).toContainText(
    `ANALYST${environment}`,
  );
  await page.goto(`/settings/access/people/${name}`);
  const held = page.getByRole("region", { name: `Grants of ${name}` });
  await expect(held.getByRole("row").filter({ hasText: environment })).toContainText(group);

  await page.goto(`/settings/access/grants?group=${group}`);
  await page
    .getByRole("button", { name: `Remove the grant ANALYST on ${environment} for ${group}` })
    .click();
  await page
    .getByRole("dialog", { name: "Remove this grant?" })
    .getByRole("button", { name: "Remove the grant" })
    .click();
  await expect(page.getByText("No grant matches these filters.")).toBeVisible();

  await page.goto(`/settings/access/groups/${group}`);
  await page.getByRole("button", { name: `Delete group ${group}` }).click();
  const deleting = page.getByRole("dialog", { name: `Delete group ${group}?` });
  await deleting.getByRole("textbox").fill(group);
  await deleting.getByRole("button", { name: "Delete group" }).click();
  await expect(page).toHaveURL(/\/settings\/access\/groups$/);
  await expect(page.getByRole("link", { name: group })).toBeHidden();
  expect(errors).toEqual([]);
  await page.context().close();
});

test("a person sees what they may do", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("may");
  await createEnvironment(request, environment);
  const { page } = await invitedPerson(browser, request, (person) =>
    grantRole(request, person, "VIEWER", environment),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);
  const asked: string[] = [];
  page.on("request", (made) => asked.push(new URL(made.url()).pathname));

  await page.goto("/settings/access");
  await expect(page).toHaveURL(/\/settings\/access\/what-i-may-do$/);
  const grants = page.getByRole("region", { name: "The grants you hold" });
  await expect(grants.getByRole("row").nth(1)).toContainText(`VIEWER`);
  await expect(grants.getByRole("row").nth(1)).toContainText(environment);
  const scope = page.getByRole("region", { name: new RegExp(`${environment}$`) });
  await expect(scope.getByText("Read the environment")).toBeVisible();
  expect(await violations(page), "What I may do").toEqual([]);

  for (const [path, place] of [
    ["/settings/access/groups", "Groups"],
    ["/settings/access/grants", "Grants"],
  ] as const) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { level: 1, name: `You cannot use ${place}` }),
    ).toBeVisible();
  }
  await page.waitForLoadState("networkidle");
  expect(
    asked.filter((path) => path.startsWith("/api/v1/groups") || path.startsWith("/api/v1/grants")),
  ).toEqual([]);
  expect(errors).toEqual([]);
  await page.context().close();
});

for (const theme of ["light", "dark"] as const) {
  test(`axe on Groups, a group and Grants, ${theme}`, async ({ browser, browserName, request }) => {
    test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
    const environment = uniqueName("axe");
    await createEnvironment(request, environment);
    const { page, name } = await invitedPerson(browser, request, (person) =>
      makeAdministrator(request, person),
    );
    await skipTheWelcome(page);
    await drawIn(page, theme);

    await page.goto("/settings/access/groups");
    await page.getByRole("button", { name: "Create group" }).click();
    const group = uniqueName("axe");
    await page.getByRole("dialog").getByRole("textbox", { name: "Name" }).fill(group);
    await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();
    await expect(page.getByRole("link", { name: group })).toBeVisible();
    expect(await violations(page), "Groups").toEqual([]);

    await page.goto(`/settings/access/groups/${group}`);
    await expect(page.getByRole("heading", { level: 1, name: group })).toBeVisible();
    await expect(page.getByText(`${group} has no member yet.`)).toBeVisible();
    expect(await violations(page), "a group").toEqual([]);

    await page.goto(`/settings/access/grants?principal=${name}`);
    await expect(page.getByRole("region", { name: "Grants" })).toBeVisible();
    expect(await violations(page), "Grants").toEqual([]);
    await page.context().close();
  });
}

test("an administrator creates a service account and deactivates a person, whose session ends", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { page } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);
  // Someone with no rights, signed in in a browser of their own.
  const leaver = await invitedPerson(browser, request, () => Promise.resolve());
  const account = uniqueName("svc");

  await page.goto("/settings/access/people");
  await page.getByRole("button", { name: "New service account" }).click();
  const create = page.getByRole("dialog", { name: "New service account" });
  await create.getByRole("textbox", { name: "Name" }).fill(account);
  // The passkey touch is asked for first, answered by the virtual authenticator.
  await create.getByRole("button", { name: "Create service account" }).click();
  await expect(create).toBeHidden();
  await expect(page.getByText(`Created the service account ${account}.`)).toBeVisible();
  await page.getByRole("searchbox", { name: "Find people" }).fill(account);
  const row = page.getByRole("row").filter({ hasText: account });
  await expect(row).toContainText("Service account");
  await expect(row).toContainText("Active");

  // The same name again: the server's sentence stays in the dialog.
  await page.getByRole("button", { name: "New service account" }).click();
  await create.getByRole("textbox", { name: "Name" }).fill(account.toUpperCase());
  await create.getByRole("button", { name: "Create service account" }).click();
  await expect(create.getByRole("alert")).toContainText("differs only in letter case");
  await create.getByRole("button", { name: "Cancel" }).click();

  await page.goto(`/settings/access/people/${leaver.name}`);
  await page.getByRole("button", { name: `Deactivate ${leaver.name}` }).click();
  const deactivate = page.getByRole("dialog", { name: `Deactivate ${leaver.name}?` });
  await deactivate
    .getByRole("textbox", { name: `Type ${leaver.name} to confirm` })
    .fill(leaver.name);
  await deactivate.getByRole("button", { name: "Deactivate" }).click();
  await expect(deactivate).toBeHidden();
  await expect(page.getByText(`${leaver.name} is deactivated`)).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: leaver.name })).toBeFocused();
  expect(await violations(page), "a deactivated person").toEqual([]);

  await page.goto("/settings/access/people");
  await page.getByRole("searchbox", { name: "Find people" }).fill(leaver.name);
  await expect(page.getByRole("row").filter({ hasText: leaver.name })).toContainText("Deactivated");

  // The person's session ended with the deactivation.
  await leaver.page.reload();
  await expect(leaver.page.getByText(/Your account is deactivated/)).toBeVisible();
  // The browser logs the one refusal, of the taken name; nothing else.
  expect(errors.filter((error) => !error.includes("status of 409"))).toEqual([]);
  expect(errors.filter((error) => error.includes("status of 409"))).toHaveLength(1);
  await leaver.page.context().close();
  await page.context().close();
});

test("an administrator who deactivates their own account lands on the sign-in page", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { page, name } = await invitedPerson(browser, request, (person) =>
    makeAdministrator(request, person),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  await page.goto(`/settings/access/people/${name}`);
  await page.getByRole("button", { name: `Deactivate ${name}` }).click();
  const deactivate = page.getByRole("dialog", { name: "Deactivate your own account?" });
  await expect(deactivate).toContainText("Every session of yours ends at once");
  await deactivate.getByRole("textbox", { name: `Type ${name} to confirm` }).fill(name);
  // The passkey touch is asked for first, answered by the virtual authenticator.
  await deactivate.getByRole("button", { name: "Deactivate" }).click();

  // The session ended with the account: the sign-in page says why.
  await expect(page.getByText(/Your account is deactivated/)).toBeVisible();
  await expect(page).toHaveURL(/\/sign-in/);
  expect(await violations(page), "signed out after deactivating oneself").toEqual([]);
  // The session read after the deactivation answers 401, which the browser logs.
  expect(errors.filter((error) => !error.includes("status of 401"))).toEqual([]);
  await page.context().close();
});
