import AxeBuilder from "@axe-core/playwright";
import { type Browser, expect, type Locator, type Page, test } from "@playwright/test";

import {
  addAuthenticator,
  interfaceState,
  inviteLink,
  makeAdministrator,
  signInLocally,
  uniqueName,
} from "./helpers";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const ORIGIN = "http://localhost:4174";
const NOT_CHROMIUM = "the virtual authenticator is CDP";

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

// React Aria's live announcer (a pending button speaks through it) keeps its message for 7 s and
// names it by the ids of the button's parts, which may be gone by then; it is the library's
// transient node, not the page.
const LIVE_ANNOUNCER = "[data-live-announcer]";

/**
 * Runs axe over the page with the WCAG 2.2 AA tags.
 *
 * @param page - The page to check.
 * @returns The violations, each with the elements it found.
 */
async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).exclude(LIVE_ANNOUNCER).analyze();
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
}

/**
 * Moves the focus with Tab until it reaches the button, then presses it with Enter.
 *
 * @param page - The page.
 * @param button - The button.
 */
async function pressByKeyboard(page: Page, button: Locator): Promise<void> {
  for (let step = 0; step < 20; step += 1) {
    if (await button.evaluate((node) => node === document.activeElement)) {
      await page.keyboard.press("Enter");
      return;
    }
    await page.keyboard.press("Tab");
  }
  throw new Error("Tab never reached the button");
}

/**
 * Waits for the next answer of `/interface-state` to the method.
 *
 * @param page - The page.
 * @param method - `GET` or `PATCH`.
 * @returns The answer's body.
 */
async function nextState(page: Page, method: string): Promise<unknown> {
  const response = await page.waitForResponse(
    (answer) => answer.url().endsWith("/interface-state") && answer.request().method() === method,
  );
  return response.json();
}

/**
 * Signs a newly invited administrator in with a passkey: the first sign-in of a person, with
 * the tour not seen yet (Chromium only).
 *
 * @param browser - The browser.
 * @param request - The request context.
 * @returns A page on Home, with the welcome open.
 */
async function firstAdministrator(
  browser: Browser,
  request: Parameters<typeof inviteLink>[0],
): Promise<Page> {
  const name = uniqueName("ada");
  const page = await freshPage(browser);
  await addAuthenticator(page.context(), page);
  await page.goto(await inviteLink(request, name));
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add a passkey" })).toBeVisible();
  // The person exists once the invite is redeemed; the role comes before Home reads it.
  await makeAdministrator(request, name);
  await page.getByRole("button", { name: "Add a passkey" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  return page;
}

test("first sign-in: welcome, the whole tour by keyboard, Finish, and it does not come back", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", NOT_CHROMIUM);
  const page = await firstAdministrator(browser, request);
  const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
  await expect(welcome).toBeVisible();
  await pressByKeyboard(page, welcome.getByRole("button", { name: "Start the tour" }));

  const first = page.getByRole("dialog", { name: "Home", exact: true });
  await expect(first).toBeVisible();
  const progress = (await first.getByText(/^Step 1 of \d+$/).textContent()) ?? "";
  const total = Number(/of (\d+)$/.exec(progress)?.[1]);
  expect(total).toBeGreaterThan(2);
  for (let current = 1; current < total; current += 1) {
    const step = page.getByRole("dialog").filter({ hasText: `Step ${String(current)} of` });
    await pressByKeyboard(page, step.getByRole("button", { name: "Next" }));
  }
  const last = page.getByRole("dialog", { name: "Help", exact: true });
  await expect(last.getByText(`Step ${String(total)} of ${String(total)}`)).toBeVisible();
  // The way on is the first open step of the person's own Get started: the passkey is added.
  await expect(last.getByRole("link", { name: "Open the tutorial" })).toBeVisible();

  const recorded = nextState(page, "PATCH");
  await pressByKeyboard(page, last.getByRole("button", { name: "Finish" }));
  expect(await recorded).toMatchObject({ tour_seen: true });
  await expect(page.getByRole("dialog")).toHaveCount(0);

  const read = nextState(page, "GET");
  await page.reload();
  expect(await read).toMatchObject({ tour_seen: true });
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Status" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.context().close();
});

test("Skip on the welcome records the tour as seen", async ({ browser, request }) => {
  await interfaceState(request, { tour_seen: false });
  const page = await freshPage(browser);
  await signInLocally(page, request);
  const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
  await expect(welcome).toBeVisible();

  const recorded = nextState(page, "PATCH");
  await welcome.getByRole("button", { name: "Skip" }).click();
  await recorded;
  await expect(welcome).toBeHidden();
  expect(await interfaceState(request)).toMatchObject({ tour_seen: true });

  await page.reload();
  await expect(page.getByRole("heading", { level: 2, name: "Status" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.context().close();
});

test("Get started ticks 'Add a passkey' after the passkey step", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", NOT_CHROMIUM);
  const page = await firstAdministrator(browser, request);
  await page
    .getByRole("dialog", { name: "Welcome to Lakana" })
    .getByRole("button", { name: "Skip" })
    .click();

  const list = page.getByRole("region", { name: "Get started" });
  const passkey = list.getByRole("listitem").filter({ hasText: "Add a passkey, so you sign in" });
  await expect(passkey).toContainText("Done");
  await expect(passkey.getByRole("link")).toHaveCount(0);
  // The local list's other steps are still open, each with its way.
  const deploy = list.getByRole("listitem").filter({ hasText: "Deploy your first project" });
  await expect(deploy).not.toContainText("Done");
  await expect(deploy.getByRole("link", { name: "Open the tutorial" })).toBeVisible();
  await page.context().close();
});

test("hidden Get started stays hidden after a reload and comes back from Help", async ({
  browser,
  request,
}) => {
  await interfaceState(request, { tour_seen: true, get_started_hidden: false });
  const page = await freshPage(browser);
  await signInLocally(page, request);
  const list = page.getByRole("region", { name: "Get started" });
  await expect(list).toBeVisible();

  const hidden = nextState(page, "PATCH");
  await list.getByRole("button", { name: "Hide" }).click();
  expect(await hidden).toMatchObject({ get_started_hidden: true });
  await expect(list).toHaveCount(0);

  await page.reload();
  await expect(page.getByRole("heading", { level: 2, name: "Status" })).toBeVisible();
  await expect(list).toHaveCount(0);

  await page.getByRole("button", { name: "Help" }).click();
  const shown = nextState(page, "PATCH");
  await page.getByRole("menuitem", { name: "Show “Get started” again" }).click();
  expect(await shown).toMatchObject({ get_started_hidden: false });
  await expect(list).toBeVisible();
  expect(await interfaceState(request)).toMatchObject({ get_started_hidden: false });
  await page.context().close();
});

test("no update note on a development build, even after a version was seen", async ({
  browser,
  request,
}) => {
  // A release would show "Lakana was updated to …" now; the e2e server runs a development
  // build, which has no release notes to point to (the shown and hidden note: Vitest).
  await interfaceState(request, { tour_seen: true, update_note_seen: "0.0.1" });
  const page = await freshPage(browser);
  const read = nextState(page, "GET");
  await signInLocally(page, request);
  expect(await read).toMatchObject({ update_note_seen: "0.0.1" });
  await expect(page.getByRole("heading", { level: 2, name: "Status" })).toBeVisible();

  await page.getByRole("button", { name: "Help" }).click();
  const version = page.getByText(/^Lakana \S+$/);
  await expect(version).toBeVisible();
  expect(await version.textContent()).not.toMatch(/^Lakana \d+\.\d+\.\d+$/);
  await page.keyboard.press("Escape");
  await expect(page.getByText(/Lakana was updated to/)).toHaveCount(0);
  await expect(page.getByRole("link", { name: /what's new/ })).toHaveCount(0);
  await page.context().close();
});

test("the status box says Down without a worker, in words", async ({ browser, request }) => {
  await interfaceState(request, { tour_seen: true });
  const page = await freshPage(browser);
  const status = page.waitForResponse(
    (answer) => new URL(answer.url()).pathname === "/api/v1/status",
  );
  await signInLocally(page, request);
  expect(await (await status).json()).toMatchObject({ state: "down" });

  const box = page.getByRole("region", { name: "Status" });
  await expect(box.getByText("Down", { exact: true })).toBeVisible();
  await expect(
    box.getByText("No worker is running, so queries and jobs wait until one starts."),
  ).toBeVisible();
  // The administrator's figures, in words too.
  await expect(box.getByText("No worker is running.")).toBeVisible();
  await expect(box.getByRole("link", { name: /Details/ })).toHaveAttribute(
    "href",
    "/settings/status",
  );
  await page.context().close();
});

test("Connect a repository without a worker names the cause and the next step", async ({
  browser,
  request,
}) => {
  // The e2e server runs no worker, so the host-key check is refused (503) before it is queued.
  await interfaceState(request, { tour_seen: true });
  const page = await freshPage(browser);
  await signInLocally(page, request);
  await page.goto("/settings/repositories/new");
  await expect(page.getByRole("heading", { level: 1, name: "Connect a repository" })).toBeVisible();
  await page
    .getByRole("textbox", { name: "Repository URL" })
    .fill("git@github.com:acme/analytics.git");
  await page.getByRole("textbox", { name: "Repository URL" }).blur();

  const refused = page.waitForResponse(
    (answer) =>
      new URL(answer.url()).pathname === "/api/v1/repository-probes" &&
      answer.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Fetch the host's keys" }).click();
  const answer = await refused;
  expect(answer.status()).toBe(503);
  expect(answer.headers()["retry-after"]).toBeTruthy();
  await expect(page.getByRole("alert")).toContainText(
    "No worker is running, so nothing can check the repository.",
  );
  await expect(page.getByRole("alert")).toContainText("lakana server start --local");
  await expect(page.getByRole("button", { name: "Fetch the host's keys" })).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Create the repository and its keys" }),
  ).toHaveCount(0);
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test(`axe finds nothing on Home, Help, a tour step and the welcome (${scheme}, ${String(width)} px)`, async ({
      browser,
      browserName,
      request,
    }) => {
      test.skip(browserName !== "chromium", "axe runs in Chromium only");
      await interfaceState(request, { tour_seen: false, get_started_hidden: false });
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);

      const welcome = page.getByRole("dialog", { name: "Welcome to Lakana" });
      await expect(welcome).toBeVisible();
      expect(await violations(page), "the welcome").toEqual([]);
      await welcome.getByRole("button", { name: "Start the tour" }).click();
      const step = page.getByRole("dialog", {
        name: width === 320 ? "In the menu: Home" : "Home",
        exact: true,
      });
      await expect(step).toBeVisible();
      expect(await violations(page), "a tour step").toEqual([]);
      await step.getByRole("button", { name: "Skip tour" }).click();
      await expect(page.getByRole("dialog")).toHaveCount(0);

      await expect(page.getByRole("region", { name: "Get started" })).toBeVisible();
      await expect(page.getByText("Down", { exact: true })).toBeVisible();
      expect(await violations(page), "Home").toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        ),
      ).toBeLessThanOrEqual(0);

      if (width === 320) {
        await page.getByRole("button", { name: "Menu" }).click();
      }
      await page.getByRole("button", { name: "Help" }).click();
      await expect(page.getByRole("menu", { name: "Help" })).toBeVisible();
      expect(await violations(page), "Help").toEqual([]);
      await page.context().close();
    });
  }
}
