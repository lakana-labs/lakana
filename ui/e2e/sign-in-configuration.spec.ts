import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  CA_BUNDLE,
  CONFIGURATION,
  INVITE_URL,
  LONG_NAME,
  SIGN_IN_STUBBED,
  stubConfiguration,
  stubSignInAndConfiguration,
} from "./access";
import { apiPaths, rawOverrides } from "./hidden";
import { addAdminPasskey, mutatingRequests, ORIGIN, stubStepUpCeremony } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

// Settings › Sign-in (the session lengths, Invites and Recovery links) and Settings ›
// Configuration against stubbed routes, in both themes and densities.

const KEY = "lakana.preferences";

/** A value the server must never send for a secret; if one does, the page must not show it. */
const MASTER_KEY = "mk-sentinel-e2e-5Rt";

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...SIGN_IN_STUBBED].includes(path))).toEqual([]);
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
 * Whether an element is wider than its box, so that it scrolls.
 *
 * @param element - The element.
 * @returns True when it scrolls sideways.
 */
function scrollsSideways(element: Locator): Promise<boolean> {
  return element.evaluate((node) => node.scrollWidth > node.clientWidth);
}

/**
 * Where a text is in the page's markup, its inputs, either storage or the address.
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

for (const theme of ["light", "dark"] as const) {
  test.describe(`Sign-in and Configuration, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await stubSignInAndConfiguration(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("Sign-in by keyboard", async ({ browserName, context, page }) => {
      test.skip(browserName !== "chromium", "the passkey touch uses a virtual authenticator");
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await addAdminPasskey(context, page);
      await stubStepUpCeremony(page);
      const saved = mutatingRequests(page, "/api/v1/settings/sign-in");
      const invited = mutatingRequests(page, "/api/v1/invites");
      const recovered = mutatingRequests(page, "/api/v1/principals/kim.schulz/recovery-link");
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/sign-in`);
      const lengths = page.getByRole("region", { name: "Session lengths" });
      await expect(lengths.getByRole("textbox", { name: "Idle timeout in minutes" })).toHaveValue(
        "720",
      );
      expect(await violations(page), "Sign-in").toEqual([]);

      await tabTo(page, lengths.getByRole("textbox", { name: "Idle timeout in minutes" }));
      await page.keyboard.press("Control+A");
      await page.keyboard.type("60");
      await press(page, lengths.getByRole("button", { name: "Save" }));
      await expect(page.getByText("Saved. New sessions use these lengths.")).toBeVisible();
      await expect(lengths.getByRole("button", { name: "Save" })).toBeFocused();
      expect(saved).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "PATCH /api/v1/settings/sign-in",
      ]);

      const list = page.getByRole("navigation", { name: "Settings places" });
      await press(page, list.getByRole("link", { name: "Invites", exact: true }));
      await expect(page.getByRole("heading", { level: 1, name: "Invites" })).toBeFocused();
      const invite = page.getByRole("button", { name: "Invite someone" });
      await press(page, invite);
      const dialog = page.getByRole("dialog", { name: "Invite someone" });
      await expect(dialog).toBeVisible();
      await tabTo(page, dialog.getByRole("textbox", { name: "Name", exact: true }));
      await page.keyboard.type("kim.schulz");
      expect(await violations(page), "Invite someone").toEqual([]);
      await press(page, dialog.getByRole("button", { name: "Create the invite" }));

      const shown = page.getByRole("dialog", { name: "Send this link to kim.schulz" });
      await expect(shown.getByText(INVITE_URL)).toBeVisible();
      expect(invited.slice(-3)).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "POST /api/v1/invites",
      ]);
      expect(await violations(page), "the invite shown once").toEqual([]);
      await expect(shown.getByRole("button", { name: "Done" })).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(shown).toBeHidden();
      await expect(invite).toBeFocused();
      await expect(
        page.getByRole("region", { name: "Open invites" }).getByText("kim.schulz"),
      ).toBeVisible();
      expect(await foundIn(page, INVITE_URL)).toEqual([]);

      await press(page, list.getByRole("link", { name: "Recovery links", exact: true }));
      const issue = page.getByRole("button", { name: "Issue a recovery link" });
      await press(page, issue);
      const ask = page.getByRole("dialog", { name: "Issue a recovery link" });
      await tabTo(page, ask.getByRole("textbox", { name: "Name" }));
      await page.keyboard.type("kim.schulz");
      await press(page, ask.getByRole("button", { name: "Continue" }));
      const link = page.getByRole("dialog", { name: "Send this link to kim.schulz" });
      await expect(link.getByText(INVITE_URL)).toBeVisible();
      expect(recovered.slice(-3)).toEqual([
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/step-up",
        "POST /api/v1/principals/kim.schulz/recovery-link",
      ]);
      expect(await violations(page), "the recovery link shown once").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(link).toBeHidden();
      await expect(issue).toBeFocused();
      await expectFocusKept(page, "after the recovery link");
      expect(await foundIn(page, INVITE_URL)).toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("old addresses still lead to Sign-in", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await page.setViewportSize({ width: 1280, height: 800 });
      for (const [old, place, heading] of [
        ["/access/invites", "/settings/sign-in/invites", "Invites"],
        ["/access/recovery-links", "/settings/sign-in/recovery-links", "Recovery links"],
      ] as const) {
        await page.goto(`${ORIGIN}/`);
        await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
        await page.goto(`${ORIGIN}${old}?x=1#y`);
        await expect(page).toHaveURL(`${ORIGIN}${place}?x=1#y`);
        await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
        await page.goBack();
        await expect(page).toHaveURL(`${ORIGIN}/`);
      }
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("Configuration by keyboard", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const logged: string[] = [];
      page.on("console", (message) => logged.push(message.text()));
      // A faulty server that sends the master key's value: the page reads has_value alone.
      await stubConfiguration(
        page,
        CONFIGURATION.map((setting) =>
          setting.name === "LAKANA_SECRETS__MASTER_KEY"
            ? { ...setting, value: MASTER_KEY }
            : setting,
        ),
      );
      await page.setViewportSize({ width: 760, height: 800 });
      await page.goto(`${ORIGIN}/settings/configuration`);
      const region = page.getByRole("region", { name: "Server settings" });
      await expect(region.getByText("LAKANA_PORT")).toBeVisible();
      await expect(region.getByRole("row", { name: /^LAKANA_SECRETS__MASTER_KEY/ })).toContainText(
        "secret, set",
      );
      await expect(page.getByText(/take effect after a restart/)).toBeVisible();
      expect(await violations(page), "Configuration").toEqual([]);

      const find = page.getByRole("searchbox", { name: "Find a setting" });
      await tabTo(page, find);
      await page.keyboard.type("proxy");
      await expect(page).toHaveURL(/\/settings\/configuration\?q=proxy$/);
      await expect(region.locator("td .row-name")).toHaveText([
        "LAKANA_RUN__HTTPS_PROXY",
        "LAKANA_RUN__NO_PROXY",
        "LAKANA_CI_OIDC__PROXY",
      ]);
      await expect(find).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(region).toBeFocused();
      expect(await scrollsSideways(region)).toBe(true);
      await page.keyboard.press("End");
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => region.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);

      // Tab into the field selects its words, so typing replaces them.
      await tabTo(page, find);
      await page.keyboard.type("zzz");
      await expect(page.getByText("No setting matches “zzz”.")).toBeVisible();
      await expect(find).toBeFocused();
      expect(await violations(page), "no setting matches").toEqual([]);
      await press(page, page.getByRole("button", { name: "Clear search" }));
      await expect(find).toBeFocused();
      await expect(find).toHaveValue("");
      await expect(region.locator("td .row-name")).toHaveCount(CONFIGURATION.length);
      await expectFocusKept(page, "after Clear search");

      expect(await foundIn(page, MASTER_KEY)).toEqual([]);
      expect(logged.filter((text) => text.includes(MASTER_KEY))).toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("320 px", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await page.setViewportSize({ width: 320, height: 800 });

      await page.goto(`${ORIGIN}/settings/sign-in`);
      await expect(page.getByRole("textbox", { name: "Idle timeout in minutes" })).toBeVisible();
      expect(await pageScrollsSideways(page), "Sign-in").toBe(false);
      expect(await violations(page), "Sign-in at 320 px").toEqual([]);

      for (const [path, name] of [
        ["/settings/sign-in/invites", "Open invites"],
        ["/settings/sign-in/recovery-links", "Open recovery links"],
      ] as const) {
        await page.goto(`${ORIGIN}${path}`);
        const region = page.getByRole("region", { name });
        const long = region.locator(".row-name", { hasText: LONG_NAME });
        await expect(long).toBeVisible();
        expect(await pageScrollsSideways(page), path).toBe(false);
        expect(await scrollsSideways(region), path).toBe(true);
        // The name is whole: one line, as wide as its text.
        const lines = await long.evaluate(
          (node) =>
            node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight),
        );
        expect(lines, `${path}: the name on one line`).toBeLessThan(1.5);
        expect(await violations(page), `${path} at 320 px`).toEqual([]);
      }

      await page.goto(`${ORIGIN}/settings/configuration`);
      const region = page.getByRole("region", { name: "Server settings" });
      const bundle = region.getByText(CA_BUNDLE);
      await expect(bundle).toBeAttached();
      expect(await pageScrollsSideways(page), "Configuration").toBe(false);
      expect(await scrollsSideways(region), "Configuration's list").toBe(true);
      const cell = region.locator("td.setting-value", { hasText: CA_BUNDLE });
      expect(await scrollsSideways(cell), "the CA bundle wraps in its cell").toBe(false);
      const cellLines = await cell.evaluate(
        (node) =>
          node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight),
      );
      expect(cellLines, "the CA bundle takes several lines").toBeGreaterThan(2);
      for (const name of ["LAKANA_RUN__INGESTION_ROOTS", "LAKANA_CI_OIDC__ALGORITHMS"]) {
        const height = await region
          .locator(".setting-name", { hasText: name })
          .evaluate(
            (node) =>
              node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight),
          );
        expect(height, `${name} on one line`).toBeLessThan(1.5);
      }
      expect(await violations(page), "Configuration at 320 px").toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("hidden characters come out marked", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubConfiguration(page, [
        {
          name: "LAKANA_BOOTSTRAP_ADMIN",
          value: "ops‮admin",
          secret: false,
          has_value: true,
          source: "set",
        },
      ]);
      await page.route("**/api/v1/invites", (route) =>
        route.fulfill({
          json: [
            {
              id: "i-9",
              name: "k‮im",
              display_name: "Kim‮ Lee",
              created_at: "2026-11-18T09:00:00Z",
              expires_at: "2026-11-25T09:00:00Z",
            },
          ],
        }),
      );
      await page.setViewportSize({ width: 1280, height: 800 });

      await page.goto(`${ORIGIN}/settings/configuration`);
      await expect(page.getByRole("main")).toContainText("opsU+202Eadmin");
      expect(await rawOverrides(page), "Configuration").toEqual([]);
      await page.goto(`${ORIGIN}/settings/sign-in/invites`);
      const region = page.getByRole("region", { name: "Open invites" });
      await expect(region).toContainText("kU+202Eim");
      await expect(region).toContainText("KimU+202E Lee");
      await expect(
        page.getByRole("button", { name: "Withdraw the invite of kU+202Eim" }),
      ).toBeVisible();
      expect(await rawOverrides(page), "Invites").toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });
  });
}
