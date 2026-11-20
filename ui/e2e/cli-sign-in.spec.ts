import { expect, type Page, test } from "@playwright/test";

import { ADMIN_SESSION, consoleErrors, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const PORT = 49152;
const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const KIM = {
  ...ADMIN_SESSION,
  name: "kim",
  step_up_until: new Date(Date.now() + 3_600_000).toISOString(),
};
const LONG_HOST = "build-runner-eu-west-3-prod-ingest-nightly-0042-a.corp.example.org";

/** The API paths these stubs answer; any other request to the API fails the test. */
const STUBBED = new Set([
  "/api/v1/sign-in/session",
  "/api/v1/sign-in/notices",
  "/api/v1/whoami",
  "/api/v1/sign-in/cli/approvals",
  "/api/v1/sign-in/cli/device/describe",
  "/api/v1/sign-in/cli/device/approve",
]);

/**
 * Answers the routes of a signed-in kim with a fresh touch, and records every API path asked.
 *
 * @param page - The page.
 * @returns The paths, filled as requests go out.
 */
async function stub(page: Page): Promise<string[]> {
  const paths: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (pathname.startsWith("/api/")) {
      paths.push(pathname);
    }
  });
  await page.route("**/api/v1/sign-in/session", (route) => route.fulfill({ json: KIM }));
  await page.route("**/api/v1/sign-in/notices", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/whoami", (route) =>
    route.fulfill({
      json: {
        name: "kim",
        type: "USER",
        task_token: null,
        ci_token: null,
        language: null,
        admin: false,
        has_access: true,
      },
    }),
  );
  await page.route("**/api/v1/sign-in/cli/approvals", (route) =>
    route.fulfill({ json: { code: "c0de", port: PORT } }),
  );
  await page.route("**/api/v1/sign-in/cli/device/describe", (route) =>
    route.fulfill({
      json: {
        host: "build-box",
        requested_from: "203.0.113.7",
        requested_at: new Date(Date.now() - 60_000).toISOString(),
      },
    }),
  );
  await page.route("**/api/v1/sign-in/cli/device/approve", (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.route(`http://127.0.0.1:${String(PORT)}/**`, (route) =>
    route.fulfill({ contentType: "text/html", body: "<p>You can close this tab.</p>" }),
  );
  return paths;
}

/**
 * The address `lakana login` opens.
 *
 * @param host - The computer's name.
 * @returns The path and query.
 */
function link(host = "laptop-7"): string {
  const query = new URLSearchParams({
    port: String(PORT),
    state: "st-1",
    challenge: CHALLENGE,
    host,
  });
  return `/sign-in/cli?${query.toString()}`;
}

/**
 * Says whether the focus is on the page's body, which a keyboard user cannot see.
 *
 * @param page - The page.
 * @returns True when nothing has the focus.
 */
async function focusOnBody(page: Page): Promise<boolean> {
  return page.evaluate(() => document.activeElement === document.body);
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await page.addInitScript(
          ([key, value]) => {
            window.localStorage.setItem(key ?? "", value ?? "");
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("the loopback confirmation by keyboard, to 127.0.0.1", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = await stub(page);
        await page.goto(link());

        const heading = page.getByRole("heading", {
          level: 1,
          name: "Create the API token “lakana login on laptop-7” for kim?",
        });
        await expect(heading).toBeFocused();
        expect(await violations(page)).toEqual([]);

        await tabTo(page, page.getByRole("button", { name: "Create token" }));
        expect(await focusOnBody(page)).toBe(false);
        const back = page.waitForRequest((request) =>
          request.url().startsWith(`http://127.0.0.1:${String(PORT)}/`),
        );
        await page.keyboard.press("Enter");
        const request = await back;

        expect(request.url()).toBe(`http://127.0.0.1:${String(PORT)}/?code=c0de&state=st-1`);
        expect(paths.filter((path) => !STUBBED.has(path))).toEqual([]);
        expect(errors).toEqual([]);
      });

      test("the device page by keyboard", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = await stub(page);
        await page.goto("/sign-in/device");

        await expect(
          page.getByRole("heading", { level: 1, name: "Sign in a command line elsewhere" }),
        ).toBeFocused();
        expect(await violations(page)).toEqual([]);
        await tabTo(page, page.getByRole("textbox", { name: "Code" }));
        await page.keyboard.type("BCDF-GHJK");
        await page.keyboard.press("Enter");
        await expect(
          page.getByRole("heading", {
            level: 1,
            name: "Create the API token “lakana login on build-box” for kim?",
          }),
        ).toBeFocused();
        expect(await violations(page), "the confirmation").toEqual([]);
        await tabTo(page, page.getByRole("button", { name: "Create token" }));
        await page.keyboard.press("Enter");
        await expect(page.getByRole("status")).toHaveText(
          "Done. Go back to the terminal: lakana login finishes there.",
        );
        expect(await focusOnBody(page)).toBe(false);
        expect(await violations(page), "done").toEqual([]);
        expect(paths.filter((path) => !STUBBED.has(path))).toEqual([]);
        expect(errors).toEqual([]);
      });

      test("the confirmation at 320 px with a 64-character host", async ({ page }) => {
        await stub(page);
        await page.setViewportSize({ width: 320, height: 640 });
        await page.goto(link(LONG_HOST.slice(0, 64)));

        const heading = page.getByRole("heading", { level: 1 });
        await expect(heading).toContainText(LONG_HOST.slice(0, 64));
        const scroll = await page.evaluate(() => ({
          page: document.documentElement.scrollWidth,
          view: document.documentElement.clientWidth,
        }));
        expect(scroll.page).toBeLessThanOrEqual(scroll.view);
        const box = await heading.boundingBox();
        expect(box === null ? 0 : box.x + box.width).toBeLessThanOrEqual(320);
        expect(await violations(page)).toEqual([]);
      });
    });
  }
}
