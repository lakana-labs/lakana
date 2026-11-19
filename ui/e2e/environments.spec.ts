import { expect, type Locator, type Page, test } from "@playwright/test";

import { ENVIRONMENTS_STUBBED, stubEnvironments, stubMara } from "./environments";
import { apiPaths } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";

/** An option's value that must go nowhere but the one request that carries it. */
const SENTINEL = "AKIA-sentinel-e2e";

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...ENVIRONMENTS_STUBBED].includes(path))).toEqual([]);
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
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`Environments, ${theme}, ${density}`, () => {
      test.beforeEach(async ({ page }) => {
        await signInAsAdmin(page);
        await stubEnvironments(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("Environments by keyboard", async ({ browserName, context, page }) => {
        test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        await addAdminPasskey(context, page);
        await stubStepUpCeremony(page);
        const creates = mutatingRequests(page, "/api/v1/environments");
        const writes = mutatingRequests(page, "/api/v1/environments/staging");
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.goto(`${ORIGIN}/settings/environments`);
        const list = page.getByRole("region", { name: "Environments", exact: true });
        await expect(list.getByText("S3 · s3://corp-lake/prod")).toBeVisible();
        await expect(page.getByRole("heading", { name: "Deleted environments" })).toBeVisible();
        expect(await violations(page), "the list").toEqual([]);

        await press(page, page.getByRole("button", { name: "Create environment" }));
        const create = page.getByRole("dialog");
        await typeInto(page, create.getByRole("textbox", { name: "Name" }), "staging");
        await typeInto(
          page,
          create.getByRole("textbox", { name: "Root" }),
          "file:///srv/lakana/staging",
        );
        expect(await violations(page), "Create environment").toEqual([]);
        await press(page, create.getByRole("button", { name: "Create environment" }));
        await expect(page.getByRole("heading", { level: 1, name: "staging" })).toBeVisible();
        await expect(page.getByText(/Created\. Give people access under/)).toBeVisible();
        expect(creates).toEqual([
          "POST /api/v1/sign-in/step-up/options",
          "POST /api/v1/sign-in/step-up",
          "POST /api/v1/environments",
        ]);
        await expectFocusKept(page, "the new environment's page");
        expect(await violations(page), "an environment's page").toEqual([]);

        await press(page, page.getByRole("button", { name: "Set retention of staging" }));
        const retention = page.getByRole("dialog");
        await tabTo(page, retention.getByRole("radio", { name: "Keep tables" }));
        await page.keyboard.press("ArrowDown");
        await expect(retention.getByRole("radio", { name: "Tables expire after" })).toBeChecked();
        expect(await violations(page), "Set retention").toEqual([]);
        await press(page, retention.getByRole("button", { name: "Save" }));
        await expect(page.getByText("tables expire after 30 days")).toBeVisible();
        await expectFocusKept(page, "Set retention");

        await press(page, page.getByRole("button", { name: "Protect staging" }));
        await expect(page.getByRole("button", { name: "Unprotect staging" })).toBeFocused();
        await press(page, page.getByRole("button", { name: "Unprotect staging" }));
        const unprotect = page.getByRole("dialog");
        expect(await violations(page), "Unprotect").toEqual([]);
        await press(page, unprotect.getByRole("button", { name: "Unprotect" }));
        await expect(page.getByRole("button", { name: "Protect staging" })).toBeFocused();

        await press(page, page.getByRole("button", { name: "Delete environment staging" }));
        const remove = page.getByRole("dialog");
        await tabTo(page, remove.getByRole("radio", { name: /Keep the files/ }));
        await page.keyboard.press("ArrowDown");
        await expect(remove.getByRole("radio", { name: /Delete the files at/ })).toBeChecked();
        await typeInto(
          page,
          remove.getByRole("textbox", { name: "Type staging to delete it" }),
          "staging",
        );
        expect(await violations(page), "Delete environment").toEqual([]);
        await press(page, remove.getByRole("button", { name: "Delete environment" }));
        await expect(page).toHaveURL(/\/settings\/environments$/);
        await expect(
          page.getByText("Deleted staging. Its files are deleted at the next clean-up."),
        ).toBeVisible();
        await expect(page.getByRole("heading", { level: 1, name: "Environments" })).toBeFocused();
        // The retention's PUT goes to …/retention, which this list leaves out; its result shows.
        expect(writes.filter((write) => !write.includes("step-up"))).toEqual([
          "PATCH /api/v1/environments/staging",
          "PATCH /api/v1/environments/staging",
          "DELETE /api/v1/environments/staging",
        ]);
        expectOnlyStubbed(paths);
        expect(errors).toEqual([]);
      });

      test("Delete the old ones asks for the passkey touch first", async ({
        browserName,
        context,
        page,
      }) => {
        test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
        const errors = consoleErrors(page);
        await addAdminPasskey(context, page);
        await stubStepUpCeremony(page);
        const deletes = mutatingRequests(
          page,
          "/api/v1/environments/dev/orphaned-locations/delete",
        );
        await page.goto(`${ORIGIN}/settings/environments/dev`);
        await page.getByRole("button", { name: "Find" }).click();
        await expect(
          page.getByRole("region", { name: "Leftover directories" }).getByText("recent, kept"),
        ).toBeVisible();
        // Finding deletes nothing and asks for no touch.
        expect(deletes).toEqual([]);

        await page.getByRole("button", { name: "Delete the old ones" }).click();
        await page.getByRole("dialog").getByRole("button", { name: "Delete them" }).click();

        await expect(page.getByRole("dialog")).toBeHidden();
        await expect
          .poll(() => deletes)
          .toEqual([
            "POST /api/v1/sign-in/step-up/options",
            "POST /api/v1/sign-in/step-up",
            "POST /api/v1/environments/dev/orphaned-locations/delete",
          ]);
        expect(errors).toEqual([]);
      });

      test("mara's rights", async ({ page }) => {
        await stubMara(page);
        const errors = consoleErrors(page);
        const paths = apiPaths(page);
        const writes: string[] = [];
        page.on("request", (request) => {
          if (request.method() !== "GET") writes.push(new URL(request.url()).pathname);
        });
        await page.goto(`${ORIGIN}/settings/environments`);
        const list = page.getByRole("region", { name: "Environments", exact: true });
        await expect(list.getByText("at least 1.2 GB")).toBeVisible();
        await expect(page.getByRole("button", { name: "Create environment" })).toHaveCount(0);
        await expect(page.getByRole("button", { name: "Restore dev-old" })).toBeVisible();
        await expect(
          page.getByRole("button", { name: "Delete the files of dev-old now" }),
        ).toBeVisible();
        await expect(page.getByRole("button", { name: /^Remove .* from the catalog/ })).toHaveCount(
          0,
        );
        await expect(page.getByRole("button", { name: /hdfs-old/ })).toHaveCount(0);
        expect(paths).not.toContain("/api/v1/environments/prod/usage");
        expect(await violations(page), "mara's list").toEqual([]);

        await page.goto(`${ORIGIN}/settings/environments/dev`);
        await page.getByRole("button", { name: "Set retention of dev" }).click();
        await page.getByRole("dialog").getByRole("button", { name: "Save" }).click();
        await expect(page.getByRole("dialog")).toHaveCount(0);
        await expect(page.getByRole("button", { name: "Delete environment dev" })).toBeVisible();
        await expect(page.getByRole("button", { name: "Protect dev" })).toHaveCount(0);
        await expect(page.getByRole("heading", { name: "Storage" })).toHaveCount(0);
        await expect(page.getByRole("heading", { name: "Leftover directories" })).toHaveCount(0);

        await page.goto(`${ORIGIN}/settings/environments/prod`);
        await expect(page.getByText("S3 · s3://corp-lake/prod")).toBeVisible();
        await expect(page.getByRole("main").getByRole("button")).toHaveCount(0);
        expect(writes).toEqual(["/api/v1/environments/dev/retention"]);
        expectOnlyStubbed(paths);
        expect(errors).toEqual([]);
      });

      test("an option goes nowhere", async ({ browserName, context, page }) => {
        test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
        await addAdminPasskey(context, page);
        await stubStepUpCeremony(page);
        const messages: string[] = [];
        page.on("console", (message) => messages.push(message.text()));
        const carrying: string[] = [];
        const addresses: string[] = [];
        page.on("request", (request) => {
          addresses.push(request.url());
          if ((request.postData() ?? "").includes(SENTINEL)) {
            carrying.push(`${request.method()} ${new URL(request.url()).pathname}`);
          }
        });
        await page.goto(`${ORIGIN}/settings/environments`);
        await page.getByRole("button", { name: "Create environment" }).click();
        const create = page.getByRole("dialog");
        await create.getByRole("textbox", { name: "Name" }).fill("staging");
        await create.getByRole("textbox", { name: "Root" }).fill("s3://corp-lake/staging");
        await create.getByRole("button", { name: "Add an option" }).click();
        await create.getByRole("textbox", { name: "Key" }).fill("key");
        await create.getByLabel("Value").fill(SENTINEL);
        await create.getByRole("button", { name: "Create environment" }).click();
        await expect(page.getByRole("heading", { level: 1, name: "staging" })).toBeVisible();

        expect(carrying).toEqual(["POST /api/v1/environments"]);
        expect(addresses.filter((address) => address.includes(SENTINEL))).toEqual([]);
        expect(messages.filter((message) => message.includes(SENTINEL))).toEqual([]);
        const stored = await page.evaluate(() =>
          JSON.stringify([
            Object.entries(window.localStorage),
            Object.entries(window.sessionStorage),
            location.href,
          ]),
        );
        expect(stored).not.toContain(SENTINEL);
        expect(await page.content()).not.toContain(SENTINEL);
      });

      test("320 px", async ({ page }) => {
        const errors = consoleErrors(page);
        await page.setViewportSize({ width: 320, height: 800 });
        await page.goto(`${ORIGIN}/settings/environments`);
        const list = page.getByRole("region", { name: "Environments", exact: true });
        await expect(list.getByText("S3 · s3://corp-lake/prod")).toBeVisible();
        expect(await overflow(page), "Environments").toBeLessThanOrEqual(0);
        expect(await list.evaluate((region) => region.scrollWidth > region.clientWidth)).toBe(true);

        await page.goto(`${ORIGIN}/settings/environments/dev`);
        await page.getByRole("button", { name: "Find" }).click();
        const leftover = page.getByRole("region", { name: "Leftover directories" });
        await expect(leftover.getByText("recent, kept")).toBeVisible();
        expect(await overflow(page), "an environment's page").toBeLessThanOrEqual(0);
        expect(await leftover.evaluate((region) => region.scrollWidth > region.clientWidth)).toBe(
          true,
        );
        expect(await violations(page), "Leftover directories at 320 px").toEqual([]);

        for (const name of [
          "Change storage of dev",
          "Set retention of dev",
          "Delete environment dev",
        ]) {
          await page.getByRole("button", { name }).click();
          const dialog = page.getByRole("dialog");
          await expect(dialog).toBeVisible();
          const box = await dialog.boundingBox();
          expect(box?.width ?? 999, name).toBeLessThanOrEqual(320);
          expect(await violations(page), `${name} at 320 px`).toEqual([]);
          await page.keyboard.press("Escape");
          await expect(dialog).toHaveCount(0);
        }
        expect(errors).toEqual([]);
      });
    });
  }
}
