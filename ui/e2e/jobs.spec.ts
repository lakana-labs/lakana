import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  BLOCKED,
  ENV,
  FAILED_CHECK,
  FAILED_ID,
  JOB,
  JOBS_STUBBED,
  LOG_PIECES,
  MISSING_ID,
  REPOSITORY,
  RESUMED_ID,
  ROWS_QUERY_ID,
  RUNNING_ID,
  SECRET,
  STARTED_ID,
  stubJobs,
  TIMED_OUT,
  TIMEOUT_CAUSE,
} from "./jobs";
import { consoleErrors, violations } from "./session";

const KEY = "lakana.preferences";

/** The job's page. */
const JOB_PAGE = `/jobs/${ENV}/job/${REPOSITORY}/${JOB}`;

/**
 * A run's page.
 *
 * @param id - The run's id.
 * @returns The address.
 */
function runPage(id: string): string {
  return `/jobs/${ENV}/runs/${id}`;
}

/**
 * A task's page.
 *
 * @param id - The run's id.
 * @param asset - The task.
 * @returns The address.
 */
function taskPage(id: string, asset: string): string {
  return `${runPage(id)}/tasks/${asset}`;
}

/**
 * Opens the job's page and waits for its facts and its runs.
 *
 * @param page - The page.
 * @param query - What follows the address, such as `?run=…`.
 */
async function openJob(page: Page, query = ""): Promise<void> {
  await page.goto(`${JOB_PAGE}${query}`);
  await expect(page.getByRole("heading", { level: 1, name: JOB })).toBeVisible();
  await expect(page.getByText(`via secret ${SECRET}`)).toBeVisible();
  await expect(page.getByRole("region", { name: `Runs of ${JOB}` })).toBeVisible();
}

/**
 * Opens a run's page and waits for its tasks.
 *
 * @param page - The page.
 * @param id - The run's id.
 */
async function openRun(page: Page, id: string): Promise<void> {
  await page.goto(runPage(id));
  await expect(page.getByRole("heading", { level: 1, name: /^Run of / })).toBeVisible();
  await expect(page.getByRole("region", { name: "Tasks of the run" })).toBeVisible();
}

/**
 * Opens the timed-out task's page and waits until its log is complete.
 *
 * @param page - The page.
 */
async function openTask(page: Page): Promise<void> {
  await page.goto(taskPage(FAILED_ID, TIMED_OUT));
  await expect(page.getByRole("heading", { level: 1, name: TIMED_OUT })).toBeVisible();
  await expect(page.getByText("The log is complete.")).toBeVisible({ timeout: 10_000 });
}

/**
 * The second line of a task of the run: why it failed and the controls beside it.
 *
 * @param page - The page.
 * @param asset - The task.
 * @returns The line.
 */
function whyLine(page: Page, asset: string): Locator {
  return page
    .locator("tr", { has: page.getByRole("link", { name: asset, exact: true }) })
    .locator("xpath=following-sibling::tr[1]");
}

/**
 * Moves the focus with Tab until it is on an element.
 *
 * @param target - The element.
 */
async function tabOnto(target: Locator): Promise<void> {
  const page = target.page();
  for (let step = 0; step < 80; step += 1) {
    if (await target.evaluate((node) => node === document.activeElement)) {
      return;
    }
    await page.keyboard.press("Tab");
  }
  throw new Error("Tab never reached the element");
}

/**
 * Whether the focus is somewhere on the page rather than on `<body>`.
 *
 * @param page - The page.
 * @returns True when an element other than `<body>` holds it.
 */
function focusOnPage(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.activeElement !== null && document.activeElement !== document.body,
  );
}

/**
 * How far the page is wider than the window.
 *
 * @param page - The page.
 * @returns The overflow in pixels; zero or less when nothing scrolls sideways.
 */
function sidewaysOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/**
 * Asserts that a region scrolls sideways inside itself and takes a tab stop.
 *
 * @param region - The region.
 */
async function scrollsInItself(region: Locator): Promise<void> {
  await expect(region).toHaveAttribute("tabindex", "0");
  expect(
    await region.evaluate((node) => node.scrollWidth > node.clientWidth),
    "the table is wider than its region",
  ).toBe(true);
}

/**
 * Sets the page's visibility as a background tab or a tab brought back would, and tells it.
 *
 * @param page - The page.
 * @param state - `hidden` or `visible`.
 */
async function showAs(page: Page, state: "hidden" | "visible"): Promise<void> {
  await page.evaluate((value) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}

for (const theme of ["light", "dark"] as const) {
  for (const density of ["roomy", "compact"] as const) {
    test.describe(`the job, run and task pages, ${theme}, ${density}`, () => {
      let sent: string[] = [];

      test.beforeEach(async ({ page }) => {
        sent = await stubJobs(page);
        await page.addInitScript(
          ([key, value]) => {
            if (window.localStorage.getItem(key ?? "") === null) {
              window.localStorage.setItem(key ?? "", value ?? "");
            }
          },
          [KEY, JSON.stringify({ theme, density, language: "en" })],
        );
      });

      test("axe finds nothing on the job page, a run, a task with its log, Show source and the Cancel and Pause dialogs", async ({
        page,
      }) => {
        await openJob(page);
        await expect(page.getByRole("button", { name: "Cancel run" })).toBeVisible();
        expect(await violations(page), "the job page").toEqual([]);

        await page.getByRole("button", { name: "Pause schedule" }).click();
        const pause = page.getByRole("dialog", { name: `Pause the schedule of ${JOB}?` });
        await expect(pause).toBeVisible();
        expect(await violations(page), "the Pause dialog").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(pause).toBeHidden();

        await page.getByRole("button", { name: "Cancel run" }).click();
        const cancel = page.getByRole("dialog", { name: `Cancel the run of ${JOB}?` });
        await expect(cancel).toBeVisible();
        expect(await violations(page), "the Cancel dialog").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(cancel).toBeHidden();

        await openRun(page, FAILED_ID);
        await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
        expect(await violations(page), "a failed run").toEqual([]);
        await whyLine(page, BLOCKED).getByRole("button", { name: "Show source" }).click();
        const source = page.getByRole("dialog", { name: "Where fact_sales is defined" });
        await expect(source.getByText("models/gold/fact_sales.sql")).toBeVisible();
        expect(await violations(page), "the Show source dialog").toEqual([]);
        await page.keyboard.press("Escape");
        await expect(source).toBeHidden();

        await openTask(page);
        await expect(page.getByRole("region", { name: `Attempts of ${TIMED_OUT}` })).toBeVisible();
        expect(await violations(page), "a task with its log").toEqual([]);

        await page.goto(runPage(MISSING_ID));
        await expect(page.getByRole("alert")).toContainText(`Run ${MISSING_ID} does not exist`);
        expect(await violations(page), "a run that is not there").toEqual([]);
      });

      test("the breadcrumbs lead from a task back to its run, its job and Jobs", async ({
        page,
      }) => {
        await openJob(page);
        const trail = page.getByRole("navigation", { name: "Breadcrumb" });
        await expect(trail.getByRole("link")).toHaveText(["Jobs", ENV]);

        await openTask(page);
        await expect(trail.getByRole("link")).toHaveText(["Jobs", ENV, JOB, /^Run of /]);
        await expect(trail).toContainText(TIMED_OUT);
        await trail.getByRole("link", { name: /^Run of / }).click();
        await expect(page).toHaveURL(new RegExp(`${runPage(FAILED_ID)}$`));
        await expect(trail.getByRole("link")).toHaveText(["Jobs", ENV, JOB]);
        await trail.getByRole("link", { name: JOB }).click();
        await expect(page).toHaveURL(new RegExp(`${JOB_PAGE}$`));
        await expect(page.getByRole("heading", { level: 1, name: JOB })).toBeVisible();
        await trail.getByRole("link", { name: "Jobs" }).click();
        await expect(page).toHaveURL(/\/jobs$/);
      });

      test("Run now, Pause and Resume schedule, Cancel, Resume, Log, Show source and Show rows work by keyboard alone", async ({
        page,
      }) => {
        // Run now: the new run is selected, the note says so, the focus stays on the button.
        await openJob(page);
        const runNow = page.getByRole("button", { name: "Run now" });
        await tabOnto(runNow);
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(new RegExp(`\\?run=${STARTED_ID}$`));
        await expect(
          page.getByRole("status").filter({ hasText: `Started a run of ${JOB}.` }),
        ).toBeVisible();
        await expect(runNow).toBeFocused();

        // Pause asks first; Escape returns the focus to the button.
        const pause = page.getByRole("button", { name: "Pause schedule" });
        await tabOnto(pause);
        await page.keyboard.press("Enter");
        const ask = page.getByRole("dialog", { name: `Pause the schedule of ${JOB}?` });
        await expect(ask).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(ask).toBeHidden();
        await expect(pause).toBeFocused();
        await page.keyboard.press("Enter");
        await expect(ask).toBeVisible();
        await tabOnto(ask.getByRole("button", { name: "Pause schedule" }));
        await page.keyboard.press("Enter");
        await expect(ask).toBeHidden();
        const resume = page.getByRole("button", { name: "Resume schedule" });
        await expect(resume).toBeFocused();
        await expect(page.getByText(/· paused$/)).toBeVisible();
        await page.keyboard.press("Enter");
        await expect(pause).toBeFocused();
        await expect(page.getByText(/next run/)).toBeVisible();
        expect(sent).toEqual([
          `POST /api/v1/environments/${ENV}/jobs/${JOB}/runs`,
          `POST /api/v1/environments/${ENV}/jobs/${JOB}/pause`,
          `POST /api/v1/environments/${ENV}/jobs/${JOB}/resume`,
        ]);

        // Cancel asks first; Escape returns the focus; once cancelled the focus is on the run.
        await openRun(page, RUNNING_ID);
        const cancel = page.getByRole("button", { name: "Cancel run" });
        await tabOnto(cancel);
        await page.keyboard.press("Enter");
        const question = page.getByRole("dialog", { name: `Cancel the run of ${JOB}?` });
        await expect(question).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(cancel).toBeFocused();
        await page.keyboard.press("Enter");
        await tabOnto(question.getByRole("button", { name: "Cancel run" }));
        await page.keyboard.press("Enter");
        await expect(question).toBeHidden();
        await expect(page.locator(".jobs-facts-list")).toContainText("Cancelled", {
          timeout: 10_000,
        });
        expect(await focusOnPage(page), "the focus is never on <body>").toBe(true);

        // Resume opens the new run, its heading holding the focus.
        await openRun(page, FAILED_ID);
        await tabOnto(page.getByRole("button", { name: "Resume" }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(new RegExp(`${runPage(RESUMED_ID)}$`));
        await expect(page.getByRole("heading", { level: 1 })).toBeFocused();

        // Show source opens the file; Escape returns the focus to the button.
        await openRun(page, FAILED_ID);
        const source = whyLine(page, BLOCKED).getByRole("button", { name: "Show source" });
        await tabOnto(source);
        await page.keyboard.press("Enter");
        const dialog = page.getByRole("dialog", { name: "Where fact_sales is defined" });
        await expect(dialog.getByText("models/gold/fact_sales.sql")).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
        await expect(source).toBeFocused();

        // Log opens the task's page.
        await tabOnto(whyLine(page, TIMED_OUT).getByRole("link", { name: "Log" }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(new RegExp(`${taskPage(FAILED_ID, TIMED_OUT)}$`));
        await expect(page.getByText("The log is complete.")).toBeVisible({ timeout: 10_000 });
        expect(await focusOnPage(page), "the focus is never on <body>").toBe(true);

        // Show rows opens the check's rows on the Queries screen.
        await openRun(page, FAILED_ID);
        await tabOnto(page.getByRole("button", { name: `Show rows of ${FAILED_CHECK}` }));
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(new RegExp(`/queries/${ENV}/runs/${ROWS_QUERY_ID}$`));
      });

      test("no horizontal scroll at 320 px, the tasks and the attempts scrolling in their own region", async ({
        page,
      }) => {
        await page.setViewportSize({ width: 320, height: 640 });
        await openJob(page, `?run=${FAILED_ID}`);
        await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
        expect(await sidewaysOverflow(page), "the job page").toBeLessThanOrEqual(0);
        await scrollsInItself(page.getByRole("region", { name: "Tasks of the run" }));

        await openRun(page, FAILED_ID);
        expect(await sidewaysOverflow(page), "a run").toBeLessThanOrEqual(0);
        await scrollsInItself(page.getByRole("region", { name: "Tasks of the run" }));
        expect(await violations(page), "a run at 320 px").toEqual([]);

        await openTask(page);
        expect(await sidewaysOverflow(page), "a task").toBeLessThanOrEqual(0);
        await scrollsInItself(page.getByRole("region", { name: `Attempts of ${TIMED_OUT}` }));
        // The log wraps instead.
        const log = page.getByRole("region", { name: `Log of ${TIMED_OUT}` });
        expect(await log.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
        expect(await violations(page), "a task at 320 px").toEqual([]);
      });

      test("the log grows over three polls, then is complete and read no more", async ({
        page,
      }) => {
        const offsets: string[] = [];
        page.on("request", (request) => {
          const url = new URL(request.url());
          if (url.pathname.endsWith(`/tasks/${TIMED_OUT}/log`)) {
            offsets.push(url.searchParams.get("offset") ?? "");
          }
        });
        await page.goto(taskPage(FAILED_ID, TIMED_OUT));
        const log = page.getByRole("region", { name: `Log of ${TIMED_OUT}` });
        await expect(log).toContainText(LOG_PIECES[0]);
        await expect(page.getByText("Following the log…")).toBeVisible();
        await expect(log).toContainText(LOG_PIECES[1]);
        await expect(log).toContainText(LOG_PIECES[2]);
        await expect(page.getByText("The log is complete.")).toBeVisible();
        expect(await log.textContent()).toBe(LOG_PIECES.join(""));
        await expect(page.getByRole("button", { name: "Save log" })).toBeVisible();
        await page.waitForTimeout(2_500);
        expect(offsets).toEqual([
          "0",
          String(LOG_PIECES[0].length),
          String(LOG_PIECES[0].length + LOG_PIECES[1].length),
        ]);
      });

      test("a running run is not read while the tab is hidden, and is read at once when shown", async ({
        page,
      }) => {
        let reads = 0;
        page.on("request", (request) => {
          if (new URL(request.url()).pathname.endsWith(`/runs/${RUNNING_ID}`)) {
            reads += 1;
          }
        });
        await openRun(page, RUNNING_ID);
        await expect.poll(() => reads, { timeout: 5_000 }).toBeGreaterThan(1);

        await showAs(page, "hidden");
        const hidden = reads;
        await page.waitForTimeout(5_000);
        expect(reads, "no read while hidden").toBe(hidden);

        await showAs(page, "visible");
        await expect.poll(() => reads, { timeout: 1_000 }).toBe(hidden + 1);
      });

      test("the pages ask only for stubbed routes and log no console error", async ({ page }) => {
        const errors = consoleErrors(page);
        const requests: string[] = [];
        page.on("request", (request) => {
          requests.push(new URL(request.url()).pathname);
        });

        await openJob(page);
        await page.getByRole("button", { name: "Run now" }).click();
        await expect(page).toHaveURL(new RegExp(`\\?run=${STARTED_ID}$`));
        await page.getByRole("button", { name: "Pause schedule" }).click();
        await page
          .getByRole("dialog", { name: `Pause the schedule of ${JOB}?` })
          .getByRole("button", { name: "Pause schedule" })
          .click();
        await page.getByRole("button", { name: "Resume schedule" }).click();
        await expect(page.getByRole("button", { name: "Pause schedule" })).toBeVisible();

        await openRun(page, FAILED_ID);
        await whyLine(page, BLOCKED).getByRole("button", { name: "Show source" }).click();
        await expect(
          page.getByRole("dialog").getByText("models/gold/fact_sales.sql"),
        ).toBeVisible();
        await page.keyboard.press("Escape");
        await page.getByRole("link", { name: "Logs" }).click();
        await expect(page.getByText("The log is complete.")).toBeVisible({ timeout: 10_000 });
        await page.goto(runPage(MISSING_ID));
        await expect(page.getByRole("alert")).toBeVisible();

        const api = requests.filter((path) => path.startsWith("/api/"));
        expect(api).toContain(`/api/v1/environments/${ENV}/jobs/${JOB}`);
        expect(api.filter((path) => !JOBS_STUBBED.includes(path))).toEqual([]);
        // A 404 the page shows as a sentence is the browser's log line, not the page's error.
        expect(errors.filter((line) => !line.includes("404"))).toEqual([]);
      });

      test("Home's failed run opens the run's page", async ({ page }) => {
        await page.goto("/");
        const attention = page.getByRole("region", { name: "Needs your attention" });
        await attention.getByRole("link", { name: new RegExp(JOB) }).click();
        await expect(page).toHaveURL(new RegExp(`${runPage(FAILED_ID)}$`));
        await expect(page.getByRole("heading", { level: 1, name: /^Run of / })).toBeVisible();
        await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
      });
    });
  }
}
