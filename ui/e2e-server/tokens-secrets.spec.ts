import { expect, type Page, test } from "@playwright/test";

import {
  createEnvironment,
  grantOn,
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
 * Creates a token on the account page and reads it from the dialog that shows it once.
 *
 * @param page - The person's page.
 * @param label - The token's name.
 * @returns The token.
 */
async function createToken(page: Page, label: string): Promise<string> {
  await page.goto("/account");
  await page.getByRole("button", { name: "Create a token" }).click();
  const dialog = page.getByRole("dialog", { name: "Create a token" });
  await dialog.getByRole("textbox", { name: "Name" }).fill(label);
  // The passkey touch is asked for first, answered by the virtual authenticator.
  await dialog.getByRole("button", { name: "Create" }).click();
  const shown = page.getByRole("dialog", { name: `Your new token ${label}` });
  await expect(shown).toBeVisible();
  const token = (await shown.locator("code").first().textContent()) ?? "";
  expect(token).not.toBe("");
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toBeHidden();
  return token;
}

test("a person makes their own token and it works", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("tok");
  await createEnvironment(request, environment);
  const { page, name } = await invitedPerson(browser, request, (person) =>
    grantOn(request, person, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  const token = await createToken(page, "laptop");
  const asThem = { headers: { Authorization: `Bearer ${token}` } };
  const whoami = await request.get("/api/v1/whoami", asThem);
  expect(whoami.status()).toBe(200);
  expect(((await whoami.json()) as { name: string }).name).toBe(name);
  await expect(page.getByRole("heading", { level: 2, name: "API tokens" })).toBeFocused();
  expect(await page.content()).not.toContain(token);

  await page.getByRole("button", { name: "Revoke laptop" }).click();
  await page
    .getByRole("dialog", { name: "Revoke this token?" })
    .getByRole("button", { name: "Revoke the token" })
    .click();
  await expect(page.getByText(/^revoked /)).toBeVisible();
  expect((await request.get("/api/v1/whoami", asThem)).status()).toBe(401);
  expect(errors).toEqual([]);
  await page.context().close();
});

test("an administrator sees and revokes it", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const person = await invitedPerson(browser, request, () => Promise.resolve());
  const token = await createToken(person.page, "nightly-export");
  await person.page.context().close();
  const { page } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  await page.goto(`/settings/access/people/${person.name}`);
  await page.getByRole("link", { name: `Tokens of ${person.name}` }).click();
  await expect(page).toHaveURL(new RegExp(`/settings/access/tokens\\?owner=${person.name}$`));
  const list = page.getByRole("region", { name: "Live personal tokens" });
  await expect(list.getByText("nightly-export")).toBeVisible();
  await page.getByRole("button", { name: `Revoke nightly-export of ${person.name}` }).click();
  await page
    .getByRole("dialog", { name: "Revoke this token?" })
    .getByRole("button", { name: "Revoke the token" })
    .click();
  await expect(page.getByText(`${person.name} has no live token.`)).toBeVisible();
  const asThem = { headers: { Authorization: `Bearer ${token}` } };
  expect((await request.get("/api/v1/whoami", asThem)).status()).toBe(401);
  expect(errors).toEqual([]);
  await page.context().close();
});

test("secrets for a person with MANAGE_SECRET in one environment", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("e");
  const other = uniqueName("e");
  await createEnvironment(request, environment);
  await createEnvironment(request, other);
  const { page } = await invitedPerson(browser, request, async (person) => {
    await grantOn(request, person, "MANAGE_SECRET", environment);
    await grantOn(request, person, "READ_ENVIRONMENT", other);
  });
  await skipTheWelcome(page);
  const errors = consoleErrors(page);
  const asked: string[] = [];
  page.on("request", (made) => asked.push(new URL(made.url()).pathname));

  await page.goto("/settings/access");
  await expect(page).toHaveURL(/\/settings\/access\/secrets$/);
  await expect(page.getByText(`No secret in ${environment} yet.`)).toBeVisible();
  await page.getByRole("button", { name: /^Environment: / }).click();
  await expect(page.getByRole("option")).toHaveText([environment]);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Add secret" }).click();
  const add = page.getByRole("dialog", { name: `Add a secret to ${environment}` });
  await add.getByRole("textbox", { name: "Name" }).fill("warehouse");
  await add.getByRole("textbox", { name: "Key" }).fill("password");
  await add.getByLabel("Value").fill("s3cr3t-e2e-server");
  await add.getByRole("button", { name: "Add secret" }).click();
  await expect(add).toBeHidden();
  const region = page.getByRole("region", { name: `Secrets of ${environment}` });
  const row = region.getByRole("row").filter({ hasText: "warehouse" });
  await expect(row).toContainText("no use yet");
  await expect(row).toContainText("—");

  await page.getByRole("button", { name: "Rotate warehouse" }).click();
  const rotate = page.getByRole("dialog", { name: "Rotate warehouse" });
  await rotate.getByRole("textbox", { name: "Key" }).fill("password");
  await rotate.getByLabel("Value").fill("s3cr3t-e2e-server-2");
  await rotate.getByRole("button", { name: "Rotate" }).click();
  await expect(rotate).toBeHidden();
  await expect(row).not.toContainText("—");

  await page.getByRole("button", { name: "Delete warehouse" }).click();
  const remove = page.getByRole("dialog", { name: "Delete warehouse?" });
  await remove.getByRole("textbox").fill("warehouse");
  await remove.getByRole("button", { name: "Delete the secret" }).click();
  await expect(page.getByText(`No secret in ${environment} yet.`)).toBeVisible();
  expect(asked.filter((path) => path.includes(`/environments/${other}/secrets`))).toEqual([]);
  expect(await page.content()).not.toContain("s3cr3t-e2e-server");
  expect(errors).toEqual([]);
  await page.context().close();
});

for (const theme of ["light", "dark"] as const) {
  test(`axe on Tokens and Secrets, ${theme}`, async ({ browser, browserName, request }) => {
    test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
    const environment = uniqueName("axe");
    await createEnvironment(request, environment);
    const { page } = await invitedPerson(browser, request, (person) =>
      makeAdministrator(request, person),
    );
    await skipTheWelcome(page);
    await page.emulateMedia({ colorScheme: theme });

    await page.goto("/settings/access/tokens");
    await expect(page.getByRole("heading", { level: 2, name: "Task tokens" })).toBeVisible();
    await expect(page.getByText("Reading the task tokens…")).toBeHidden();
    expect(await violations(page), "Tokens").toEqual([]);

    await page.goto(`/settings/access/secrets?environment=${environment}`);
    await expect(page.getByText(`No secret in ${environment} yet.`)).toBeVisible();
    expect(await violations(page), "Secrets").toEqual([]);
    await page.context().close();
  });
}
