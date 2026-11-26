import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  AUDIT_EVENTS_LONG_NAME,
  AUDIT_STUBBED,
  LONG_PRINCIPAL,
  SQL_SHA256,
  stubAudit,
} from "./audit";
import { apiPaths } from "./hidden";
import { ORIGIN } from "./manage";
import { consoleErrors, signInAsAdmin, STUBBED, tabTo, violations } from "./session";

// Settings › Audit log against stubbed routes, in both themes: by keyboard, axe, and at 320 px.

const KEY = "lakana.preferences";

/**
 * Requires that every API request went to a stubbed path.
 *
 * @param paths - The paths sent.
 */
function expectOnlyStubbed(paths: readonly string[]): void {
  expect(paths.filter((path) => ![...STUBBED, ...AUDIT_STUBBED].includes(path))).toEqual([]);
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
 * The audit reads sent, each as its query.
 *
 * @param page - The page.
 * @returns The list, filled as reads go out.
 */
function auditReads(page: Page): URLSearchParams[] {
  const reads: URLSearchParams[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/audit-events") reads.push(url.searchParams);
  });
  return reads;
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`Audit log, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await signInAsAdmin(page);
      await stubAudit(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("by keyboard", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      const reads = auditReads(page);
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto(`${ORIGIN}/settings/audit`);
      const region = page.getByRole("region", { name: "Audit events" });
      await expect(region.getByText("QUERY_COMPLETED")).toBeVisible();
      await expect(page.getByText("5 events")).toBeVisible();
      expect(await violations(page), "Audit log").toEqual([]);
      expect(await pageScrollsSideways(page), "no sideways page at 1280 px").toBe(false);
      expect(await scrollsSideways(region), "no sideways list at 1280 px").toBe(false);

      // The details open by keyboard, in the row's last cell.
      const show = region.getByRole("button", { name: /^Show details of QUERY_COMPLETED/ });
      await tabTo(page, show);
      await page.keyboard.press("Enter");
      const hide = region.getByRole("button", { name: /^Hide details of QUERY_COMPLETED/ });
      await expect(hide).toBeFocused();
      await expect(hide).toHaveAttribute("aria-expanded", "true");
      await expect(region).toContainText(`Hidden: SHA-256 ${SQL_SHA256.slice(0, 12)}…`);
      expect(await violations(page), "details open").toEqual([]);
      await page.keyboard.press("Space");
      await expect(
        region.getByRole("button", { name: /^Show details of QUERY_COMPLETED/ }),
      ).toBeFocused();

      // When sorts on the server, the oldest first.
      const when = region.getByRole("button", { name: "When" });
      await tabTo(page, when);
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/settings\/audit\?sort=occurred&order=asc$/);
      await expect.poll(() => reads.at(-1)?.get("order")).toBe("asc");
      await expect(region.locator("tbody tr").first()).toContainText("WIDGET_FROBNICATED");
      await expect(region.getByRole("button", { name: "When" })).toBeFocused();

      // A typed event applies on Enter; one that does not exist is refused beside the field.
      const event = page.getByRole("combobox", { name: "Event" });
      await tabTo(page, event);
      await page.keyboard.type("WIDGET");
      await page.keyboard.press("Escape");
      await page.keyboard.press("Enter");
      await expect(
        page.getByText("No event type named “WIDGET”. Pick one of the suggestions."),
      ).toBeVisible();
      await expect(event).toBeFocused();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("table_write");
      await page.keyboard.press("Escape");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/event=TABLE_WRITE/);
      await expect(region.locator("tbody tr")).toHaveCount(1);
      await expect(event).toBeFocused();
      expect(await violations(page), "filtered").toEqual([]);

      // A filter that matches nothing offers Clear filters.
      const who = page.getByRole("combobox", { name: "Who" });
      await tabTo(page, who);
      await page.keyboard.type("maria");
      // Once the suggestions have arrived, Escape closes them and Enter applies the name typed.
      await expect(page.getByRole("option").first()).toBeAttached();
      await page.keyboard.press("Escape");
      await page.keyboard.press("Enter");
      await expect(page.getByRole("listbox")).toHaveCount(0);
      await expect(page).toHaveURL(/who=maria/);
      await expect(page.getByText("No event matches these filters.")).toBeVisible();
      const clear = page.getByRole("button", { name: "Clear filters" });
      await tabTo(page, clear);
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/settings\/audit\?sort=occurred&order=asc$/);
      await expect(page.getByText("5 events")).toBeVisible();
      await expect(who).toHaveValue("");

      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });

    test("320 px", async ({ page }) => {
      const errors = consoleErrors(page);
      const paths = apiPaths(page);
      await stubAudit(page, AUDIT_EVENTS_LONG_NAME);
      await page.setViewportSize({ width: 320, height: 800 });
      await page.goto(`${ORIGIN}/settings/audit`);
      const region = page.getByRole("region", { name: "Audit events" });
      await expect(region.getByText(LONG_PRINCIPAL)).toBeAttached();
      expect(await pageScrollsSideways(page), "the page").toBe(false);
      expect(await scrollsSideways(region), "the list scrolls in its region").toBe(true);
      await region.getByRole("button", { name: /^Show details of QUERY_COMPLETED/ }).click();
      expect(await pageScrollsSideways(page), "the page, details open").toBe(false);
      expect(await violations(page), "Audit log at 320 px").toEqual([]);
      expectOnlyStubbed(paths);
      expect(errors).toEqual([]);
    });
  });
}
