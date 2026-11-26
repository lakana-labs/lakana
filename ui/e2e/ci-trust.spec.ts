import { expect, type Locator, type Page, test } from "@playwright/test";

import { CI_TRUST_STUBBED, NEW_RULE, PROD_RULE, stubCiTrust } from "./ciTrust";
import { ENVIRONMENTS_STUBBED, stubEnvironments } from "./environments";
import { apiPaths } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const PLACE = `${ORIGIN}/settings/ci-trust`;
const RULES = "/api/v1/ci-trust-rules";

/** The decoded claims of a push to main, as GitHub writes them. */
const CLAIMS = JSON.stringify({
  iss: "https://token.actions.githubusercontent.com",
  repository_id: "123456",
  ref: "refs/heads/main",
  event_name: "push",
  environment: "prod",
});

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(
    paths.filter(
      (path) => ![...STUBBED, ...CI_TRUST_STUBBED, ...ENVIRONMENTS_STUBBED].includes(path),
    ),
  ).toEqual([]);
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
 * Types into a field reached by Tab.
 *
 * @param page - The page.
 * @param field - The field.
 * @param text - What to type.
 */
async function typeInto(page: Page, field: Locator, text: string): Promise<void> {
  await tabTo(page, field);
  await expect(field).toBeFocused();
  await page.keyboard.type(text);
}

/**
 * Chooses an option of a select reached by Tab, by keyboard.
 *
 * @param page - The page.
 * @param select - The select's button.
 * @param option - The option's name.
 */
async function choose(page: Page, select: Locator, option: string): Promise<void> {
  await press(page, select);
  await page.getByRole("option", { name: option }).press("Enter");
  await expect(select).toContainText(option);
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
  test.describe(`CI trust, ${theme}`, () => {
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

    test("CI trust by keyboard", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubCiTrust(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const adds = mutatingRequests(page, RULES);
      const changes = mutatingRequests(page, `${RULES}/${PROD_RULE}`);
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(PLACE);
      const list = page.getByRole("region", { name: "CI trust rules" });
      await expect(list.getByRole("link", { name: "analytics to prod" })).toBeVisible();
      expect(await violations(page), "the list").toEqual([]);

      await press(page, page.getByRole("button", { name: "Add rule" }));
      const add = page.getByRole("dialog", { name: "Add a CI trust rule" });
      await choose(page, add.getByRole("button", { name: /Environment$/ }), "prod");
      await choose(page, add.getByRole("button", { name: /Repository$/ }), "dbt-models");
      await typeInto(page, add.getByRole("combobox", { name: "Acts as" }), "ci-prod");
      await page.keyboard.press("Escape");
      await typeInto(page, add.getByRole("textbox", { name: "Value of claim 1" }), "654321");
      expect(await violations(page), "Add a CI trust rule").toEqual([]);
      await press(page, add.getByRole("button", { name: "Add rule" }));
      await expect(page.getByText("Added the rule for dbt-models to prod.")).toBeVisible();
      await expect(page.getByRole("heading", { level: 1, name: "CI trust" })).toBeFocused();
      expect(adds).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        `POST ${RULES}`,
      ]);
      await expect(list.getByRole("link", { name: "dbt-models to prod" })).toHaveAttribute(
        "href",
        `/settings/ci-trust/${NEW_RULE}`,
      );

      await press(page, list.getByRole("link", { name: "analytics to prod" }));
      await expect(
        page.getByRole("heading", { level: 1, name: "analytics to prod" }),
      ).toBeVisible();
      expect(await violations(page), "a rule's page").toEqual([]);

      await press(page, page.getByRole("button", { name: "Edit the rule for analytics to prod" }));
      const edit = page.getByRole("dialog", { name: "Edit the rule for analytics to prod" });
      await tabTo(page, edit.getByRole("checkbox", { name: "Dry run" }));
      await page.keyboard.press("Space");
      expect(await violations(page), "Edit a rule").toEqual([]);
      await press(page, edit.getByRole("button", { name: "Save" }));
      await expect(page.getByText(/^Saved\./)).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Edit the rule for analytics to prod" }),
      ).toBeFocused();
      await expect(page.getByText("Deploy and Dry run")).toBeVisible();

      await press(
        page,
        page.getByRole("button", { name: "Disable the rule for analytics to prod" }),
      );
      const disable = page.getByRole("dialog", { name: "Disable the rule for analytics to prod?" });
      expect(await violations(page), "Disable").toEqual([]);
      await press(page, disable.getByRole("button", { name: "Disable" }));
      const enable = page.getByRole("button", { name: "Enable the rule for analytics to prod" });
      await expect(enable).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("button", { name: "Disable the rule for analytics to prod" }),
      ).toBeFocused();

      await press(
        page,
        page.getByRole("button", { name: "Delete rule for analytics to prod" }),
      );
      const remove = page.getByRole("dialog", { name: "Delete the rule for analytics to prod?" });
      expect(await violations(page), "Delete").toEqual([]);
      await press(page, remove.getByRole("button", { name: "Delete rule" }));
      await expect(page).toHaveURL(/\/settings\/ci-trust$/);
      await expect(page.getByText("Deleted the rule for analytics to prod.")).toBeVisible();
      await expect(page.getByRole("heading", { level: 1, name: "CI trust" })).toBeFocused();
      expect(changes.filter((change) => !change.includes("step-up"))).toEqual([
        `PATCH ${RULES}/${PROD_RULE}`,
        `DELETE ${RULES}/${PROD_RULE}`,
      ]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("Explain by keyboard", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubCiTrust(page);
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(PLACE);
      await press(page, page.getByRole("link", { name: "Explain a CI job's claims" }));
      await expect(
        page.getByRole("heading", { level: 1, name: "Explain a CI job's claims" }),
      ).toBeVisible();
      await tabTo(page, page.getByRole("textbox", { name: "Claims" }));
      await page.keyboard.insertText(CLAIMS);
      await choose(page, page.getByRole("button", { name: /Environment$/ }), "prod");
      await choose(page, page.getByRole("button", { name: /Repository$/ }), "analytics");
      await press(page, page.getByRole("button", { name: "Explain" }));
      const verdict = page.getByRole("region", { name: "Verdict" });
      await expect(verdict.getByText("Accepted")).toBeVisible();
      await expect(page.getByRole("heading", { level: 2, name: "Verdict" })).toBeFocused();
      await expect(
        page.getByRole("region", { name: "Every rule" }).getByText("Does not match"),
      ).toBeVisible();
      expect(await violations(page), "Explain").toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("a managed environment's rule offers no change", async ({ page }) => {
      await stubCiTrust(page, ["prod"]);
      await page.goto(`${PLACE}/${PROD_RULE}`);
      await expect(page.getByText(/The environment prod is managed by access.yaml/)).toBeVisible();
      await expect(page.getByRole("button", { name: /Edit the rule/ })).toHaveCount(0);
      expect(await violations(page), "a managed rule").toEqual([]);
    });

    test("run-as by keyboard", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubEnvironments(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const sent = mutatingRequests(page, "/api/v1/environments/dev/deployments/analytics/run-as");
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/environments/dev`);
      const region = page.getByRole("region", { name: "Deployments in dev" });
      await expect(region.getByText(/No account: runs started by a person/)).toBeVisible();

      await press(page, page.getByRole("button", { name: "Set run-as for analytics" }));
      const dialog = page.getByRole("dialog", {
        name: "Set the run-as account of analytics in dev",
      });
      await typeInto(page, dialog.getByRole("combobox", { name: "Service account" }), "etl-dev");
      await page.keyboard.press("Escape");
      expect(await violations(page), "Set run-as").toEqual([]);
      await press(page, dialog.getByRole("button", { name: "Save" }));
      await expect(page.getByText("Runs of analytics in dev now write as etl-dev.")).toBeVisible();
      const change = page.getByRole("button", { name: "Change the run-as account of analytics" });
      await expect(change).toBeFocused();
      await expect(region.getByText("etl-dev")).toBeVisible();

      await press(
        page,
        page.getByRole("button", { name: "Clear the run-as account of analytics" }),
      );
      const clear = page.getByRole("dialog", {
        name: "Clear the run-as account of analytics in dev?",
      });
      expect(await violations(page), "Clear run-as").toEqual([]);
      await press(page, clear.getByRole("button", { name: "Clear" }));
      await expect(
        page.getByRole("button", { name: "Set run-as for analytics" }),
      ).toBeFocused();
      expect(sent).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "PUT /api/v1/environments/dev/deployments/analytics/run-as",
        "DELETE /api/v1/environments/dev/deployments/analytics/run-as",
      ]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("320 px", async ({ page }) => {
      await stubCiTrust(page);
      await stubEnvironments(page);
      await page.setViewportSize({ width: 320, height: 800 });
      for (const [name, path] of [
        ["the list", PLACE],
        ["a rule's page", `${PLACE}/${PROD_RULE}`],
        ["Explain", `${PLACE}/explain`],
        ["an environment's deployments", `${ORIGIN}/settings/environments/prod`],
      ] as const) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.getByText(/^(Reading|Loading)/)).toHaveCount(0);
        expect(await overflow(page), `${name} at 320 px`).toBeLessThanOrEqual(0);
        expect(await violations(page), `${name} at 320 px`).toEqual([]);
      }
      await page.goto(PLACE);
      await page.getByRole("button", { name: "Add rule" }).click();
      const dialog = page.getByRole("dialog", { name: "Add a CI trust rule" });
      await expect(dialog).toBeVisible();
      const box = await dialog.boundingBox();
      expect(box?.width ?? 999, "Add a CI trust rule at 320 px").toBeLessThanOrEqual(320);
      expect(await overflow(page), "Add a CI trust rule at 320 px").toBeLessThanOrEqual(0);
      expect(await violations(page), "Add a CI trust rule at 320 px").toEqual([]);
    });
  });
}
