import { expect, type Locator, type Page, test } from "@playwright/test";

import { EXPORTED, LONG_EXPRESSION, POLICIES_STUBBED, stubPolicies } from "./access";
import { apiPaths } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

const KEY = "lakana.preferences";
const PLACE = `${ORIGIN}/settings/access/filters-and-masks`;
const FILE_PLACE = `${ORIGIN}/settings/access/access-file`;
const SENTINEL = "sentinel-e2e-7Qx";

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...POLICIES_STUBBED].includes(path))).toEqual([]);
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

/**
 * Where a text is in the page's text or attributes, either storage or the address.
 *
 * @param page - The page.
 * @param text - The text.
 * @returns Where it was found.
 */
function foundIn(page: Page, text: string): Promise<string[]> {
  return page.evaluate((needle) => {
    const found: string[] = [];
    if (document.documentElement.outerHTML.includes(needle)) found.push("page");
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

for (const theme of ["light", "dark"] as const) {
  test.describe(`Row filters, masks and the access file, ${theme}`, () => {
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

    test("Row filters & masks by keyboard", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubPolicies(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const filtersSent = mutatingRequests(page, "/api/v1/row-filters/f-1");
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(PLACE);
      const filters = page.getByRole("region", { name: "Row filters" });
      await expect(filters.getByText("region = 'EU'")).toBeVisible();
      expect(await violations(page), "Row filters & masks").toEqual([]);

      await tabTo(page, page.getByRole("button", { name: /^Environment/ }));
      await tabTo(page, page.getByRole("button", { name: /Who$/ }));
      await tabTo(page, filters);
      await tabTo(page, page.getByRole("region", { name: "Column masks" }));
      await press(page, page.getByRole("button", { name: "Show more" }));
      await expect(filters.getByText("has_group('ops')")).toBeVisible();
      await expectFocusKept(page, "after Show more");

      await press(
        page,
        page.getByRole("button", {
          name: "Delete the row filter on dev › gold › station_ranking for maria",
        }),
      );
      const confirm = page.getByRole("dialog", { name: "Delete this row filter?" });
      await expect(confirm).toBeVisible();
      expect(await violations(page), "the Delete dialog").toEqual([]);
      await press(page, confirm.getByRole("button", { name: "Delete the row filter" }));
      await expect(confirm).toBeHidden();
      // The panel's heading is its list's count (#782).
      await expect(page.getByRole("heading", { level: 2, name: /row filters?$/ })).toBeFocused();
      await expect(filters.getByText("region = 'EU'")).toBeHidden();
      expect(filtersSent).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "DELETE /api/v1/row-filters/f-1",
      ]);

      const add = page.getByRole("button", { name: "Add filter or mask" });
      await press(page, add);
      const dialog = page.getByRole("dialog", { name: "Add filter or mask" });
      await expect(dialog).toBeVisible();
      await tabTo(page, dialog.getByRole("radio", { name: "Row filter" }));
      await page.keyboard.press("ArrowRight");
      await expect(dialog.getByRole("radio", { name: "Column mask" })).toBeChecked();
      await tabTo(page, dialog.getByRole("radio", { name: "Person or service account" }));
      await page.keyboard.press("ArrowRight");
      await expect(dialog.getByRole("radio", { name: "Group" })).toBeChecked();
      // The Name suggests the groups (#802): picked by keyboard.
      const name = dialog.getByRole("combobox", { name: "Name", exact: true });
      await tabTo(page, name);
      await page.keyboard.type("op");
      await expect(page.getByRole("option", { name: "ops" })).toBeVisible();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await expect(page.getByRole("listbox")).toBeHidden();
      await expect(name).toHaveValue("ops");
      await expect(dialog).toBeVisible();
      await press(page, dialog.getByRole("button", { name: /Environment$/ }));
      await page.getByRole("option", { name: "dev" }).press("Enter");
      for (const [field, text] of [
        ["Namespace", "gold"],
        ["Table", "station_ranking"],
        ["Column", "email"],
        ["Expression", "'***'"],
      ] as const) {
        // The table field suggests names: a combobox.
        const role = field === "Table" ? "combobox" : "textbox";
        await tabTo(page, dialog.getByRole(role, { name: field, exact: true }));
        await page.keyboard.type(text);
        if (role === "combobox") {
          // Escape closes the suggestions, which hide the rest of the dialog from screen readers
          // while they are open, and leaves the dialog open.
          await page.keyboard.press("Escape");
          await expect(page.getByRole("listbox")).toBeHidden();
          await expect(dialog).toBeVisible();
        }
      }
      expect(await violations(page), "Add filter or mask").toEqual([]);
      const masksSent = mutatingRequests(page, "/api/v1/column-masks");
      await press(page, dialog.getByRole("button", { name: "Create" }));
      await expect(dialog).toBeHidden();
      await expect(page.getByText("Column mask created.")).toBeVisible();
      await expect(
        page.getByRole("region", { name: "Column masks" }).getByText("group ops"),
      ).toBeVisible();
      expect(masksSent).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "POST /api/v1/column-masks",
      ]);

      await press(page, add);
      await expect(dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(add).toBeFocused();
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("managed scopes", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const urls: string[] = [];
      page.on("request", (request) => urls.push(request.url()));
      await stubPolicies(page, { managed: ["prod"] });
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(PLACE);
      const filters = page.getByRole("region", { name: "Row filters" });
      const prod = filters.getByRole("row").filter({ hasText: "fact_sales" });
      await expect(prod).toContainText("⌂ Git");
      await expect(prod.getByRole("button")).toHaveCount(0);
      await expect(
        filters.getByRole("button", { name: /^Delete the row filter on dev/ }),
      ).toBeVisible();
      expect(await violations(page), "a managed environment").toEqual([]);

      await page.unrouteAll({ behavior: "ignoreErrors" });
      await signInAsAdmin(page);
      await stubPolicies(page, { managed: ["instance"] });
      await page.goto(PLACE);
      await expect(page.getByText(/The whole instance is managed by/)).toBeVisible();
      await expect(
        page.getByText(
          'lakana column-mask revoke COLUMN_MASK_ID --override-managed --reason "WHY"',
        ),
      ).toBeVisible();
      await expect(filters.getByText("region = 'EU'")).toBeVisible();
      await expect(page.getByRole("button", { name: /^Delete/ })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Add filter or mask" })).toHaveCount(0);
      expect(await violations(page), "a managed instance").toEqual([]);
      expect(urls.filter((url) => url.includes("override_managed"))).toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("a policy manager", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubPolicies(page, { managed: ["prod"], policyManager: true });
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/access`);
      await expect(page).toHaveURL(/\/settings\/access\/filters-and-masks$/);
      const places = page.getByRole("navigation", { name: "Settings places" });
      await expect(places.getByRole("link")).toHaveText([
        "Status",
        "Row filters & masks",
        "What I may do",
      ]);
      const filters = page.getByRole("region", { name: "Row filters" });
      await expect(filters.getByText("maria")).toBeVisible();
      await expect(filters.getByRole("link", { name: "maria" })).toHaveCount(0);
      await expect(filters).toContainText("⌂ Git");
      expect(await violations(page), "a policy manager").toEqual([]);
      expect(paths.filter((path) => /^\/api\/v1\/access\/(status|export)/.test(path))).toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("download by keyboard", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const leaks: string[] = [];
      page.on("console", (message) => {
        if (message.text().includes(SENTINEL)) leaks.push(message.text());
      });
      await stubPolicies(page, { managed: ["prod"], overrides: true });
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(FILE_PLACE);
      await expect(
        page.getByRole("region", { name: "Managed scopes" }).getByText("Changed by hand (1)"),
      ).toBeVisible();
      await expect(page.getByRole("region", { name: "Changes by hand" })).toContainText(
        "incident 42",
      );
      expect(await violations(page), "Access file").toEqual([]);

      await press(page, page.getByRole("button", { name: "Download access file" }));
      const dialog = page.getByRole("dialog", { name: "Download access file" });
      await expect(dialog).toBeVisible();
      await tabTo(page, dialog.getByRole("radio", { name: "The whole instance" }));
      await page.keyboard.press("ArrowDown");
      await expect(dialog.getByRole("radio", { name: "Some environments" })).toBeChecked();
      await tabTo(page, dialog.getByRole("checkbox", { name: "dev" }));
      await page.keyboard.press("Space");
      expect(await violations(page), "Download access file").toEqual([]);
      const download = page.waitForEvent("download");
      await press(page, dialog.getByRole("button", { name: "Download" }));
      const saved = await download;
      expect(saved.suggestedFilename()).toBe("access.yaml");
      const stream = await saved.createReadStream();
      const chunks: Buffer[] = [];
      for await (const chunk of stream) chunks.push(chunk as Buffer);
      expect(Buffer.concat(chunks).toString("utf8")).toBe(EXPORTED);
      await expect(dialog).toBeHidden();
      await expect(page.getByText("Downloaded access.yaml.")).toBeVisible();
      expect(paths).toContain("/api/v1/access/export");
      expect(await foundIn(page, SENTINEL)).toEqual([]);
      expect(leaks).toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("320 px", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubPolicies(page, { managed: ["prod"], overrides: true, longExpression: true });
      await page.setViewportSize({ width: 320, height: 800 });
      await page.goto(PLACE);
      const filters = page.getByRole("region", { name: "Row filters" });
      await expect(filters.getByText("region = 'EU'")).toBeVisible();
      await page.getByRole("button", { name: "Show more" }).click();
      const long = filters
        .locator("code.expression")
        .filter({ hasText: LONG_EXPRESSION.slice(0, 40) });
      await expect(long).toBeVisible();
      expect(await long.textContent()).toBe(LONG_EXPRESSION);
      const cell = await long.evaluate((code) => {
        const td = code.closest("td");
        return td === null ? null : { cell: td.scrollWidth, shown: td.clientWidth };
      });
      expect(cell !== null && cell.cell <= cell.shown).toBe(true);
      expect(await pageScrollsSideways(page)).toBe(false);
      expect(await filters.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
        true,
      );
      await expect(filters).toHaveAttribute("tabindex", "0");
      expect(await violations(page), "Row filters & masks at 320 px").toEqual([]);
      for (const [button, name] of [
        ["Add filter or mask", "Add filter or mask"],
        [/^Delete the row filter on dev/, "Delete this row filter?"],
      ] as const) {
        await page.getByRole("button", { name: button }).first().click();
        const dialog = page.getByRole("dialog", { name });
        await expect(dialog).toBeVisible();
        const box = await dialog.boundingBox();
        expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
        expect((box?.x ?? 0) + (box?.width ?? 1000)).toBeLessThanOrEqual(320);
        expect(await pageScrollsSideways(page), name).toBe(false);
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
      }

      await page.goto(FILE_PLACE);
      const scopes = page.getByRole("region", { name: "Managed scopes" });
      await expect(scopes.getByText("access.yaml")).toBeVisible();
      expect(await pageScrollsSideways(page)).toBe(false);
      expect(await scopes.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
        true,
      );
      expect(await violations(page), "Access file at 320 px").toEqual([]);
      await page.getByRole("button", { name: "Download access file" }).click();
      const dialog = page.getByRole("dialog", { name: "Download access file" });
      await expect(dialog).toBeVisible();
      const box = await dialog.boundingBox();
      expect((box?.x ?? 0) + (box?.width ?? 1000)).toBeLessThanOrEqual(320);
      await page.keyboard.press("Escape");
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });
  });
}
