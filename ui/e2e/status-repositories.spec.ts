import { expect, type Locator, type Page, test } from "@playwright/test";

import { apiPaths } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { REPOSITORIES_STUBBED, stubReader, stubRepositories } from "./repositories";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...REPOSITORIES_STUBBED].includes(path))).toEqual([]);
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
 * Presses Enter on a control reached by Tab.
 *
 * @param page - The page.
 * @param control - The control.
 */
async function press(page: Page, control: Locator): Promise<void> {
  await tabTo(page, control);
  await expect(control).toBeFocused();
  await page.keyboard.press("Enter");
}

/**
 * How far the page scrolls sideways.
 *
 * @param page - The page.
 * @returns The pixels; 0 or less when it does not.
 */
function overflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`Status and Repositories, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await stubRepositories(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("Status for a manager", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/status`);

      await expect(page.getByText("2 workers up, 1 lost")).toBeVisible();
      await expect(page.getByRole("meter", { name: "Compute" })).toBeVisible();
      const workers = page.getByRole("region", { name: "Workers" });
      await expect(workers.getByRole("row").nth(1)).toContainText("worker-2.internal");
      await expect(workers.getByText("differs from the server")).toBeVisible();
      await expect(page.getByText("lakana-server-1")).toBeVisible();
      await expect(page.getByText("This server runs Lakana 0.23.0.")).toBeVisible();
      expect(await violations(page), "Status for a manager").toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("Status for everyone", async ({ page }) => {
      await stubReader(page);
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await page.goto(`${ORIGIN}/settings/status`);

      await expect(page.getByText("2 workers up, 1 lost")).toBeVisible();
      await expect(page.getByText("This server runs Lakana 0.23.0.")).toBeVisible();
      await expect(page.getByRole("heading", { name: "Workers" })).toHaveCount(0);
      expect(paths).not.toContain("/api/v1/scheduler");
      expect(await violations(page), "Status for everyone").toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("Repositories by keyboard", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const keys = mutatingRequests(page, "/api/v1/repositories/analytics/deploy-key");
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/repositories`);
      const list = page.getByRole("region", { name: "Repositories" });
      await expect(list.getByText("Deploy key in 1 environment")).toBeVisible();
      expect(await violations(page), "the list").toEqual([]);

      await press(page, list.getByRole("link", { name: "analytics" }));
      await expect(page.getByRole("heading", { level: 1, name: "analytics" })).toBeVisible();
      await expectFocusKept(page, "the repository's page");
      await expect(page.getByText("SHA256:dev7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe")).toBeVisible();
      expect(await violations(page), "a repository's page").toEqual([]);

      await press(page, page.getByRole("button", { name: "Test access to analytics" }));
      await expect(page.getByRole("alert")).toContainText("then test again.");
      await expectFocusKept(page, "Test");

      await press(page, page.getByRole("button", { name: "Replace key of dev" }));
      const replace = page.getByRole("dialog");
      await expect(replace).toContainText("The old key stops working at once.");
      expect(await violations(page), "Replace key").toEqual([]);
      await press(page, replace.getByRole("button", { name: "Replace key" }));
      await expect(replace.getByText("ssh-ed25519 AAAANEW dev")).toBeVisible();
      expect(keys).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "POST /api/v1/repositories/analytics/deploy-key",
      ]);
      await press(page, replace.getByRole("button", { name: "Done" }));
      await expect(page.getByRole("heading", { level: 3, name: "Deploy keys" })).toBeFocused();

      await press(page, page.getByRole("button", { name: "Delete repository analytics" }));
      const remove = page.getByRole("dialog");
      await tabTo(page, remove.getByRole("textbox", { name: "Type analytics to delete it" }));
      await page.keyboard.type("analytics");
      await press(page, remove.getByRole("button", { name: "Delete repository" }));
      await expect(page).toHaveURL(/\/settings\/repositories$/);
      await expect(page.getByRole("link", { name: "analytics" })).toHaveCount(0);
      await expectFocusKept(page, "Delete");
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("a reader writes nothing", async ({ page }) => {
      await stubReader(page);
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const writes: string[] = [];
      page.on("request", (request) => {
        if (request.method() !== "GET") writes.push(`${request.method()} ${request.url()}`);
      });
      await page.goto(`${ORIGIN}/settings/repositories`);
      await expect(page.getByText("Token in secret gh-token")).toBeVisible();
      await expect(page.getByRole("link", { name: "Connect a repository" })).toHaveCount(0);

      await page.goto(`${ORIGIN}/settings/repositories/analytics`);
      await expect(page.getByText("SHA256:dev7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe")).toBeVisible();
      await expect(page.getByRole("button", { name: /^(Edit|Delete|Replace|Test)/ })).toHaveCount(
        0,
      );
      expect(await violations(page), "a repository's page, read-only").toEqual([]);

      await page.goto(`${ORIGIN}/settings/repositories/new`);
      await expect(
        page.getByRole("heading", { level: 1, name: "You cannot use Connect a repository" }),
      ).toBeVisible();
      expect(writes).toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("320 px", async ({ page }) => {
      const errors = consoleErrors(page);
      await page.setViewportSize({ width: 320, height: 800 });
      await page.goto(`${ORIGIN}/settings/status`);
      await expect(page.getByRole("region", { name: "Workers" })).toBeVisible();
      expect(await overflow(page), "Status").toBeLessThanOrEqual(0);

      await page.goto(`${ORIGIN}/settings/repositories`);
      const list = page.getByRole("region", { name: "Repositories" });
      await expect(list.getByText("gh-token", { exact: false })).toBeVisible();
      expect(await overflow(page), "Repositories").toBeLessThanOrEqual(0);
      expect(await list.evaluate((region) => region.scrollWidth > region.clientWidth)).toBe(true);

      await page.goto(`${ORIGIN}/settings/repositories/analytics`);
      await expect(page.getByText("SHA256:dev7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe")).toBeVisible();
      expect(await overflow(page), "a repository's page").toBeLessThanOrEqual(0);
      await page.getByRole("button", { name: "Edit repository analytics" }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      const box = await dialog.boundingBox();
      expect(box?.width ?? 999).toBeLessThanOrEqual(320);
      expect(await violations(page), "Edit at 320 px").toEqual([]);
      expect(errors).toEqual([]);
    });
  });
}
