import { expect, type Page, test } from "@playwright/test";

import { ACCESS_STUBBED, LAST_ADMIN, LONG_NAME, stubAccess } from "./access";
import { apiPaths } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const PLACES = "Settings places";

/**
 * Requires that every API request went to a stubbed path, and none carried the override.
 *
 * @param paths - The paths sent.
 * @param urls - The full addresses sent.
 */
function expectOnlyStubbed(paths: readonly string[], urls: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...ACCESS_STUBBED].includes(path))).toEqual([]);
  expect(urls.filter((url) => url.includes("override_managed"))).toEqual([]);
}

/**
 * Records the full addresses of the API requests.
 *
 * @param page - The page.
 * @returns The list, filled as requests go out.
 */
function apiUrls(page: Page): string[] {
  const urls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/")) urls.push(request.url());
  });
  return urls;
}

/**
 * Requires that the focus is on an element of the page, never on its body.
 *
 * @param page - The page.
 * @param step - What was just done.
 */
async function expectFocusKept(page: Page, step: string): Promise<void> {
  await expect
    .poll(() => page.evaluate(() => document.activeElement?.tagName ?? "BODY"), { message: step })
    .not.toBe("BODY");
}

/**
 * Whether the page scrolls sideways.
 *
 * @param page - The page.
 * @returns True when it does.
 */
function pageScrollsSideways(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`Access, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("Grants by keyboard alone", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls = apiUrls(page);
      await stubAccess(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const sent = mutatingRequests(page, "/api/v1/grants");
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/access/grants`);
      const region = page.getByRole("region", { name: "Grants" });
      await expect(region.getByRole("link", { name: "analysts" })).toBeVisible();

      for (const control of [
        page.getByRole("button", { name: /Who$/ }),
        page.getByRole("button", { name: /On exactly$/ }),
        page.getByRole("button", { name: /What$/ }),
        region,
        page.getByRole("button", { name: "Remove the grant ANALYST on dev for analysts" }),
      ]) {
        await tabTo(page, control);
        await expect(control).toBeFocused();
      }
      expect(await violations(page)).toEqual([]);

      const add = page.getByRole("button", { name: "Add grant" });
      await add.focus();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Add grant" });
      await expect(dialog).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(add).toBeFocused();

      await page.keyboard.press("Enter");
      await expect(dialog).toBeVisible();
      await tabTo(page, dialog.getByRole("radio", { name: "A person or service account" }));
      await page.keyboard.press("ArrowRight");
      await expect(dialog.getByRole("radio", { name: "A group" })).toBeChecked();
      await tabTo(page, dialog.getByRole("combobox", { name: "Name" }));
      await page.keyboard.type("op");
      // The group is chosen from the suggestions, by keyboard alone.
      await expect(page.getByRole("option", { name: "ops" })).toBeVisible();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await expect(page.getByRole("listbox")).toBeHidden();
      await expect(dialog.getByRole("combobox", { name: "Name" })).toHaveValue("ops");
      await tabTo(page, dialog.getByRole("button", { name: /Role$/ }));
      await page.keyboard.press("Enter");
      await page.getByRole("option", { name: /^ANALYST/ }).focus();
      await page.keyboard.press("Enter");
      await tabTo(page, dialog.getByRole("button", { name: /Environment$/ }));
      await page.keyboard.press("Enter");
      await page.getByRole("option", { name: /dev/ }).focus();
      await page.keyboard.press("Enter");
      await tabTo(page, dialog.getByRole("button", { name: "Grant" }));
      await page.keyboard.press("Enter");

      await expect(dialog).toBeHidden();
      await expect(page.getByText("Granted.")).toBeVisible();
      await expect(region.getByRole("link", { name: "ops" })).toBeVisible();
      expect(sent).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "POST /api/v1/grants",
      ]);
      await expectFocusKept(page, "after Grant");
      expectOnlyStubbed(paths, urls);
      expect(errors).toEqual([]);
    });

    test("a group by keyboard", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls = apiUrls(page);
      await stubAccess(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const writes: string[] = [];
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (request.method() !== "GET" && /^\/api\/v1\/(groups|sign-in\/step-up)/.test(path)) {
          writes.push(`${request.method()} ${path}`);
        }
      });
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/access/groups/analysts`);
      const members = page.getByRole("region", { name: "Members of analysts" });
      await expect(members.getByRole("link", { name: "maria" })).toBeVisible();
      expect(await violations(page)).toEqual([]);

      const field = page.getByRole("combobox", { name: "Person or service account" });
      await tabTo(page, field);
      await page.keyboard.type("ben");
      await page.keyboard.press("Enter");
      await expect(members.getByRole("link", { name: "ben" })).toBeVisible();
      await expect(field).toBeFocused();

      await tabTo(page, page.getByRole("button", { name: "Remove maria from analysts" }));
      await page.keyboard.press("Enter");
      const remove = page.getByRole("dialog", { name: "Remove maria from analysts?" });
      await expect(remove).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await tabTo(page, remove.getByRole("button", { name: "Remove", exact: true }));
      await page.keyboard.press("Enter");
      await expect(remove).toBeHidden();
      await expect(members.getByRole("link", { name: "maria" })).toBeHidden();
      await expectFocusKept(page, "after Remove");

      await tabTo(page, page.getByRole("button", { name: "Delete group analysts" }));
      await page.keyboard.press("Enter");
      const deleting = page.getByRole("dialog", { name: "Delete group analysts?" });
      await expect(deleting).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await tabTo(page, deleting.getByRole("textbox", { name: "Type analysts to confirm" }));
      await page.keyboard.type("analysts");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/settings\/access\/groups$/);
      await expect(page.getByRole("heading", { level: 1, name: "Groups" })).toBeFocused();
      await expect(page.getByRole("link", { name: "analysts" })).toBeHidden();
      // Each write asks for the passkey touch first: the stubbed session has none.
      const touch = ["POST /api/v1/sign-in/step-up/options", "POST /api/v1/sign-in/step-up"];
      expect(writes).toEqual([
        ...touch,
        "PUT /api/v1/groups/analysts/members/ben",
        ...touch,
        "DELETE /api/v1/groups/analysts/members/maria",
        ...touch,
        "DELETE /api/v1/groups/analysts",
      ]);
      expectOnlyStubbed(paths, urls);
      expect(errors).toEqual([]);
    });

    test("a new service account and a deactivation by keyboard", async ({
      browserName,
      context,
      page,
    }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls = apiUrls(page);
      await stubAccess(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const writes: string[] = [];
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (request.method() !== "GET" && /^\/api\/v1\/(principals|sign-in\/step-up)/.test(path)) {
          writes.push(`${request.method()} ${path}`);
        }
      });
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/access/people`);
      await expect(page.getByRole("link", { name: "admin" })).toBeVisible();
      expect(await violations(page), "People").toEqual([]);

      const create = page.getByRole("button", { name: "New service account" });
      await tabTo(page, create);
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "New service account" });
      await expect(dialog).toBeVisible();
      expect(await violations(page), "New service account").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(create).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(dialog).toBeVisible();
      await tabTo(page, dialog.getByRole("textbox", { name: "Name" }));
      await page.keyboard.type("ci-loader");
      await page.keyboard.press("Enter");
      await expect(dialog).toBeHidden();
      await expect(
        page.getByText(
          "Created the service account ci-loader. It can do nothing until it has a grant.",
        ),
      ).toBeVisible();
      await expect(page.getByRole("link", { name: "ci-loader" })).toBeVisible();
      await expect(page.getByRole("heading", { level: 1, name: "People" })).toBeFocused();

      await page.goto(`${ORIGIN}/settings/access/people/maria`);
      const deactivate = page.getByRole("button", { name: "Deactivate maria" });
      await tabTo(page, deactivate);
      await page.keyboard.press("Enter");
      const confirm = page.getByRole("dialog", { name: "Deactivate maria?" });
      await expect(confirm).toContainText("This cannot be undone in this release");
      expect(await violations(page), "Deactivate maria?").toEqual([]);
      await tabTo(page, confirm.getByRole("textbox", { name: "Type maria to confirm" }));
      await page.keyboard.type("maria");
      await page.keyboard.press("Enter");
      await expect(confirm).toBeHidden();
      await expect(page.getByText("maria is deactivated")).toBeVisible();
      await expect(page.getByRole("heading", { level: 1, name: "maria" })).toBeFocused();
      await expect(page.getByRole("button", { name: "Issue a recovery link" })).toHaveCount(0);
      expect(await violations(page), "a deactivated person").toEqual([]);

      // The last administrator: the server refuses, and the dialog says why in its words.
      await page.goto(`${ORIGIN}/settings/access/people/admin`);
      const own = page.getByRole("button", { name: "Deactivate admin" });
      await tabTo(page, own);
      await page.keyboard.press("Enter");
      const ownDialog = page.getByRole("dialog", { name: "Deactivate your own account?" });
      await expect(ownDialog).toBeVisible();
      await tabTo(page, ownDialog.getByRole("textbox", { name: "Type admin to confirm" }));
      await page.keyboard.type("admin");
      await page.keyboard.press("Enter");
      const refusal = ownDialog.getByRole("alert");
      await expect(refusal).toHaveText(LAST_ADMIN);
      await expect(refusal).toBeFocused();
      expect(await violations(page), "the refusal").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(ownDialog).toBeHidden();
      await expect(own).toBeFocused();

      // Each write asks for the passkey touch first: the stubbed session has none.
      const touch = ["POST /api/v1/sign-in/step-up/options", "POST /api/v1/sign-in/step-up"];
      expect(writes).toEqual([
        ...touch,
        "POST /api/v1/principals",
        ...touch,
        "DELETE /api/v1/principals/maria",
        ...touch,
        "DELETE /api/v1/principals/admin",
      ]);
      expectOnlyStubbed(paths, urls);
      // The browser logs the one refusal; nothing else.
      expect(errors.filter((error) => !error.includes("status of 409"))).toEqual([]);
      expect(errors.filter((error) => error.includes("status of 409"))).toHaveLength(1);
    });

    test("managed scopes", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls = apiUrls(page);
      await stubAccess(page, { managed: ["instance"] });
      await page.setViewportSize({ width: 1280, height: 800 });

      await page.goto("/settings/access/groups");
      await expect(page.getByRole("link", { name: "analysts" })).toBeVisible();
      await expect(page.getByText("The whole instance is managed by access.yaml")).toBeVisible();
      await expect(page.getByRole("button", { name: "Create group" })).toHaveCount(0);
      expect(await violations(page)).toEqual([]);

      await page.goto("/settings/access/groups/analysts");
      await expect(page.getByRole("link", { name: "maria" })).toBeVisible();
      await expect(page.getByRole("button", { name: /^(Remove|Delete group|Add)/ })).toHaveCount(0);
      await expect(page.getByRole("combobox", { name: "Person or service account" })).toHaveCount(
        0,
      );

      await page.goto("/settings/access/people");
      await expect(page.getByRole("link", { name: "admin" })).toBeVisible();
      await expect(page.getByText("The whole instance is managed by access.yaml")).toBeVisible();
      await expect(page.getByRole("button", { name: "New service account" })).toHaveCount(0);
      // People are never in an access file: deactivating a person stays offered.
      await page.goto("/settings/access/people/maria");
      await expect(page.getByRole("button", { name: "Deactivate maria" })).toBeVisible();
      expect(await violations(page)).toEqual([]);

      await page.goto("/settings/access/grants");
      const region = page.getByRole("region", { name: "Grants" });
      await expect(region.getByRole("link", { name: "analysts" })).toBeVisible();
      await expect(region.getByText("⌂ Git")).toHaveCount(3);
      await expect(page.getByRole("button", { name: /^Remove/ })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Add grant" })).toHaveCount(0);
      expect(await violations(page)).toEqual([]);
      expectOnlyStubbed(paths, urls);
      expect(errors).toEqual([]);
    });

    test("320 px", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls = apiUrls(page);
      await stubAccess(page, { longNames: true });
      await page.setViewportSize({ width: 320, height: 640 });

      for (const [address, name, value] of [
        ["/settings/access/grants", "Grants", LONG_NAME],
        ["/settings/access/groups/analysts", "Members of analysts", LONG_NAME],
        ["/settings/access/what-i-may-do", "The grants you hold", "ANALYST"],
      ] as const) {
        await page.goto(address);
        const region = page.getByRole("region", { name, exact: true });
        await expect(region.getByText(value, { exact: true }).first()).toBeVisible();
        expect(await pageScrollsSideways(page), address).toBe(false);
        await expect(region).toHaveAttribute("tabindex", "0");
        const scrolls = await region.evaluate((node) => node.scrollWidth > node.clientWidth);
        expect(scrolls, `${address} scrolls in its region`).toBe(true);
        if (value === LONG_NAME) {
          // The name stays on one line: as tall as a short one.
          const height = await region
            .getByText(value, { exact: true })
            .first()
            .evaluate((node) => node.getBoundingClientRect().height);
          const line = await region
            .locator("th")
            .first()
            .evaluate((node) => node.getBoundingClientRect().height);
          expect(height, `${address}: the long name on one line`).toBeLessThanOrEqual(line * 1.6);
        }
      }
      // The two dialogs of People fit too.
      for (const [address, button] of [
        ["/settings/access/people", "New service account"],
        ["/settings/access/people/maria", "Deactivate maria"],
      ] as const) {
        await page.goto(address);
        await page.getByRole("button", { name: button }).click();
        await expect(page.getByRole("dialog")).toBeVisible();
        expect(await pageScrollsSideways(page), `${button} at 320 px`).toBe(false);
        const sideways = await page
          .getByRole("dialog")
          .evaluate((node) => node.scrollWidth > node.clientWidth);
        expect(sideways, `${button}'s dialog at 320 px`).toBe(false);
        expect(await violations(page), `${button} at 320 px`).toEqual([]);
        await page.keyboard.press("Escape");
        await expect(page.getByRole("dialog")).toBeHidden();
      }
      expectOnlyStubbed(paths, urls);
      expect(errors).toEqual([]);
    });

    test("What I may do for a person without admin rights", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls = apiUrls(page);
      await stubAccess(page);
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

      await page.goto("/settings/access");
      await expect(page).toHaveURL(/\/settings\/access\/what-i-may-do$/);
      await expect(page.getByRole("heading", { level: 1, name: "What I may do" })).toBeVisible();
      await expect(page.getByRole("navigation", { name: PLACES }).getByRole("link")).toHaveText([
        "Status",
        "What I may do",
      ]);
      const addsUp = page.getByRole("region", { name: "What they add up to" });
      await expect(addsUp.getByText("Run queries").first()).toBeVisible();
      expect(await violations(page)).toEqual([]);
      expect(paths.filter((path) => /^\/api\/v1\/(groups|grants|access)/.test(path))).toEqual([]);
      expectOnlyStubbed(paths, urls);
      expect(errors).toEqual([]);
    });
  });
}
