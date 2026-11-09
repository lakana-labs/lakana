import AxeBuilder from "@axe-core/playwright";
import { type Browser, expect, type Page, test } from "@playwright/test";

import { addAuthenticator, inviteLink, localLink, setupLink, uniqueName } from "./helpers";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const ORIGIN = "http://localhost:4174";
const SECRET_PARAM = /[#&?]s=/;
const CSRF_KEY = "lakana.csrf";

/**
 * Reads the secret (`s=`) of a link, from after its `#`.
 *
 * @param link - The link.
 * @returns The secret.
 */
function secretOf(link: string): string {
  const secret = new URLSearchParams(new URL(link).hash.slice(1)).get("s");
  expect(secret).toBeTruthy();
  return secret ?? "";
}

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
    const before = await page.evaluate((key) => localStorage.getItem(key), CSRF_KEY);
    expect(before).toBeTruthy();
    await page.getByRole("button", { name: "Signed in as admin" }).click();
    await page.getByRole("menuitem", { name: "Sign out everywhere" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Sign in to Lakana" })).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), CSRF_KEY)).toBeNull();

    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    const after = await page.evaluate((key) => localStorage.getItem(key), CSRF_KEY);
    expect(after).toBeTruthy();
    expect(after).not.toBe(before);
  });
});

test("the link secret is gone from the address bar and history", async ({ browser, request }) => {
  const page = await freshPage(browser);
  const link = await inviteLink(request, uniqueName("secret"));
  expect(link).toMatch(SECRET_PARAM);
  const secret = secretOf(link);

  await page.goto(link);
  await expect(page.getByRole("button", { name: "Continue" })).toBeVisible();
  expect(page.url()).not.toContain(secret);
  expect(page.url()).not.toContain("#");

  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  expect(page.url()).not.toContain(secret);
  const kept = await page.evaluate(() => ({
    state: JSON.stringify(history.state),
    location: location.href,
  }));
  expect(kept.state).not.toContain(secret);
  expect(kept.location).not.toContain(secret);
  await page.context().close();
});

/**
 * Walks the tab's session history from its first entry to its last, through Back and Forward.
 *
 * @param page - The page.
 * @returns Every address the tab showed on the way.
 */
async function walkHistory(page: Page): Promise<string[]> {
  const length = await page.evaluate(() => history.length);
  const seen = [page.url()];
  for (let step = 1; step < length; step += 1) {
    await page.goBack();
    seen.push(page.url());
  }
  for (let step = 1; step < length; step += 1) {
    await page.goForward();
    seen.push(page.url());
  }
  return seen;
}

test("no Back or Forward step shows the local link's secret", async ({ browser, request }) => {
  // The automatic sign-in of `lakana server start --local`: link, redeem, passkey offer, Home.
  const page = await freshPage(browser);
  await page.goto(`${ORIGIN}/healthz`);
  const link = await localLink(request);
  const secret = secretOf(link);
  const before = await page.evaluate(() => history.length);

  await page.goto(link);
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  await page.getByRole("button", { name: "Not now" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

  // The link's own entry is replaced, never followed by another one.
  expect(await page.evaluate(() => history.length)).toBe(before + 1);
  const seen = await walkHistory(page);
  expect(seen).toContain(`${ORIGIN}/healthz`);
  for (const address of seen) {
    expect(address).not.toContain("#");
    expect(address).not.toContain(secret);
  }
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

test("a cross-origin POST is refused, the same POST from the origin is not", async ({
  request,
}) => {
  const refused = await request.post("/api/v1/sign-in/links/describe", {
    headers: { Origin: "http://evil.example" },
    data: { link_id: "anything" },
  });
  expect(refused.status()).toBe(403);
  const control = await request.post("/api/v1/sign-in/links/describe", {
    headers: { Origin: ORIGIN },
    data: { link_id: "anything" },
  });
  // An unknown link: the request passed the Origin check and reached the route.
  expect(control.status()).toBe(410);
  expect(((await control.json()) as { state?: string }).state).toBe("unknown");
});

test("the session cookie alone signs nobody in; a new tab of the origin stays signed in", async ({
  browser,
  request,
}) => {
  const page = await freshPage(browser);
  await page.goto(await inviteLink(request, uniqueName("cookie-alone")));
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  const [cookie] = await page.context().cookies();
  const token = await page.evaluate((key) => localStorage.getItem(key), CSRF_KEY);
  expect(cookie?.name).toBe("lakana_session_4174");
  expect(token).toBeTruthy();

  // What another program on another localhost port has: the cookie, not the origin's storage.
  const jar = `${cookie?.name ?? ""}=${cookie?.value ?? ""}`;
  for (const [method, path] of [
    ["GET", "/api/v1/sign-in/session"],
    ["GET", "/api/v1/whoami"],
    ["DELETE", "/api/v1/sign-in/session"],
  ] as const) {
    const alone = await request.fetch(path, { method, headers: { Cookie: jar, Origin: ORIGIN } });
    expect(alone.status(), `${method} ${path}`).toBe(401);
    expect(((await alone.json()) as { type?: string }).type).toBe("CsrfRefused");
  }
  const withToken = await request.get("/api/v1/sign-in/session", {
    headers: { Cookie: jar, Origin: ORIGIN, "X-Lakana-CSRF": token ?? "" },
  });
  expect(withToken.status()).toBe(200);
  expect(await withToken.text()).not.toContain(token ?? "");

  const tab = await page.context().newPage();
  await tab.goto("/");
  await expect(tab.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  await page.context().close();
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
