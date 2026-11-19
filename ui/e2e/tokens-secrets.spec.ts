import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  NEW_TOKEN,
  SECRET_VALUE,
  stubSecretsOfDevOnly,
  stubTokensSecrets,
  TOKENS_STUBBED,
} from "./access";
import { apiPaths } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...TOKENS_STUBBED].includes(path))).toEqual([]);
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
 * Whether a text is anywhere in the page's text or attributes, or in either storage.
 *
 * @param page - The page.
 * @param text - The text.
 * @returns Where it was found.
 */
function foundIn(page: Page, text: string): Promise<string[]> {
  return page.evaluate((needle) => {
    const found: string[] = [];
    if (document.documentElement.outerHTML.includes(needle)) found.push("page");
    for (const input of document.querySelectorAll("input")) {
      if (input.value.includes(needle)) found.push("input");
    }
    for (const storage of [window.localStorage, window.sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index) ?? "";
        if (`${key}${storage.getItem(key) ?? ""}`.includes(needle)) found.push("storage");
      }
    }
    if (window.location.href.includes(needle)) found.push("address");
    return found;
  }, text);
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
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`Tokens and secrets, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await signInAsAdmin(page);
        await stubTokensSecrets(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("own token by keyboard", async ({ browserName, context, page }) => {
        test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await addAdminPasskey(context, page);
        await stubStepUpCeremony(page);
        const sent = mutatingRequests(page, "/api/v1/principals/admin/tokens");
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.goto(`${ORIGIN}/account`);
        await expect(page.getByRole("button", { name: "Revoke laptop" })).toBeVisible();

        await press(page, page.getByRole("button", { name: "Create a token" }));
        const dialog = page.getByRole("dialog", { name: "Create a token" });
        await expect(dialog).toBeVisible();
        await tabTo(page, dialog.getByRole("textbox", { name: "Name" }));
        await page.keyboard.type("build-box");
        await press(page, dialog.getByRole("button", { name: "Create" }));

        const shown = page.getByRole("dialog", { name: "Your new token build-box" });
        await expect(shown).toBeVisible();
        await expect(shown.getByText(NEW_TOKEN)).toBeVisible();
        await expect(shown.getByText(`lakana login --url ${ORIGIN}`)).toBeVisible();
        expect(sent).toEqual([
          "POST /api/v1/sign-in/step-up/options",
          "POST /api/v1/sign-in/step-up",
          "POST /api/v1/principals/admin/tokens",
        ]);
        expect(await violations(page), "the token shown once").toEqual([]);
        await expect(shown.getByRole("button", { name: "Done" })).toBeFocused();
        await page.keyboard.press("Enter");

        await expect(shown).toBeHidden();
        await expect(page.getByRole("heading", { level: 2, name: "API tokens" })).toBeFocused();
        await expect(page.getByRole("button", { name: "Revoke build-box" })).toBeVisible();
        expect(await foundIn(page, NEW_TOKEN)).toEqual([]);
        expectOnlyStubbed(paths);
        expect(errors).toEqual([]);
      });

      test("Tokens by keyboard", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.goto(`${ORIGIN}/settings/access/tokens`);
        const personal = page.getByRole("region", { name: "Live personal tokens" });
        await expect(personal.getByText("laptop")).toBeVisible();
        expect(await violations(page), "Tokens").toEqual([]);

        await tabTo(page, page.getByRole("textbox", { name: "Owner" }));
        await page.keyboard.type("maria");
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/\?owner=maria$/);
        await expect(personal.getByText("nightly-export")).toBeHidden();
        await page.keyboard.press("Control+A");
        await page.keyboard.press("Backspace");
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/\/settings\/access\/tokens$/);

        await press(page, page.getByRole("button", { name: "Show more" }));
        await expect(personal.getByText("workstation")).toBeVisible();
        await expectFocusKept(page, "after Show more");

        await press(page, page.getByRole("button", { name: "Revoke laptop of maria" }));
        const dialog = page.getByRole("dialog", { name: "Revoke this token?" });
        await expect(dialog).toBeVisible();
        await expect(dialog).toContainText("Task tokens minted with it stop working too.");
        expect(await violations(page), "the Revoke dialog").toEqual([]);
        await press(page, dialog.getByRole("button", { name: "Revoke the token" }));
        await expect(dialog).toBeHidden();
        await expect(
          page.getByRole("heading", { level: 2, name: "Personal tokens" }),
        ).toBeFocused();
        await expect(personal.getByText("laptop")).toBeHidden();

        await press(
          page,
          page.getByRole("button", {
            name: "Revoke the task token of ci-bot in prod, minted by hand",
          }),
        );
        await expect(dialog).toBeVisible();
        await press(page, dialog.getByRole("button", { name: "Revoke the token" }));
        await expect(dialog).toBeHidden();
        await expect(page.getByRole("heading", { level: 2, name: "Task tokens" })).toBeFocused();
        await expectFocusKept(page, "after the task token's revoke");
        expectOnlyStubbed(paths);
        expect(errors).toEqual([]);
      });

      test("Secrets for a person who manages some environments", async ({
        browserName,
        context,
        page,
      }) => {
        test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        const leaks: string[] = [];
        page.on("console", (message) => {
          if (message.text().includes(SECRET_VALUE)) leaks.push(`console: ${message.text()}`);
        });
        page.on("request", (request) => {
          if (request.url().includes(SECRET_VALUE)) leaks.push(`address: ${request.url()}`);
        });
        await stubSecretsOfDevOnly(page);
        await addAdminPasskey(context, page);
        await stubStepUpCeremony(page);
        const sent = mutatingRequests(page, "/api/v1/environments/dev/secrets");
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.goto(`${ORIGIN}/settings/access/secrets?environment=prod`);

        await expect(page.getByText("You cannot manage the secrets of prod.")).toBeVisible();
        const region = page.getByRole("region", { name: "Secrets of dev" });
        await expect(region.getByText("pypi-corp")).toBeVisible();
        expect(paths.filter((path) => path.includes("/environments/prod/"))).toEqual([]);
        await page.getByRole("button", { name: /^Environment: dev/ }).click();
        await expect(page.getByRole("option")).toHaveText(["dev"]);
        await page.keyboard.press("Escape");
        expect(await violations(page), "Secrets").toEqual([]);

        await press(page, page.getByRole("button", { name: "Add secret" }));
        const dialog = page.getByRole("dialog", { name: "Add a secret to dev" });
        await expect(dialog).toBeVisible();
        await tabTo(page, dialog.getByRole("textbox", { name: "Name" }));
        await page.keyboard.type("warehouse");
        await tabTo(page, dialog.getByRole("textbox", { name: "Key" }));
        await page.keyboard.type("password");
        await tabTo(page, dialog.getByLabel("Value"));
        await page.keyboard.type(SECRET_VALUE);
        expect(await violations(page), "Add secret").toEqual([]);
        await press(page, dialog.getByRole("button", { name: "Add secret" }));

        await expect(dialog).toBeHidden();
        await expect(region.getByText("warehouse")).toBeVisible();
        await expect(page.getByRole("button", { name: "Add secret" })).toBeFocused();
        expect(sent).toEqual([
          "POST /api/v1/sign-in/step-up/options",
          "POST /api/v1/sign-in/step-up",
          "POST /api/v1/environments/dev/secrets",
        ]);
        expect(await foundIn(page, SECRET_VALUE)).toEqual([]);
        expect(leaks).toEqual([]);
        expectOnlyStubbed(paths);
        expect(errors).toEqual([]);
      });

      test("320 px", async ({ page }) => {
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await page.setViewportSize({ width: 320, height: 800 });

        await page.goto(`${ORIGIN}/settings/access/tokens`);
        const personal = page.getByRole("region", { name: "Live personal tokens" });
        await expect(personal.getByText("laptop")).toBeVisible();
        expect(await pageScrollsSideways(page)).toBe(false);
        expect(
          await personal.evaluate((element) => element.scrollWidth > element.clientWidth),
        ).toBe(true);
        expect(await violations(page), "Tokens at 320 px").toEqual([]);
        await page.getByRole("button", { name: "Revoke laptop of maria" }).click();
        const revoke = page.getByRole("dialog", { name: "Revoke this token?" });
        await expect(revoke).toBeVisible();
        expect(await pageScrollsSideways(page)).toBe(false);
        await page.keyboard.press("Escape");

        await page.goto(`${ORIGIN}/settings/access/secrets`);
        const region = page.getByRole("region", { name: "Secrets of dev" });
        await expect(region.getByText("pypi-corp")).toBeVisible();
        expect(await pageScrollsSideways(page)).toBe(false);
        expect(await region.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
          true,
        );
        expect(await violations(page), "Secrets at 320 px").toEqual([]);
        for (const [button, name] of [
          ["Add secret", "Add a secret to dev"],
          ["Rotate pypi-corp", "Rotate pypi-corp"],
          ["Set use of pypi-corp", "Set the use of pypi-corp"],
          ["Delete pypi-corp", "Delete pypi-corp?"],
        ] as const) {
          await page.getByRole("button", { name: button }).click();
          const dialog = page.getByRole("dialog", { name });
          await expect(dialog).toBeVisible();
          const box = await dialog.boundingBox();
          expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
          expect((box?.x ?? 0) + (box?.width ?? 1000)).toBeLessThanOrEqual(320);
          expect(await pageScrollsSideways(page), name).toBe(false);
          await page.keyboard.press("Escape");
          await expect(dialog).toBeHidden();
        }
        expectOnlyStubbed(paths);
        expect(errors).toEqual([]);
      });
    });
  }
}
