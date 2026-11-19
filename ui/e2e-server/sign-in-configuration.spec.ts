import { expect, type Page, test } from "@playwright/test";

import {
  administrator,
  ageThePasskeyTouch,
  invitedPerson,
  makeAdministrator,
  skipTheWelcome,
  violations,
} from "./helpers";

// The secrets the server runs with (playwright.server.config.ts), written here as literals: the
// page must show them as set, never their text.
const BOOTSTRAP_SECRET = "e2esecret";
const MASTER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

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

test("the real server's secrets are not on the page", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const page = await administrator(browser, request);
  const errors = consoleErrors(page);
  const answer = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v1/settings/configuration",
  );

  await page.goto("/settings/configuration");
  const body = await (await answer).text();
  const region = page.getByRole("region", { name: "Server settings" });
  for (const name of [
    "LAKANA_BOOTSTRAP_PAT_SECRET",
    "LAKANA_SECRETS__MASTER_KEY",
    "LAKANA_DATABASE__URL",
  ]) {
    await expect(region.getByRole("row", { name: new RegExp(`^${name} `) })).toContainText(
      "secret, set",
    );
  }
  const enabled = region.getByRole("row", { name: /^LAKANA_UI__ENABLED / });
  await expect(enabled.getByRole("cell").nth(1)).toHaveText("true");
  await expect(enabled.getByRole("cell").nth(2)).toHaveText("set");
  for (const secret of [BOOTSTRAP_SECRET, MASTER_KEY, "lakana.db"]) {
    expect(body, `the answer carries ${secret}`).not.toContain(secret);
    expect(await page.content(), `the page carries ${secret}`).not.toContain(secret);
  }
  await axeInBothThemes(page, "Configuration");

  await page.goto("/settings/sign-in");
  await expect(page.getByRole("region", { name: "Session lengths" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Idle timeout in minutes" })).toHaveValue(/^\d+$/);
  await axeInBothThemes(page, "Sign-in");
  expect(errors).toEqual([]);
  await page.context().close();
});

test("an aged touch is asked for before Save", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { page, name } = await invitedPerson(browser, request, (admin) =>
    makeAdministrator(request, admin),
  );
  await skipTheWelcome(page);
  const errors = consoleErrors(page);
  const sent: string[] = [];
  page.on("request", (made) => {
    const path = new URL(made.url()).pathname;
    if (
      made.method() !== "GET" &&
      (path.startsWith("/api/v1/sign-in/step-up") || path === "/api/v1/settings/sign-in")
    ) {
      sent.push(`${made.method()} ${path}`);
    }
  });

  await page.goto("/settings/sign-in");
  const idle = page.getByRole("textbox", { name: "Idle timeout in minutes" });
  await expect(idle).toHaveValue(/^\d+$/);
  const before = await idle.inputValue();
  const changed = before === "45" ? "46" : "45";
  ageThePasskeyTouch(name);
  await idle.fill(changed);
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page.getByText("Saved. New sessions use these lengths.")).toBeVisible();
  expect(sent).toEqual([
    "POST /api/v1/sign-in/step-up/options",
    "POST /api/v1/sign-in/step-up",
    "PATCH /api/v1/settings/sign-in",
  ]);
  await page.reload();
  await expect(idle).toHaveValue(changed);

  // Back to the lengths the other tests start with; the touch is fresh now, so none is asked.
  await idle.fill(before);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved. New sessions use these lengths.")).toBeVisible();
  expect(sent.slice(3)).toEqual(["PATCH /api/v1/settings/sign-in"]);
  expect(errors).toEqual([]);
  await page.context().close();
});

test("an invited person cannot use Configuration", async ({ browser, browserName, request }) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { page } = await invitedPerson(browser, request, async () => {});
  const asked: string[] = [];
  page.on("request", (made) => asked.push(new URL(made.url()).pathname));

  await page.goto("/settings/status");
  const places = page.getByRole("navigation", { name: "Settings places" });
  await expect(places.getByRole("link", { name: "Status" })).toBeVisible();
  await expect(places.getByRole("link", { name: "Sign-in" })).toHaveCount(0);
  await expect(places.getByRole("link", { name: "Configuration" })).toHaveCount(0);

  await page.goto("/settings/configuration");
  await expect(
    page.getByRole("heading", { level: 1, name: "You cannot use Configuration" }),
  ).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(asked.filter((path) => path.startsWith("/api/v1/settings/"))).toEqual([]);
  await page.context().close();
});
