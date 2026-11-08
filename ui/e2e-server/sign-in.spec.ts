import AxeBuilder from "@axe-core/playwright";
import { type Browser, expect, type Page, test } from "@playwright/test";

import { addAuthenticator, inviteLink, setupLink, uniqueName } from "./helpers";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const ORIGIN = "http://localhost:4174";
const SECRET_PARAM = /[#&?]s=/;

/**
 * Runs axe over the page with the WCAG 2.2 AA tags.
 *
 * @param page - The page to check.
 * @returns The violations' ids.
 */
async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  return results.violations.map((violation) => violation.id);
}

/**
 * Opens a browser that has never visited the server.
 *
 * @param browser - The browser.
 * @returns A page of a fresh context.
 */
async function freshPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({ baseURL: ORIGIN });
  return context.newPage();
}

test.describe("the first administrator", () => {
  // Skipped inside each hook and test: a serial group runs its next test after a skipped one.
  const NOT_CHROMIUM = "the virtual authenticator is CDP";
  test.describe.configure({ mode: "serial" });

  let page: Page;

  test.beforeAll(async ({ browser, browserName }) => {
    if (browserName !== "chromium") {
      return;
    }
    page = await freshPage(browser);
    await addAuthenticator(page.context(), page);
  });

  test.afterAll(async ({ browserName }) => {
    if (browserName === "chromium") {
      await page.context().close();
    }
  });

  test("setup link: confirm, add a passkey, land on Home signed in", async ({
    request,
    browserName,
  }) => {
    test.skip(browserName !== "chromium", NOT_CHROMIUM);
    await page.goto(await setupLink(request));

    await expect(page.getByRole("heading", { level: 1, name: "Sign in as admin" })).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
    await page.getByRole("button", { name: "Add a passkey" }).click();

    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Access" })).toBeVisible();
  });

  test("passkey sign-in after sign out", async ({ browserName }) => {
    test.skip(browserName !== "chromium", NOT_CHROMIUM);
    await page.getByRole("button", { name: "Signed in as admin" }).click();
    await page.getByRole("menuitem", { name: "Sign out everywhere" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Sign in to Lakana" })).toBeVisible();

    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  });
});

test("the link secret is gone from the address bar and history", async ({ browser, request }) => {
  const page = await freshPage(browser);
  const link = await inviteLink(request, uniqueName("secret"));
  expect(link).toMatch(SECRET_PARAM);

  await page.goto(link);
  await expect(page.getByRole("button", { name: "Continue" })).toBeVisible();
  expect(page.url()).not.toMatch(SECRET_PARAM);
  expect(page.url()).not.toContain("#");

  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  expect(page.url()).not.toMatch(SECRET_PARAM);
  const state = await page.evaluate(() => JSON.stringify(history.state));
  expect(state).not.toContain("secret");
  await page.context().close();
});

test("a used link names the next step", async ({ browser, request }) => {
  const link = await inviteLink(request, uniqueName("used"));
  const first = await freshPage(browser);
  await first.goto(link);
  await first.getByRole("button", { name: "Continue" }).click();
  await expect(first.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  await first.context().close();

  const second = await freshPage(browser);
  await second.goto(link);
  await expect(
    second.getByRole("heading", { level: 1, name: "This link was already used" }),
  ).toBeVisible();
  await expect(
    second.getByText("Sign in with your passkey, or ask whoever sent the link for a new one."),
  ).toBeVisible();
  await second.context().close();
});

test("cookie is HttpOnly, SameSite=Strict, per port on http://localhost", async ({
  browser,
  request,
}) => {
  const page = await freshPage(browser);
  await page.goto(await inviteLink(request, uniqueName("cookie")));
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();

  const cookies = await page.context().cookies();
  expect(cookies.map((cookie) => cookie.name)).toEqual(["lakana_session_4174"]);
  expect(cookies[0]).toMatchObject({
    httpOnly: true,
    sameSite: "Strict",
    secure: false,
    domain: "localhost",
    path: "/",
  });
  await page.context().close();
});

test("127.0.0.1 redirects to localhost", async ({ browser }) => {
  const page = await freshPage(browser);
  await page.goto("http://127.0.0.1:4174/sign-in");
  await expect(page).toHaveURL(`${ORIGIN}/sign-in`);
  await expect(page.getByRole("heading", { level: 1, name: "Sign in to Lakana" })).toBeVisible();
  await page.context().close();
});

test("a cross-origin POST is refused", async ({ request }) => {
  const refused = await request.post("/api/v1/sign-in/links/describe", {
    headers: { Origin: "http://evil.example" },
    data: { link_id: "anything" },
  });
  expect(refused.status()).toBe(403);
});

test.describe("an invited person", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "the virtual authenticator is CDP");

  test("invite: a new person sees 'You have no access yet'", async ({ browser, request }) => {
    const name = uniqueName("kim");
    const page = await freshPage(browser);
    await addAuthenticator(page.context(), page);

    await page.goto(await inviteLink(request, name));
    await expect(
      page.getByRole("heading", { level: 1, name: `Join Lakana as ${name}` }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Add a passkey" }).click();

    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    await expect(page.getByText("You have no access yet; ask an administrator.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Access" })).toHaveCount(0);
    await page.context().close();
  });
});

for (const scheme of ["light", "dark"] as const) {
  test(`axe finds nothing on the sign-in, landing and passkey pages (${scheme})`, async ({
    browser,
    browserName,
    request,
  }) => {
    test.skip(browserName !== "chromium", "axe runs in Chromium only");
    const page = await freshPage(browser);
    await page.emulateMedia({ colorScheme: scheme });

    await page.goto("/sign-in");
    await expect(page.getByRole("heading", { level: 1, name: "Sign in to Lakana" })).toBeVisible();
    expect(await violations(page), "the sign-in page").toEqual([]);

    await page.context().close();

    // A new page: the link differs from /sign-in only after the #, which would not reload.
    const joining = await freshPage(browser);
    await joining.emulateMedia({ colorScheme: scheme });
    await joining.goto(await inviteLink(request, uniqueName("axe")));
    await expect(joining.getByRole("button", { name: "Continue" })).toBeVisible();
    expect(await violations(joining), "the link landing").toEqual([]);

    await joining.getByRole("button", { name: "Continue" }).click();
    await expect(joining.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
    expect(await violations(joining), "the passkey step").toEqual([]);
    await joining.context().close();
  });
}
