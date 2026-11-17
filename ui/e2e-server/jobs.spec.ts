import { expect, type Page, test } from "@playwright/test";

import {
  administrator,
  createEnvironment,
  type DeployedJob,
  freshPage,
  grantOn,
  invitedPerson,
  recordDeployedJob,
  signInLocally,
  skipTheTour,
  skipTheWelcome,
  startWorker,
  uniqueName,
  violations,
} from "./helpers";

type Request = Parameters<typeof createEnvironment>[0];

/** The job each test deploys, in an environment of its own. */
const JOB: DeployedJob = {
  name: "daily_sales",
  table: "gold.daily_sales",
  cron: "0 2 * * *",
  secret: "alerts-webhook",
};

/**
 * A new environment with a repository deployed in it that declares {@link JOB}, so that each
 * test reads its own job and runs on the run's one shared server.
 *
 * @param request - The request context.
 * @returns The environment's name and the job page's address.
 */
async function deployed(request: Request): Promise<{ environment: string; address: string }> {
  const environment = uniqueName("job");
  const repository = uniqueName("analytics");
  await createEnvironment(request, environment);
  await recordDeployedJob(request, environment, repository, JOB);
  return { environment, address: `/jobs/${environment}/job/${repository}/${JOB.name}` };
}

/**
 * Opens the job's page and waits for its facts.
 *
 * @param page - The page.
 * @param address - The job page's address.
 */
async function openJob(page: Page, address: string): Promise<void> {
  await page.goto(address);
  await expect(page.getByRole("heading", { level: 1, name: JOB.name })).toBeVisible();
  await expect(page.getByText(/^Notifies on failure/)).toBeVisible();
}

/**
 * The run's facts on the job page.
 *
 * @param page - The page.
 * @returns The facts.
 */
function runFacts(page: Page): ReturnType<Page["locator"]> {
  return page.locator(".jobs-run .jobs-facts-list");
}

test("an administrator sees the job's facts, starts a run that stays queued with no worker, and cancels it", async ({
  browser,
  request,
}) => {
  test.setTimeout(180_000);
  const { address } = await deployed(request);
  const page = await administrator(browser, request);

  await openJob(page, address);
  await expect(
    page.getByText(
      `Notifies on failure, recovery, column changes and health signals · Slack · via secret ${JOB.secret}`,
    ),
  ).toBeVisible();
  await expect(page.getByText(/^daily 02:00 Europe\/Berlin · next run /)).toBeVisible();
  await expect(page.getByText("No run of this job in this period.")).toBeVisible();

  // The server runs no worker: the run stays queued.
  const runNow = page.getByRole("button", { name: "Run now" });
  await runNow.click();
  await expect(
    page.getByRole("status").filter({ hasText: `Started a run of ${JOB.name}.` }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\?run=[0-9a-f-]+$/);
  await expect(runFacts(page)).toContainText("Queued");
  await expect(runNow).toBeFocused();

  await page.getByRole("button", { name: "Cancel run" }).click();
  const ask = page.getByRole("dialog", { name: `Cancel the run of ${JOB.name}?` });
  await expect(ask).toContainText("Finished tasks stay done.");
  await ask.getByRole("button", { name: "Cancel run" }).click();
  await expect(ask).toBeHidden();
  // The server only records the cancel; the scheduler of a worker carries it out.
  await expect(page.getByRole("status").filter({ hasText: "Cancelling…" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  const stopWorker = await startWorker(request);
  try {
    await expect(runFacts(page)).toContainText("Cancelled", { timeout: 60_000 });
    await expect(page.getByRole("status").filter({ hasText: "Cancelling…" })).toHaveCount(0);
  } finally {
    await stopWorker();
  }
  await page.context().close();
});

test("Pause schedule and Resume schedule change the job's facts", async ({ browser, request }) => {
  const { address } = await deployed(request);
  const page = await administrator(browser, request);

  await openJob(page, address);
  await page.getByRole("button", { name: "Pause schedule" }).click();
  const ask = page.getByRole("dialog", { name: `Pause the schedule of ${JOB.name}?` });
  await expect(ask).toContainText("A run that is running finishes.");
  await ask.getByRole("button", { name: "Pause schedule" }).click();
  await expect(page.getByText("daily 02:00 Europe/Berlin · paused")).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: `Paused the schedule of ${JOB.name}.` }),
  ).toBeVisible();

  // A reload reads what the server keeps.
  await openJob(page, address);
  await expect(page.getByText("daily 02:00 Europe/Berlin · paused")).toBeVisible();
  await page.getByRole("button", { name: "Resume schedule" }).click();
  await expect(page.getByText(/^daily 02:00 Europe\/Berlin · next run /)).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause schedule" })).toBeFocused();
  await page.context().close();
});

test("a reader of the environment sees the job with no control and no secret's name", async ({
  browser,
  request,
}) => {
  const { environment, address } = await deployed(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    grantOn(request, name, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(page);

  await openJob(page, address);
  await expect(
    page.getByText("Notifies on failure, recovery, column changes and health signals · Slack", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(JOB.secret)).toHaveCount(0);
  await expect(page.getByText(/^daily 02:00 Europe\/Berlin · next run /)).toBeVisible();
  for (const control of ["Run now", "Pause schedule", "Resume schedule", "Cancel run"]) {
    await expect(page.getByRole("button", { name: control })).toHaveCount(0);
  }
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test(`axe finds nothing on the real job page (${scheme}, ${String(width)} px)`, async ({
      browser,
      browserName,
      request,
    }) => {
      test.skip(browserName !== "chromium", "axe runs in Chromium only");
      const { address } = await deployed(request);
      await skipTheTour(request);
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);

      await openJob(page, address);
      expect(await violations(page), "the job page").toEqual([]);
      await page.getByRole("button", { name: "Run now" }).click();
      await expect(runFacts(page)).toContainText("Queued");
      expect(await violations(page), "the job page with its queued run").toEqual([]);
      const run = new URL(page.url()).searchParams.get("run") ?? "";
      await page.goto(`${address.split("/job/")[0] ?? ""}/runs/${run}`);
      await expect(page.getByRole("heading", { level: 1, name: /^Run of / })).toBeVisible();
      expect(await violations(page), "the run's page").toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        ),
      ).toBeLessThanOrEqual(0);
      await page.context().close();
    });
  }
}
