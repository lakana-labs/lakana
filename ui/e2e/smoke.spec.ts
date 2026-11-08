import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

test("the page has no axe violations", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Lakana" })).toBeVisible();

  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();

  expect(results.violations).toEqual([]);
});

test("axe catches a missing alt text", async ({ page }) => {
  // The positive control: the check above would pass vacuously if axe found nothing at all.
  await page.setContent('<img src="x.png">');

  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();

  expect(results.violations.map((violation) => violation.id)).toContain("image-alt");
});

test("the page makes no request to the server's API", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));

  await page.goto("/");
  await page.waitForLoadState("networkidle");

  expect(requests.filter((url) => url.includes("/api/") || url.includes("/v1/"))).toEqual([]);
});

test("the page logs no console error", async ({ page }) => {
  // A violation of the content security policy shows up here.
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto("/");
  await page.waitForLoadState("networkidle");

  expect(errors).toEqual([]);
});
