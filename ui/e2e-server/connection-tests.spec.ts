import { expect, test } from "@playwright/test";

import {
  administrator,
  createConnection,
  createEnvironment,
  startWorker,
  uniqueName,
} from "./helpers";

// The tests here are carried out: the file starts a worker of its own on the server's database,
// and stops it when the file is done, so the other files keep a server without one.
let stopWorker: (() => Promise<void>) | null = null;

test.beforeAll(async ({ request }, testInfo) => {
  testInfo.setTimeout(120_000);
  stopWorker = await startWorker(request);
});

test.afterAll(async () => {
  test.setTimeout(60_000);
  await stopWorker?.();
});

test("Test all tests every connection of the environment once and counts how they ended", async ({
  browser,
  request,
}) => {
  test.setTimeout(120_000);
  const environment = uniqueName("tests");
  await createEnvironment(request, environment);
  // Neither reaches anything: a host name that does not resolve, and a root the server lacks.
  const nowhere = uniqueName("nowhere");
  const folder = uniqueName("folder");
  await createConnection(request, environment, {
    name: nowhere,
    kind: "tcp",
    address: { host: "nothing.invalid", port: 5432 },
    settings: {},
  });
  await createConnection(request, environment, {
    name: folder,
    kind: "directory",
    address: { root: "elsewhere", path: "exports" },
    settings: {},
  });
  const page = await administrator(browser, request);
  const asked: string[] = [];
  page.on("request", (sent) => {
    const path = new URL(sent.url()).pathname;
    if (sent.method() === "POST" && path.startsWith(`/api/v1/environments/${environment}/`)) {
      asked.push(path);
    }
  });

  await page.goto("/connections");
  await expect(
    page.getByText("Choose an environment to test all of its connections."),
  ).toBeVisible();
  await page.getByRole("button", { name: /^Environment: / }).click();
  await page.getByRole("option", { name: environment, exact: true }).click();
  const testAll = page.getByRole("button", { name: `Test all in ${environment}` });
  await testAll.click();
  await testAll.click({ force: true });
  await expect(page.getByText(`Testing 2 connections in ${environment}…`)).toBeVisible();
  await expect(
    page.getByText(`Tested 2 connections in ${environment}: 0 OK, 2 not OK.`),
  ).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
  // The second press asked for nothing more: one request per connection at most, or one batch.
  expect(asked.length).toBeLessThanOrEqual(2);
  for (const name of [nowhere, folder]) {
    await expect(
      page.getByRole("row").filter({ has: page.getByRole("link", { name, exact: true }) }),
    ).not.toContainText(/queued|testing…/u);
  }
  await page.context().close();
});
