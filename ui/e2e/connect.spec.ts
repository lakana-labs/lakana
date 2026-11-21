import { expect, type Page, test } from "@playwright/test";

import { DEPLOY_KEY, ROUTES, stubConnect } from "./connect";
import { signInAsAdmin, STUBBED, violations } from "./session";

const KEY = "lakana.preferences";

/**
 * Walks through the four steps, running axe after each.
 *
 * @param page - The page, on the Connect a repository page.
 * @returns The violations, by step.
 */
async function walk(page: Page): Promise<Record<string, string[]>> {
  const found: Record<string, string[]> = {};
  await expect(page.getByRole("heading", { level: 1, name: "Connect a repository" })).toBeVisible();
  found["empty"] = await violations(page);
  await page
    .getByRole("textbox", { name: "Repository URL" })
    .fill("https://github.com/acme/analytics");
  await page.getByRole("textbox", { name: "Repository URL" }).blur();
  await page.getByRole("button", { name: "Use a token instead" }).click();
  found["the repository"] = await violations(page);
  await page.getByRole("button", { name: "Fetch the host's keys" }).click();
  await page.getByRole("button", { name: "Show other key types (1)" }).click();
  await page.getByRole("checkbox", { name: "This matches: ssh-ed25519" }).check();
  found["the host's keys"] = await violations(page);
  await page.getByRole("button", { name: "Create the repository and its keys" }).click();
  await expect(page.getByText(DEPLOY_KEY.keys[0]?.public_key ?? "")).toBeVisible();
  found["the keys"] = await violations(page);
  await page.getByRole("button", { name: "Test access in dev" }).click();
  await expect(page.getByRole("alert")).toContainText("refused the key of dev");
  found["a refused test"] = await violations(page);
  return found;
}

for (const theme of ["light", "dark"] as const) {
  test(`Connect a repository passes axe in every step, ${theme}`, async ({ page }) => {
    await signInAsAdmin(page);
    await stubConnect(page);
    await page.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key ?? "", value ?? "");
      },
      [KEY, JSON.stringify({ theme, language: "en" })],
    );
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requests: string[] = [];
    page.on("request", (request) => requests.push(new URL(request.url()).pathname));

    await page.goto("/settings/repositories/new");
    const found = await walk(page);

    expect(found).toEqual({
      empty: [],
      "the repository": [],
      "the host's keys": [],
      "the keys": [],
      "a refused test": [],
    });
    expect(errors).toEqual([]);
    const api = requests.filter((path) => path.startsWith("/api/"));
    expect(api.filter((path) => !STUBBED.includes(path) && !ROUTES.includes(path))).toEqual([]);
  });
}

test("Connect a repository fits 320 px in every step", async ({ page }) => {
  await signInAsAdmin(page);
  await stubConnect(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/settings/repositories/new");

  const found = await walk(page);

  expect(Object.values(found).flat()).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
});
