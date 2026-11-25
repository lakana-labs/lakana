import { expect, type Page, test } from "@playwright/test";

import {
  administrator,
  cancelRuns,
  createEnvironment,
  type DeployedJob,
  freshPage,
  grantOn,
  invitedPerson,
  recordDeployedJob,
  recordDeployHistory,
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

/** The schedule of {@link JOB} once the bootstrap administrator paused it: since when, by whom. */
const PAUSED_BY_ADMIN = /^daily 02:00 Europe\/Berlin · paused since .+ by admin$/;

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
  await expect(page.getByText(PAUSED_BY_ADMIN)).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: `Paused the schedule of ${JOB.name}.` }),
  ).toBeVisible();

  // A reload reads what the server keeps.
  await openJob(page, address);
  await expect(page.getByText(PAUSED_BY_ADMIN)).toBeVisible();
  await page.getByRole("button", { name: "Resume schedule" }).click();
  await expect(page.getByText(/^daily 02:00 Europe\/Berlin · next run /)).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause schedule" })).toBeFocused();
  await page.context().close();
});

test("a reader of the environment sees the job with no control and no secret's name", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
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
      const { environment, address } = await deployed(request);
      await skipTheTour(request);
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);

      // The run this test starts is cancelled at the end: no worker runs here, and a queued
      // run would be picked up by the worker of a later file.
      try {
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
      } finally {
        await cancelRuns(request, environment);
      }
      await page.context().close();
    });
  }
}

/**
 * Two new environments, each with a repository deployed in it that declares {@link JOB}; the two
 * repositories' names share a tag that no other test's do, so a search for it finds only them.
 *
 * @param request - The request context.
 * @returns The tag and the two environments.
 */
async function deployedTwice(
  request: Request,
): Promise<{ tag: string; first: string; second: string }> {
  const tag = uniqueName("ledger");
  const first = uniqueName("jobs");
  const second = uniqueName("jobs");
  for (const [environment, repository] of [
    [first, `${tag}-a`],
    [second, `${tag}-b`],
  ] as const) {
    await createEnvironment(request, environment);
    await recordDeployedJob(request, environment, repository, JOB);
  }
  return { tag, first, second };
}

test("an administrator lists the job in two environments, filters to one across a reload, runs it and cancels it", async ({
  browser,
  request,
}) => {
  test.setTimeout(180_000);
  const { tag, first, second } = await deployedTwice(request);
  const page = await administrator(browser, request);
  const all = page.getByRole("region", { name: "Every job listed" });
  const running = page.getByRole("region", { name: "Runs running now" });
  try {
    await page.goto(`/jobs?q=${tag}`);
    await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
    await expect(all.getByRole("link", { name: JOB.name })).toHaveCount(2);
    await expect(all).toContainText(first);
    await expect(all).toContainText(second);

    await page.getByRole("button", { name: "Environment: All environments" }).click();
    await page.getByRole("option", { name: first, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`environment=${first}`));
    await expect(all.getByRole("link", { name: JOB.name })).toHaveCount(1);

    // The choice is remembered: the list opens on it without the address naming it.
    await page.goto("/jobs");
    await expect(page.getByRole("button", { name: `Environment: ${first}` })).toBeVisible();
    await expect(all.getByRole("link", { name: JOB.name })).toHaveCount(1);
    await expect(all).not.toContainText(second);

    // The server runs no worker: the run stays queued under Running now.
    await all.getByRole("button", { name: `Run ${JOB.name}` }).click();
    await expect(page.getByText(`Started a run of ${JOB.name}.`)).toBeVisible();
    await expect(running.getByText("Queued", { exact: true })).toBeVisible();
    await running.getByRole("button", { name: `Cancel the run of ${JOB.name}` }).click();
    const ask = page.getByRole("dialog", { name: `Cancel the run of ${JOB.name}?` });
    await ask.getByRole("button", { name: "Cancel run" }).click();
    await expect(ask).toBeHidden();
    await expect(running.getByText("Cancelling…")).toBeVisible();
    // A worker carries the cancel out; the next read of the list no longer holds the run.
    const stopWorker = await startWorker(request);
    try {
      await expect(page.getByText("Nothing is running.")).toBeVisible({ timeout: 60_000 });
    } finally {
      await stopWorker();
    }
  } finally {
    await cancelRuns(request, first);
    await cancelRuns(request, second);
  }
  await page.context().close();
});

test("a reader of one environment sees only its jobs on the list, with no control", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { tag, first, second } = await deployedTwice(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    grantOn(request, name, "READ_ENVIRONMENT", first),
  );
  await skipTheWelcome(page);

  await page.goto(`/jobs?q=${tag}`);
  const all = page.getByRole("region", { name: "Every job listed" });
  await expect(all.getByRole("link", { name: JOB.name })).toHaveCount(1);
  await expect(all).toContainText(first);
  await expect(all).not.toContainText(second);
  // The column headers sort the list; no row holds a control.
  await expect(all.locator("tbody").getByRole("button")).toHaveCount(0);
  await page.getByRole("button", { name: "Environment: All environments" }).click();
  await expect(page.getByRole("option")).toHaveText(["All environments", first]);
  await page.keyboard.press("Escape");
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test(`axe finds nothing on the real Jobs list (${scheme}, ${String(width)} px)`, async ({
      browser,
      browserName,
      request,
    }) => {
      test.skip(browserName !== "chromium", "axe runs in Chromium only");
      const { environment } = await deployed(request);
      await skipTheTour(request);
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);

      await page.goto(`/jobs?environment=${environment}`);
      await expect(
        page
          .getByRole("region", { name: "Every job listed" })
          .getByRole("link", { name: JOB.name }),
      ).toBeVisible();
      await expect(page.getByText("Nothing is running.")).toBeVisible();
      expect(await violations(page), "the Jobs list").toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        ),
      ).toBeLessThanOrEqual(0);
      await page.context().close();
    });
  }
}

/** The job only the older version declares. */
const OLDER_JOB: DeployedJob = {
  name: "weekly_report",
  table: "gold.weekly_report",
  cron: "0 6 * * 1",
  secret: "alerts-webhook",
};

/** The older version's commit, and the current one's. */
const OLDER_COMMIT = "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234";
const CURRENT_COMMIT = "9c1e5b3a2d4f6e8a0b1c2d3e4f5a6b7c8d9e0f12";

/**
 * A new environment with a repository deployed twice: an older version that declares
 * {@link JOB} and {@link OLDER_JOB}, then the current one, which declares {@link JOB} alone.
 *
 * @param request - The request context.
 * @returns The environment's and the repository's names.
 */
async function deployedTwiceOver(
  request: Request,
): Promise<{ environment: string; repository: string }> {
  const environment = uniqueName("deploys");
  const repository = uniqueName("analytics");
  await createEnvironment(request, environment);
  await recordDeployHistory(request, environment, repository, [
    { commit: OLDER_COMMIT, jobs: [JOB, OLDER_JOB] },
    { commit: CURRENT_COMMIT, jobs: [JOB] },
  ]);
  return { environment, repository };
}

/**
 * The older deploy's Roll back to this…, by its accessible name.
 *
 * @param page - The page.
 * @param repository - The repository.
 * @returns The button.
 */
function rollBackButton(page: Page, repository: string): ReturnType<Page["getByRole"]> {
  return page.getByRole("button", { name: `Roll back to this… (${repository} at 4f9d2a7)` });
}

test("a reader of the environment sees its deploys with no rollback", async ({
  browser,
  browserName,
  request,
}) => {
  test.skip(browserName !== "chromium", "signs in with a virtual authenticator");
  const { environment, repository } = await deployedTwiceOver(request);
  const { page } = await invitedPerson(browser, request, (name) =>
    grantOn(request, name, "READ_ENVIRONMENT", environment),
  );
  await skipTheWelcome(page);

  await page.goto(`/jobs/deploys?environment=${environment}`);
  const region = page.getByRole("region", { name: `Deploys of ${environment}` });
  await expect(region.getByRole("row")).toHaveCount(3);
  await expect(region.getByRole("row").nth(1)).toContainText("9c1e5b3 · current");
  // The column headers sort the list; no row holds a control.
  await expect(region.locator("tbody").getByRole("button")).toHaveCount(0);
  await expect(rollBackButton(page, repository)).toHaveCount(0);
  await page.context().close();
});

test("the Runs tab lists a run queued through Run now", async ({ browser, request }) => {
  const { environment, address } = await deployed(request);
  const page = await administrator(browser, request);
  try {
    await openJob(page, address);
    await page.getByRole("button", { name: "Run now" }).click();
    await expect(runFacts(page)).toContainText("Queued");

    await page.goto(`/jobs/runs?environment=${environment}`);
    const region = page.getByRole("region", { name: "Runs listed" });
    await expect(region.getByRole("row")).toHaveCount(2);
    await expect(region.getByRole("row").nth(1)).toContainText("Queued");
    await expect(region.getByRole("link", { name: JOB.name })).toBeVisible();
    await expect(region.getByRole("row").nth(1)).toContainText("By hand");
  } finally {
    await cancelRuns(request, environment);
  }
  await page.context().close();
});

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 320]) {
    test(`axe finds nothing on the real Runs and Deploys tabs (${scheme}, ${String(width)} px)`, async ({
      browser,
      browserName,
      request,
    }) => {
      test.skip(browserName !== "chromium", "axe runs in Chromium only");
      const { environment, repository } = await deployedTwiceOver(request);
      await skipTheTour(request);
      const page = await freshPage(browser);
      await page.setViewportSize({ width, height: width === 320 ? 640 : 800 });
      await page.emulateMedia({ colorScheme: scheme });
      await signInLocally(page, request);
      const overflow = (): Promise<number> =>
        page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );

      await page.goto(`/jobs/runs?environment=${environment}`);
      await expect(page.getByText("No runs match these filters.")).toBeVisible();
      expect(await violations(page), "the Runs tab").toEqual([]);
      expect(await overflow()).toBeLessThanOrEqual(0);

      await page.goto(`/jobs/deploys?environment=${environment}`);
      await expect(rollBackButton(page, repository)).toBeVisible();
      expect(await violations(page), "the Deploys tab").toEqual([]);
      expect(await overflow()).toBeLessThanOrEqual(0);

      await rollBackButton(page, repository).click();
      const dialog = page.getByRole("dialog", {
        name: `Roll back ${repository} in ${environment}?`,
      });
      await expect(dialog.getByRole("button", { name: "Roll back" })).toBeVisible();
      expect(await violations(page), "the rollback dialog").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await page.context().close();
    });
  }
}

// Last in the file: the rollback it queues stays queued while no worker runs here.
test("an administrator rolls back to the older version, naming the jobs it changes", async ({
  browser,
  request,
}) => {
  const { environment, repository } = await deployedTwiceOver(request);
  const page = await administrator(browser, request);

  await page.goto(`/jobs/deploys?environment=${environment}`);
  const region = page.getByRole("region", { name: `Deploys of ${environment}` });
  await expect(region.getByRole("row")).toHaveCount(3);
  await expect(region.getByRole("row").nth(1)).toContainText("9c1e5b3 · current");
  await expect(region.locator("tbody").getByRole("button")).toHaveCount(1);
  const button = rollBackButton(page, repository);
  await expect(region.getByRole("row").nth(2)).toContainText("4f9d2a7");
  await expect(region.getByRole("row").nth(2)).toContainText("an account since deleted");

  await button.click();
  const dialog = page.getByRole("dialog", { name: `Roll back ${repository} in ${environment}?` });
  await expect(dialog.getByRole("list", { name: "Jobs the rollback changes" })).toBeVisible();
  await expect(dialog.getByRole("listitem")).toHaveText([
    JOB.name,
    `${OLDER_JOB.name}: back with the rollback`,
  ]);
  await dialog.getByRole("button", { name: "Roll back" }).click();
  await expect(dialog).toBeHidden();
  await expect(button).toBeFocused();
  // The server runs no worker: the rollback stays queued, and the note says it is rolling back.
  await expect(
    page.getByText(`Rolling back ${repository} in ${environment} to 4f9d2a7…`),
  ).toBeVisible();
  await page.context().close();
});
