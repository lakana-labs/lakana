import { expect, type Page, test } from "@playwright/test";

import { signInAsAdmin, STUBBED, violations } from "./session";

const KEY = "lakana.preferences";
const AT = "2026-11-11T08:00:00Z";
const HOST_KEYS = {
  id: "p-1",
  kind: "host_keys",
  state: "done",
  outcome: "ok",
  message: "Found 2 host keys of github.com.",
  host_keys: [
    {
      type: "ssh-ed25519",
      fingerprint: "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
      line: "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
    },
    {
      type: "ssh-rsa",
      fingerprint: "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
      line: "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt",
    },
  ],
  created_at: AT,
  finished_at: AT,
};
const ACCESS = {
  ...HOST_KEYS,
  id: "p-2",
  kind: "access",
  outcome: "auth_refused",
  message:
    "The git host refused the key of dev: add it as a read-only deploy key of the repository, then test again.",
  host_keys: null,
};
const DEPLOY_KEY = {
  secret: "analytics-deploy-key",
  keys: ["dev", "prod"].map((environment, index) => ({
    environment,
    public_key: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL${environment}Q0r8VHx2mPz7Ykq lakana analytics ${environment}`,
    fingerprint: `SHA256:${environment}7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe`,
    made: index === 0,
  })),
  skipped: ["staging"],
  replaced: [],
  notes: [],
};
const ROUTES = [
  "/api/v1/repository-probes",
  "/api/v1/repositories",
  "/api/v1/repositories/analytics/deploy-key",
];

/**
 * Answers the routes of the page like the server: the host's keys at once, the repository and
 * its keys, and an access test the git host refuses.
 *
 * @param page - The page.
 */
async function stubConnect(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/v1/repository-probes",
    async (route) => {
      const body = route.request().postDataJSON() as { kind: string };
      await route.fulfill({ status: 202, json: body.kind === "access" ? ACCESS : HOST_KEYS });
    },
  );
  await page.route(
    (url) => url.pathname === "/api/v1/repositories",
    (route) => route.fulfill({ status: 201, json: { name: "analytics" } }),
  );
  await page.route("**/api/v1/repositories/analytics/deploy-key", (route) =>
    route.fulfill({ status: 201, json: DEPLOY_KEY }),
  );
}

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
  for (const density of ["roomy", "compact"] as const) {
    test(`Connect a repository passes axe in every step, ${theme}, ${density}`, async ({
      page,
    }) => {
      await signInAsAdmin(page);
      await stubConnect(page);
      await page.addInitScript(
        ([key, value]) => {
          window.localStorage.setItem(key ?? "", value ?? "");
        },
        [KEY, JSON.stringify({ theme, density, language: "en" })],
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
