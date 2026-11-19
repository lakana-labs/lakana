import { expect, type Page, test } from "@playwright/test";

import {
  ageThePasskeyTouch,
  createEnvironment,
  createRepository,
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

test("Status against the real server", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const admin = await invitedPerson(browser, request, (name) => makeAdministrator(request, name));
  await skipTheWelcome(admin.page);
  const errors = consoleErrors(admin.page);

  // The version the page reads, from the server's own answer.
  const answered = admin.page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/info"),
  );
  await admin.page.goto("/settings/status");
  const info = (await (await answered).json()) as { version: string };
  await expect(admin.page.getByRole("heading", { level: 2, name: "Workers" })).toBeVisible();
  await expect(admin.page.getByRole("heading", { level: 2, name: "Scheduler" })).toBeVisible();
  await expect(admin.page.getByText(`This server runs Lakana ${info.version}.`)).toBeVisible();
  await axeInBothThemes(admin.page, "Status for an administrator");
  expect(errors).toEqual([]);
  await admin.page.context().close();

  const environment = uniqueName("st");
  await createEnvironment(request, environment);
  const reader = await invitedPerson(browser, request, (name) =>
    grantOn(request, name, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(reader.page);
  await reader.page.goto("/settings/status");
  await expect(reader.page.getByRole("heading", { level: 2, name: "Health" })).toBeVisible();
  await expect(reader.page.getByRole("heading", { level: 2, name: "Workers" })).toHaveCount(0);
  await reader.page.context().close();
});

test("a repository from the list to its page and away", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const repository = uniqueName("repo");
  await createRepository(request, {
    name: repository,
    kind: "https",
    url: `https://github.com/acme/${repository}.git`,
  });
  const { page, name } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);

  await page.goto("/settings/repositories");
  const list = page.getByRole("region", { name: "Repositories" });
  await expect(list.getByRole("link", { name: repository })).toBeVisible();
  await axeInBothThemes(page, "the list");
  await list.getByRole("link", { name: repository }).click();
  await expect(page.getByRole("heading", { level: 1, name: repository })).toBeVisible();
  await expect(page.getByText("No credential")).toBeVisible();
  await axeInBothThemes(page, "a repository's page");

  // An old touch: saving asks for the passkey again before it is sent.
  ageThePasskeyTouch(name);
  await page.getByRole("button", { name: `Edit repository ${repository}` }).click();
  const edit = page.getByRole("dialog");
  await edit.getByRole("textbox", { name: "Default ref" }).fill("release");
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toBeHidden();
  await expect(page.getByText("release", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: `Delete repository ${repository}` }).click();
  const remove = page.getByRole("dialog");
  await remove.getByRole("textbox", { name: `Type ${repository} to delete it` }).fill(repository);
  await remove.getByRole("button", { name: "Delete repository" }).click();
  await expect(page).toHaveURL(/\/settings\/repositories$/);
  await expect(page.getByRole("link", { name: repository })).toHaveCount(0);
  expect(errors).toEqual([]);
  await page.context().close();
});

test("a reader sees Repositories read-only", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const environment = uniqueName("rd");
  await createEnvironment(request, environment);
  const repository = uniqueName("repo");
  await createRepository(request, {
    name: repository,
    kind: "https",
    url: `https://gitlab.example.com/data/${repository}.git`,
  });
  const { page } = await invitedPerson(browser, request, (name) =>
    grantOn(request, name, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(page);

  await page.goto("/settings/repositories");
  await expect(page.getByRole("link", { name: repository })).toBeVisible();
  await expect(page.getByRole("link", { name: "Connect a repository" })).toHaveCount(0);
  await page.goto(`/settings/repositories/${repository}`);
  await expect(page.getByText(`https://gitlab.example.com/data/${repository}.git`)).toBeVisible();
  await expect(page.getByRole("button", { name: /^(Edit|Delete|Test)/ })).toHaveCount(0);
  await page.context().close();
});
