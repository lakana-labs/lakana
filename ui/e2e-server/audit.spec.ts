import { expect, type Page, test } from "@playwright/test";

import {
  administrator,
  auditEvents,
  createServiceAccount,
  invitedPerson,
  uniqueName,
  violations,
} from "./helpers";

// Settings › Audit log against the real server: the events it records, the filters it applies,
// the read it records in turn (ACTIVITY_VIEWED) and who may open the page.

/**
 * Checks a page with axe in light and in dark.
 *
 * @param page - The page.
 * @param label - What the page is.
 */
async function axeInBothThemes(page: Page, label: string): Promise<void> {
  for (const scheme of ["light", "dark"] as const) {
    // Without motion, so the theme's fade is over before axe reads the colours.
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    expect(await violations(page), `${label}, ${scheme}`).toEqual([]);
  }
}

test("an administrator reads the log, filters it, and the read is recorded", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  // Someone else's event, so that the page shows another person's activity.
  const other = await invitedPerson(browser, request, () => Promise.resolve());
  await other.page.context().close();
  const account = uniqueName("nightly-loader");
  await createServiceAccount(request, account);
  const page = await administrator(browser, request);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto("/settings/audit");
  const region = page.getByRole("region", { name: "Audit events" });
  await expect(region.getByRole("row", { name: new RegExp(other.name) }).first()).toBeVisible();
  await axeInBothThemes(page, "Audit log");

  const event = page.getByRole("combobox", { name: "Event" });
  await event.fill("PRINCIPAL_CREATED");
  await event.press("Enter");
  await event.press("Escape");
  await expect(page).toHaveURL(/event=PRINCIPAL_CREATED/);
  const created = region.getByRole("row").filter({ hasText: "PRINCIPAL_CREATED" });
  await expect(created.first()).toBeVisible();
  await expect(region.getByRole("row").filter({ hasNotText: "PRINCIPAL_CREATED" })).toHaveCount(1);

  const who = page.getByRole("combobox", { name: "Who" });
  await who.fill("admin");
  await who.press("Enter");
  await who.press("Escape");
  await expect(page).toHaveURL(/who=admin/);
  const row = region.getByRole("row").filter({ hasText: "PRINCIPAL_CREATED" }).first();
  await row.getByRole("button", { name: /^Show details/ }).click();
  await expect(region.getByText(account)).toBeVisible();
  await axeInBothThemes(page, "Audit log, filtered, details open");

  // The server recorded the read of other people's events, as for `lakana audit list`.
  const viewed = await auditEvents(request, { event_type: "ACTIVITY_VIEWED" });
  expect(
    viewed.some((each) => each.principal_name === "admin" && each.detail.view === "audit_log"),
  ).toBe(true);
  expect(errors).toEqual([]);
  await page.context().close();
});

test("a person who is not an administrator cannot use the Audit log", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { page } = await invitedPerson(browser, request, () => Promise.resolve());
  const asked: string[] = [];
  page.on("request", (sent) => {
    if (new URL(sent.url()).pathname === "/api/v1/audit-events") asked.push(sent.url());
  });

  await page.goto("/settings/audit");

  await expect(
    page.getByRole("heading", { level: 1, name: "You cannot use Audit log" }),
  ).toBeVisible();
  expect(asked).toEqual([]);
  await page.context().close();
});
