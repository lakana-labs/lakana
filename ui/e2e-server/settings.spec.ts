import { expect, type Page, test } from "@playwright/test";

import { administrator, invitedPerson, violations } from "./helpers";

const PLACES = "Settings places";

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

test("an administrator's places load from the server, and the old addresses land on them", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const page = await administrator(browser, request);
  const errors = consoleErrors(page);

  await page.goto("/access");
  await expect(page).toHaveURL(/\/settings\/access\/people$/);
  await expect(page.getByRole("heading", { level: 1, name: "People" })).toBeVisible();
  await expect(page.getByRole("table").getByText("admin", { exact: true })).toBeVisible();

  await page.goto("/access/invites");
  await expect(page).toHaveURL(/\/settings\/sign-in\/invites$/);
  await expect(page.getByRole("heading", { level: 1, name: "Invites" })).toBeVisible();

  await page.goto("/access/recovery-links");
  await expect(page).toHaveURL(/\/settings\/sign-in\/recovery-links$/);
  await expect(page.getByRole("heading", { level: 1, name: "Recovery links" })).toBeVisible();

  await page.goto("/access/people/admin");
  await expect(page).toHaveURL(/\/settings\/access\/people\/admin$/);
  await expect(page.getByRole("heading", { level: 1, name: "admin" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toHaveText(
    /Settings\s*›\s*Access\s*›\s*People\s*›\s*admin/,
  );

  await page.goto("/settings/sign-in");
  await expect(page.getByRole("heading", { level: 1, name: "Sign-in" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Idle timeout in minutes" })).toHaveValue(/^\d+$/);
  await expect(page.getByRole("textbox", { name: "Longest session in minutes" })).toHaveValue(
    /^\d+$/,
  );

  await page.goto("/settings/repositories");
  await expect(page.getByRole("heading", { level: 1, name: "Repositories" })).toBeVisible();
  await page.getByRole("link", { name: "Connect a repository" }).click();
  await expect(page).toHaveURL(/\/settings\/repositories\/new$/);
  await expect(page.getByRole("heading", { level: 1, name: "Connect a repository" })).toBeVisible();

  expect(errors).toEqual([]);
  await page.context().close();
});

test("a person without admin rights sees Settings with Status only", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { page } = await invitedPerson(browser, request, async () => {});
  const asked: string[] = [];
  page.on("request", (made) => asked.push(new URL(made.url()).pathname));

  await expect(page.getByRole("link", { name: "Settings" })).toBeVisible();

  await page.goto("/settings");
  const list = page.getByRole("navigation", { name: PLACES });
  await expect(list.getByRole("link")).toHaveText(["Status"]);

  await page.goto("/access/invites");
  await expect(page).toHaveURL(/\/settings\/sign-in\/invites$/);
  await expect(page.getByText("You cannot use Invites")).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(asked.filter((path) => path.startsWith("/api/v1/invites"))).toEqual([]);

  await page.goto("/settings/access/people/admin");
  await expect(page.getByText("You cannot use People")).toBeVisible();
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  test(`axe on Settings (${scheme})`, async ({ browser, browserName, request }) => {
    test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
    const page = await administrator(browser, request);
    await page.emulateMedia({ colorScheme: scheme });

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/settings/access/people");
    await expect(page.getByRole("heading", { level: 1, name: "People" })).toBeVisible();
    await expect(page.getByRole("table").getByText("admin", { exact: true })).toBeVisible();
    expect(await violations(page), "1280 px").toEqual([]);

    await page.setViewportSize({ width: 320, height: 800 });
    await page.getByRole("button", { name: "Section" }).click();
    await expect(page.getByRole("dialog", { name: PLACES })).toBeVisible();
    expect(await violations(page), "320 px, the picker open").toEqual([]);
    await page.context().close();
  });
}
