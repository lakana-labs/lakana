import { expect, type Page, test } from "@playwright/test";

import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const PLACES = "Settings places";

/**
 * Records the API paths a page asks for.
 *
 * @param page - The page.
 * @returns The list, filled as requests go out.
 */
function apiPaths(page: Page): string[] {
  const paths: string[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (pathname.startsWith("/api/")) {
      paths.push(pathname);
    }
  });
  return paths;
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await signInAsAdmin(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("the places by keyboard alone, in list order", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.goto("/settings/status");
        const list = page.getByRole("navigation", { name: PLACES });
        await expect(list).toBeVisible();

        await tabTo(page, list.getByRole("link", { name: "Status", exact: true }));
        for (const name of ["Repositories", "People", "Sign-in", "Invites", "Recovery links"]) {
          await page.keyboard.press("Tab");
          await expect(list.getByRole("link", { name, exact: true })).toBeFocused();
        }

        await tabTo(page, list.getByRole("link", { name: "People", exact: true }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/\/settings\/access\/people$/);
        await expect(page.getByRole("heading", { level: 1, name: "People" })).toBeFocused();
        await expect(list.getByRole("link", { name: "People", exact: true })).toHaveAttribute(
          "aria-current",
          "page",
        );
        expect(await violations(page), "/settings/access/people").toEqual([]);

        for (const [path, heading] of [
          ["/settings/status", "Status"],
          ["/settings/sign-in/invites", "Invites"],
        ] as const) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
          expect(await violations(page), path).toEqual([]);
        }
        await page.waitForLoadState("networkidle");
        expect(paths.filter((path) => !STUBBED.includes(path))).toEqual([]);
        expect(errors).toEqual([]);
      });

      test("the Section picker at 320 px", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await page.setViewportSize({ width: 320, height: 800 });
        await page.goto("/settings/status");
        await expect(page.getByRole("heading", { level: 1, name: "Status" })).toBeVisible();
        await expect(page.getByRole("navigation", { name: PLACES })).toHaveCount(0);

        const section = page.getByRole("button", { name: "Section" });
        const dialog = page.getByRole("dialog", { name: PLACES });
        await tabTo(page, section);
        await page.keyboard.press("Enter");
        await expect(dialog).toBeVisible();
        expect(await violations(page), "the open picker").toEqual([]);
        const overflow = () =>
          page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
          );
        expect(await overflow()).toBeLessThanOrEqual(0);

        await tabTo(page, dialog.getByRole("link", { name: "Invites", exact: true }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/\/settings\/sign-in\/invites$/);
        await expect(dialog).toHaveCount(0);
        await expect(page.getByRole("heading", { level: 1, name: "Invites" })).toBeFocused();

        await section.focus();
        await page.keyboard.press("Enter");
        await expect(dialog).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(dialog).toHaveCount(0);
        await expect(section).toBeFocused();
        await page.waitForLoadState("networkidle");
        expect(paths.filter((path) => !STUBBED.includes(path))).toEqual([]);
        expect(errors).toEqual([]);
      });

      test("old addresses redirect and Back skips them", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.goto("/");
        await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
        await page.goto("/access/recovery-links");
        await expect(page).toHaveURL(/\/settings\/sign-in\/recovery-links$/);
        await expect(page.getByRole("heading", { level: 1, name: "Recovery links" })).toBeVisible();
        await page.goBack();
        await expect(page).toHaveURL(/\/$/);
        await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

        const name = "admin";
        const person = ["", "/permissions", "/passkeys", "/sessions"].map(
          (rest) => `/api/v1/principals/${name}${rest}`,
        );
        await page.route(`**/api/v1/principals/${name}/permissions`, (route) =>
          route.fulfill({ json: { permissions: [] } }),
        );
        await page.route(`**/api/v1/principals/${name}/passkeys`, (route) =>
          route.fulfill({ json: [] }),
        );
        await page.route(`**/api/v1/principals/${name}/sessions`, (route) =>
          route.fulfill({ json: [] }),
        );
        await page.route(`**/api/v1/principals/${name}`, (route) =>
          route.fulfill({ json: { name, type: "USER" } }),
        );
        await page.goto("/access/people/admin");
        const crumbs = page.getByRole("navigation", { name: "Breadcrumb" });
        await expect(crumbs).toHaveText(/Settings\s*›\s*Access\s*›\s*People\s*›\s*admin/);
        await crumbs.getByRole("link", { name: "People" }).click();
        await expect(page).toHaveURL(/\/settings\/access\/people$/);
        await page.waitForLoadState("networkidle");
        expect(paths.filter((path) => ![...STUBBED, ...person].includes(path))).toEqual([]);
        expect(errors).toEqual([]);
      });
    });
  }
}

test("an encoded address to a hidden place says so and reads nothing", async ({ page }) => {
  const errors = consoleErrors(page);
  const paths = apiPaths(page);
  await signInAsAdmin(page);
  // The same person without admin rights: Settings lists Status alone.
  await page.route("**/api/v1/whoami", (route) =>
    route.fulfill({
      json: {
        name: "admin",
        type: "USER",
        task_token: null,
        ci_token: null,
        language: null,
        admin: false,
        has_access: true,
        may_deploy: false,
        picture: "e1",
      },
    }),
  );
  await page.setViewportSize({ width: 1280, height: 800 });

  for (const [path, place] of [
    ["/settings/%73ign-in/invites", "Invites"],
    ["/Settings/Sign-In/%52ecovery-Links", "Recovery links"],
    ["/settings/%61ccess/people", "People"],
    ["/settings/%53IGN-IN", "Sign-in"],
  ] as const) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { level: 1, name: `You cannot use ${place}` }),
    ).toBeVisible();
    await expect(page.getByRole("navigation", { name: PLACES }).getByRole("link")).toHaveText([
      "Status",
    ]);
  }
  await page.waitForLoadState("networkidle");
  expect(
    paths.filter((path) =>
      [
        "/api/v1/invites",
        "/api/v1/recovery-links",
        "/api/v1/settings/sign-in",
        // People's list; nothing else on these pages asks for it.
        "/api/v1/principals",
      ].includes(path),
    ),
  ).toEqual([]);
  expect(errors).toEqual([]);
});
