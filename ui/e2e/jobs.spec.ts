import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  ADHOC_MESSAGE,
  ADHOC_REPOSITORY,
  BLOCKED,
  DEPLOY_FAILURE,
  DEV,
  DEV_JOB,
  ENV,
  FAILED_CHECK,
  FAILED_ID,
  JOB,
  JOBS_STUBBED,
  LOG_PIECES,
  MISSING_ID,
  OLDER_ONLY_JOB,
  REPOSITORY,
  RESUMED_ID,
  ROWS_QUERY_ID,
  RUNNING_ID,
  SECRET,
  SELECTION_PATTERNS,
  STARTED_ID,
  stubJobs,
  stubWaitingJob,
  TIMED_OUT,
  TIMEOUT_CAUSE,
  WAITING_JOB,
  WAITING_MESSAGE,
} from "./jobs";
import { consoleErrors, violations } from "./session";

const KEY = "lakana.preferences";

/** The Jobs list. */
const LIST = "/jobs";

/** The Runs tab. */
const RUNS_TAB = "/jobs/runs";

/** The Deploys tab, on the protected environment. */
const DEPLOYS_TAB = `/jobs/deploys?environment=${ENV}`;

/** The older deploy's Roll back to this…, by its accessible name. */
const ROLL_BACK = `Roll back to this… (${REPOSITORY} at 4f9d2a7)`;

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
 * Moves the focus with Shift+Tab until it is on an element before it.
 *
 * @param target - The element.
 */
async function tabBackOnto(target: Locator): Promise<void> {
  const page = target.page();
  for (let step = 0; step < 80; step += 1) {
    if (await target.evaluate((node) => node === document.activeElement)) {
      return;
    }
    await page.keyboard.press("Shift+Tab");
  }
  throw new Error("Shift+Tab never reached the element");
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
 * The colour a token resolves to in the page's theme, as `getComputedStyle` writes colours.
 *
 * @param page - The page.
 * @param token - The custom property, such as `--color-accent`.
 * @returns The colour.
 */
function tokenColour(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    const probe = document.createElement("span");
    probe.style.color = getComputedStyle(document.documentElement).getPropertyValue(name);
    document.body.append(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
  }, token);
}

/**
 * Asserts that every link in the page's main region but a button is drawn in the accent, not
 * in the browser's own link colour.
 *
 * @param page - The page.
 * @param where - The page's name in a failure.
 */
async function expectAccentLinks(page: Page, where: string): Promise<void> {
  const accent = await tokenColour(page, "--color-accent");
  const links = await page
    .locator("main a:not(.button)")
    .evaluateAll((nodes) =>
      nodes.map((node) => ({ text: node.textContent, colour: getComputedStyle(node).color })),
    );
  expect(links.length, where).toBeGreaterThan(0);
  expect(
    links.filter((link) => link.colour !== accent),
    `${where}: links in a colour other than the accent`,
  ).toEqual([]);
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

/**
 * Opens the Jobs list and waits for its three sections.
 *
 * @param page - The page.
 * @param query - What follows the address, such as `?environment=dev`.
 */
async function openList(page: Page, query = ""): Promise<void> {
  await page.goto(`${LIST}${query}`);
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: /^Running now/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "Every job listed" })).toBeVisible();
}

/**
 * The gaps between the visible content of every two neighbouring cells of the page's tables:
 * their text and pictures, without what is hidden for the eye.
 *
 * @param page - The page.
 * @returns Each gap, with its table, row and cell.
 */
function cellGaps(page: Page): Promise<{ where: string; gap: number }[]> {
  return page.evaluate(() => {
    const out: { where: string; gap: number }[] = [];
    document.querySelectorAll("table").forEach((table, index) => {
      table.querySelectorAll("tbody tr").forEach((tr, row) => {
        const spans = [...tr.querySelectorAll("td")].map((td) => {
          const rects: DOMRect[] = [];
          const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
            const parent = node.parentElement;
            if (
              (node.textContent ?? "").trim() !== "" &&
              parent?.closest(".visually-hidden") === null
            ) {
              const range = document.createRange();
              range.selectNodeContents(node);
              rects.push(range.getBoundingClientRect());
            }
          }
          td.querySelectorAll("svg, img").forEach((picture) => {
            rects.push(picture.getBoundingClientRect());
          });
          return rects.length === 0
            ? null
            : {
                left: Math.min(...rects.map((rect) => rect.left)),
                right: Math.max(...rects.map((rect) => rect.right)),
              };
        });
        for (let cell = 0; cell + 1 < spans.length; cell += 1) {
          const here = spans[cell];
          const next = spans[cell + 1];
          if (here && next) {
            out.push({
              where: `table ${String(index)} row ${String(row)} cell ${String(cell)}`,
              gap: next.left - here.right,
            });
          }
        }
      });
    });
    return out;
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`the Jobs list, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await stubJobs(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("axe finds nothing on the list, the open filter, a search with no hits and the Cancel and Pause dialogs", async ({
      page,
    }) => {
      await openList(page);
      await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
      await expect(page.getByText(ADHOC_MESSAGE)).toBeVisible();
      expect(await violations(page), "the list").toEqual([]);

      await page.getByRole("button", { name: "Environment: All environments" }).click();
      await expect(page.getByRole("listbox")).toBeVisible();
      expect(await violations(page), "the open filter").toEqual([]);
      await page.keyboard.press("Escape");

      await page.getByRole("button", { name: `Cancel the run of ${JOB}` }).click();
      const cancel = page.getByRole("dialog", { name: `Cancel the run of ${JOB}?` });
      await expect(cancel).toBeVisible();
      expect(await violations(page), "the Cancel dialog").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(cancel).toBeHidden();

      await page.getByRole("button", { name: `Pause the schedule of ${JOB}` }).click();
      const pause = page.getByRole("dialog", { name: `Pause the schedule of ${JOB}?` });
      await expect(pause).toBeVisible();
      expect(await violations(page), "the Pause dialog").toEqual([]);
      await page.keyboard.press("Escape");
      await expect(pause).toBeHidden();

      await page.getByRole("searchbox", { name: "Search jobs" }).fill("no-such-job");
      await expect(page.getByText("Nothing here matches the search.")).toHaveCount(3);
      expect(await violations(page), "a search with no hits").toEqual([]);
    });

    test("at least 24 px between the content of every two neighbouring cells", async ({ page }) => {
      await page.setViewportSize({ width: 1920, height: 1080 });
      await openList(page);
      await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
      const gaps = await cellGaps(page);
      expect(gaps.length).toBeGreaterThanOrEqual(10);
      const tight = gaps.filter((entry) => entry.gap < 24);
      expect(tight, JSON.stringify(tight)).toEqual([]);
    });

    test("the filter, the search, the links and the controls work by keyboard alone", async ({
      page,
    }) => {
      await openList(page);
      const filter = page.getByRole("button", { name: "Environment: All environments" });
      await tabOnto(filter);
      await page.keyboard.press("Enter");
      await expect(page.getByRole("listbox")).toBeVisible();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\?environment=prod$/);
      await expect(
        page.getByRole("button", { name: "Environment: prod, protected" }),
      ).toBeFocused();
      await expect(page.getByRole("link", { name: DEV_JOB })).toHaveCount(0);

      // The search sits left of the filter in the toolbar (#782): one step back.
      await page.keyboard.press("Shift+Tab");
      await expect(page.getByRole("searchbox", { name: "Search jobs" })).toBeFocused();
      await page.keyboard.type("daily");
      await expect(page).toHaveURL(/q=daily/);

      const cancel = page.getByRole("button", { name: `Cancel the run of ${JOB}` });
      await tabOnto(cancel);
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: `Cancel the run of ${JOB}?` });
      await expect(dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(cancel).toBeFocused();

      const pause = page.getByRole("button", { name: `Pause the schedule of ${JOB}` });
      await tabOnto(pause);
      await page.keyboard.press("Enter");
      const asked = page.getByRole("dialog", { name: `Pause the schedule of ${JOB}?` });
      await asked.getByRole("button", { name: "Pause schedule" }).press("Enter");
      await expect(page.getByText(`Paused the schedule of ${JOB}.`)).toBeVisible();
      await expect(
        page.getByRole("button", { name: `Resume the schedule of ${JOB}` }),
      ).toBeFocused();

      const jobLink = page
        .getByRole("region", { name: "Every job listed" })
        .getByRole("link", { name: JOB });
      await tabBackOnto(jobLink);
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(new RegExp(`${JOB_PAGE}$`));
      await expect(page.getByRole("heading", { level: 1, name: JOB })).toBeVisible();
    });

    test("a control's press does not open its row", async ({ page }) => {
      await openList(page);
      const run = page.getByRole("button", { name: `Run ${JOB}` });
      await run.click();
      await expect(page.getByText(`Started a run of ${JOB}.`)).toBeVisible();
      await expect(page).toHaveURL(new RegExp(`${LIST}$`));
      await expect(run).toBeFocused();
      // The run it queued shows under Running now once the list is read again.
      await expect(
        page.getByRole("region", { name: "Runs running now" }).getByText("Queued", { exact: true }),
      ).toHaveCount(2);
    });

    test("no horizontal scroll at 320 px, each list scrolling in its own region", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 320, height: 640 });
      await openList(page);
      await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
      expect(await sidewaysOverflow(page)).toBeLessThanOrEqual(0);
      for (const region of ["Runs running now", "Runs that failed", "Every job listed"]) {
        await scrollsInItself(page.getByRole("region", { name: region }));
      }
      expect(await violations(page), "the list at 320 px").toEqual([]);
    });

    test("the filter is remembered across a reload; Back and Forward keep the filter and the search", async ({
      page,
    }) => {
      await openList(page);
      await page.getByRole("button", { name: "Environment: All environments" }).click();
      await page.getByRole("option", { name: DEV }).click();
      await expect(page).toHaveURL(/\?environment=dev$/);
      await page.getByRole("searchbox", { name: "Search jobs" }).fill("orders");
      await expect(page).toHaveURL(/\?environment=dev&q=orders$/);

      await page.goto(LIST);
      await expect(page.getByRole("button", { name: "Environment: dev" })).toBeVisible();
      await expect(page.getByRole("link", { name: JOB })).toHaveCount(0);
      await expect(page.getByRole("link", { name: DEV_JOB }).first()).toBeVisible();

      await page.goto(`${LIST}?environment=prod&q=daily`);
      await expect(
        page.getByRole("button", { name: "Environment: prod, protected" }),
      ).toBeVisible();
      await page
        .getByRole("region", { name: "Every job listed" })
        .getByRole("link", { name: JOB })
        .click();
      await expect(page).toHaveURL(new RegExp(`${JOB_PAGE}$`));
      await page.goBack();
      await expect(page).toHaveURL(/\?environment=prod&q=daily$/);
      await expect(page.getByRole("searchbox", { name: "Search jobs" })).toHaveValue("daily");
      await expect(
        page.getByRole("button", { name: "Environment: prod, protected" }),
      ).toBeVisible();
      await page.goForward();
      await expect(page).toHaveURL(new RegExp(`${JOB_PAGE}$`));
    });

    test("the list is not read while the tab is hidden, and is read at once when shown", async ({
      page,
    }) => {
      let reads = 0;
      page.on("request", (request) => {
        if (new URL(request.url()).pathname === "/api/v1/runs") {
          reads += 1;
        }
      });
      await page.clock.install();
      await openList(page);
      await expect.poll(() => reads).toBe(1);

      await showAs(page, "hidden");
      await page.clock.runFor(120_000);
      expect(reads, "no read while hidden").toBe(1);

      await showAs(page, "visible");
      await expect.poll(() => reads, { timeout: 2_000 }).toBe(2);
    });

    test("the list asks only for stubbed routes and logs no console error", async ({ page }) => {
      const errors = consoleErrors(page);
      const requests: string[] = [];
      page.on("request", (request) => {
        requests.push(new URL(request.url()).pathname);
      });

      await openList(page);
      await expect(page.getByText(ADHOC_MESSAGE)).toBeVisible();
      await expect(page.getByRole("link", { name: ADHOC_REPOSITORY, exact: true })).toBeVisible();
      await page.getByRole("button", { name: `Run ${JOB}` }).click();
      await expect(page.getByText(`Started a run of ${JOB}.`)).toBeVisible();
      await page.getByRole("button", { name: "Environment: All environments" }).click();
      await page.getByRole("option", { name: DEV }).click();
      await expect(page.getByRole("link", { name: JOB })).toHaveCount(0);
      await page.getByRole("link", { name: `Logs of the run of ${ADHOC_REPOSITORY}` }).click();
      await expect(page.getByRole("heading", { level: 1, name: /^Run of / })).toBeVisible();

      const api = requests.filter((path) => path.startsWith("/api/"));
      expect(api).toContain("/api/v1/runs/failed");
      expect(api.filter((path) => !JOBS_STUBBED.includes(path))).toEqual([]);
      expect(errors.filter((line) => !line.includes("404"))).toEqual([]);
    });
  });

  test.describe(`the Runs and Deploys tabs, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await stubJobs(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    /**
     * Opens the Runs tab and waits for its first page.
     *
     * @param page - The page.
     * @param query - What follows the address.
     */
    async function openRuns(page: Page, query = ""): Promise<void> {
      await page.goto(`${RUNS_TAB}${query}`);
      await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
      await expect(
        page
          .getByRole("region", { name: "Runs listed" })
          .or(page.getByText("No runs match these filters.")),
      ).toBeVisible();
    }

    /**
     * Opens the Deploys tab of the protected environment and waits for its rows.
     *
     * @param page - The page.
     */
    async function openDeploys(page: Page): Promise<void> {
      await page.goto(DEPLOYS_TAB);
      await expect(page.getByRole("heading", { level: 1, name: "Deploys" })).toBeVisible();
      await expect(page.getByRole("button", { name: ROLL_BACK })).toBeVisible();
    }

    test("axe finds nothing on the Runs tab, the Deploys tab and the rollback dialog", async ({
      page,
    }) => {
      await openRuns(page);
      await page.getByRole("button", { name: "Show more" }).click();
      await expect(page.getByText(`selection ${SELECTION_PATTERNS.join(" ")}`)).toBeVisible();
      expect(await violations(page), "the Runs tab").toEqual([]);

      await openDeploys(page);
      await expect(page.getByText(DEPLOY_FAILURE)).toBeVisible();
      expect(await violations(page), "the Deploys tab").toEqual([]);

      await page.getByRole("button", { name: ROLL_BACK }).click();
      const dialog = page.getByRole("dialog", { name: `Roll back ${REPOSITORY} in ${ENV}?` });
      await expect(dialog.getByRole("button", { name: "Roll back" })).toBeVisible();
      await expect(dialog).toContainText(`${ENV} is protected.`);
      expect(await violations(page), "the rollback dialog").toEqual([]);
    });

    test("the tabs, the filters, Show more, Read again and a rollback work by keyboard alone", async ({
      page,
    }) => {
      await openList(page);
      const runsTab = page
        .getByRole("navigation", { name: "Jobs views" })
        .getByRole("link", { name: "Runs" });
      await tabOnto(runsTab);
      await page.keyboard.press("Enter");
      await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
      await expect(page.getByRole("region", { name: "Runs listed" })).toBeVisible();

      // The filter's menu, not the column header that sorts by the state.
      const state = page
        .getByRole("button", { name: /State/ })
        .and(page.locator(":not(.sort-head)"));
      await tabOnto(state);
      await page.keyboard.press("Enter");
      await page.getByRole("option", { name: "Queued or running" }).press("Enter");
      await expect(page).toHaveURL(/status=queued%2Crunning/);
      await expect(state).toBeFocused();

      const job = page.getByRole("textbox", { name: "Job" });
      await tabOnto(job);
      await page.keyboard.type(DEV_JOB);
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(new RegExp(`job=${DEV_JOB}`));
      await expect(page.getByRole("region", { name: "Runs listed" }).getByRole("row")).toHaveCount(
        2,
      );

      await openRuns(page);
      const more = page.getByRole("button", { name: "Show more" });
      await tabOnto(more);
      await page.keyboard.press("Enter");
      await expect(page.getByText(`selection ${SELECTION_PATTERNS.join(" ")}`)).toBeVisible();
      expect(await focusOnPage(page)).toBe(true);
      const again = page.getByRole("button", { name: "Read again" });
      await tabBackOnto(again);
      await page.keyboard.press("Enter");
      await expect(again).toBeHidden();
      await expect(page.getByRole("button", { name: "Show more" })).toBeVisible();

      await openDeploys(page);
      const rollBack = page.getByRole("button", { name: ROLL_BACK });
      await tabOnto(rollBack);
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: `Roll back ${REPOSITORY} in ${ENV}?` });
      await expect(dialog.getByRole("button", { name: "Roll back" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(rollBack).toBeFocused();

      await page.keyboard.press("Enter");
      await dialog.getByRole("button", { name: "Roll back" }).press("Enter");
      await expect(dialog).toBeHidden();
      await expect(page.getByText(`Rolled back ${REPOSITORY} in ${ENV} to 4f9d2a7.`)).toBeVisible({
        timeout: 10_000,
      });
      expect(await focusOnPage(page)).toBe(true);
    });

    test("a rollback names the jobs it changes and is followed from running to succeeded", async ({
      page,
    }) => {
      await openDeploys(page);
      const rows = page.getByRole("region", { name: `Deploys of ${ENV}` }).getByRole("row");
      // The dry run, the current deploy, the failed one, the older deploy and the rollback.
      await expect(rows).toHaveCount(6);
      await expect(rows.nth(2)).toContainText("9c1e5b3 · Review 4-6: four jobs · current");
      // A rollback from before subjects were stored shows its commit's ID alone.
      await expect(rows.nth(5)).toContainText("0be1f6a");
      await expect(rows.nth(5)).not.toContainText("·");
      await expect(rows.nth(4)).toContainText("an account since deleted");
      await expect(page.getByRole("button", { name: /^Roll back to this… / })).toHaveCount(2);

      await page.getByRole("button", { name: ROLL_BACK }).click();
      const dialog = page.getByRole("dialog", { name: `Roll back ${REPOSITORY} in ${ENV}?` });
      const jobs = dialog.getByRole("list", { name: "Jobs the rollback changes" });
      await expect(jobs.getByRole("listitem")).toHaveText([
        JOB,
        `${OLDER_ONLY_JOB}: back with the rollback`,
      ]);
      await dialog.getByRole("button", { name: "Roll back" }).click();
      await expect(dialog).toBeHidden();
      await expect(page.getByRole("button", { name: ROLL_BACK })).toBeFocused();
      await expect(
        page.getByText(`Rolling back ${REPOSITORY} in ${ENV} to 4f9d2a7…`),
      ).toBeVisible();
      await expect(page.getByText(`Rolled back ${REPOSITORY} in ${ENV} to 4f9d2a7.`)).toBeVisible({
        timeout: 10_000,
      });
      // The list was read again: the rollback heads it, and the older version is current.
      await expect(rows).toHaveCount(7);
      await expect(rows.nth(1)).toContainText("Rollback");
      await expect(rows.nth(1)).toContainText("4f9d2a7 · Rank stations by daily riders · current");
      // The older deploy made the same version, but only the newest run of it is current.
      await expect(rows.nth(5)).toContainText("4f9d2a7 · Rank stations by daily riders");
      await expect(rows.nth(5)).not.toContainText("current");
      await expect(page.getByRole("button", { name: ROLL_BACK })).toHaveCount(0);
    });

    test("Back and Forward keep each tab's filters; a job links to its deploys", async ({
      page,
    }) => {
      await openRuns(page, `?environment=${ENV}&trigger=schedule&period=7d`);
      await page
        .getByRole("navigation", { name: "Jobs views" })
        .getByRole("link", { name: "Deploys" })
        .click();
      await expect(page.getByRole("heading", { level: 1, name: "Deploys" })).toBeVisible();
      await page.getByRole("button", { name: /Repository/ }).click();
      await page.getByRole("option", { name: REPOSITORY }).click();
      await expect(page).toHaveURL(new RegExp(`repository=${REPOSITORY}`));
      await page.goBack();
      await expect(page).toHaveURL(
        new RegExp(`/jobs/runs\\?environment=${ENV}&trigger=schedule&period=7d$`),
      );
      await expect(page.getByRole("button", { name: /Trigger/ })).toContainText("Schedule");
      await expect(page.getByRole("button", { name: /Period/ })).toContainText("Last 7 days");
      await page.goForward();
      await expect(page).toHaveURL(
        new RegExp(`/jobs/deploys\\?environment=${ENV}&repository=${REPOSITORY}$`),
      );

      await openJob(page);
      await page.getByRole("link", { name: `Deploys of ${REPOSITORY}` }).click();
      await expect(page).toHaveURL(
        new RegExp(`/jobs/deploys\\?environment=${ENV}&repository=${REPOSITORY}$`),
      );
      await expect(page.getByRole("button", { name: ROLL_BACK })).toBeVisible();
    });

    test("Back undoes the Runs tab's filters one at a time and stays on the tab; typing a job is one step", async ({
      page,
    }) => {
      await page.goto("/jobs");
      await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
      await page
        .getByRole("navigation", { name: "Jobs views" })
        .getByRole("link", { name: "Runs" })
        .click();
      await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
      const listed = page.getByRole("region", { name: "Runs listed" });
      const devRun = listed.locator(".env-label").filter({ hasText: DEV });
      await expect(devRun.first()).toBeVisible();

      // Back undoes an environment chosen, though this browser remembers it for the next visit.
      await page.getByRole("button", { name: "Environment: All environments" }).click();
      await page.getByRole("option", { name: /prod/u }).click();
      await expect(page).toHaveURL(new RegExp(`/jobs/runs\\?environment=${ENV}$`, "u"));
      await expect(
        page.getByRole("button", { name: `Environment: ${ENV}, protected` }),
      ).toBeVisible();
      await expect(devRun).toHaveCount(0);
      await page.goBack();
      await expect(page).toHaveURL(/\/jobs\/runs$/u);
      await expect(
        page.getByRole("button", { name: "Environment: All environments" }),
      ).toBeVisible();
      await expect(devRun.first()).toBeVisible();

      // Each change goes straight on to the next, without waiting for the list (#744). The
      // Period is chosen and the job typed in one task of the page, before the address the
      // Period led to renders: both reach the address.
      await page.getByRole("button", { name: /Trigger/ }).click();
      await page.getByRole("option", { name: "Schedule" }).click();
      await page.getByRole("button", { name: /Period/ }).click();
      await page.getByRole("option", { name: "Last 7 days" }).waitFor();
      await page.evaluate(() => {
        const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
          (element) => element.textContent.includes("Last 7 days"),
        );
        option?.click();
        const field = document.querySelector<HTMLInputElement>(".jobs-job-field input");
        if (field !== null) {
          // Key by key, as a person types.
          for (let end = 1; end <= "nightly".length; end += 1) {
            // The prototype's setter, so that React sees the value change.
            Reflect.set(HTMLInputElement.prototype, "value", "nightly".slice(0, end), field);
            field.dispatchEvent(new Event("input", { bubbles: true }));
          }
          field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        }
      });
      const job = page.getByRole("textbox", { name: "Job" });
      await expect(page).toHaveURL(/\/jobs\/runs\?trigger=schedule&period=7d&job=nightly$/u);

      // The job's name typed key by key is one step back, not seven.
      await page.goBack();
      await expect(page).toHaveURL(/\/jobs\/runs\?trigger=schedule&period=7d$/u);
      await expect(job).toHaveValue("");
      await page.goBack();
      await expect(page).toHaveURL(/\/jobs\/runs\?trigger=schedule$/u);
      await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
      await expect(page.getByRole("button", { name: /Trigger/ })).toContainText("Schedule");
      await expect(page.getByRole("button", { name: /Period/ })).toContainText("Last 30 days");
      await page.goForward();
      await expect(page).toHaveURL(/\/jobs\/runs\?trigger=schedule&period=7d$/u);
      await expect(page.getByRole("button", { name: /Period/ })).toContainText("Last 7 days");
    });

    test("the Runs tab opens on the environment chosen on the Jobs list, and Back undoes a change of it", async ({
      page,
    }) => {
      await page.goto("/jobs");
      await page.getByRole("button", { name: "Environment: All environments" }).click();
      await page.getByRole("option", { name: /prod/u }).click();
      await expect(page).toHaveURL(/\/jobs\?environment=prod$/u);
      // The list opens on the remembered environment without naming it in the address.
      await page.goto("/jobs");
      await expect(
        page.getByRole("button", { name: `Environment: ${ENV}, protected` }),
      ).toBeVisible();
      await expect(page).toHaveURL(/\/jobs$/u);

      const tabs = page.getByRole("navigation", { name: "Jobs views" });
      await tabs.getByRole("link", { name: "Runs" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
      await expect(page).toHaveURL(new RegExp(`/jobs/runs\\?environment=${ENV}$`, "u"));
      await expect(
        page.getByRole("button", { name: `Environment: ${ENV}, protected` }),
      ).toBeVisible();
      const listed = page.getByRole("region", { name: "Runs listed" });
      await expect(listed.locator(".env-label").filter({ hasText: DEV })).toHaveCount(0);

      // A change of environment is a step of its own: Back restores the one before it.
      await page.getByRole("button", { name: `Environment: ${ENV}, protected` }).click();
      await page.getByRole("option", { name: "All environments" }).click();
      await expect(page).toHaveURL(/\/jobs\/runs$/u);
      await page.goBack();
      await expect(page).toHaveURL(new RegExp(`/jobs/runs\\?environment=${ENV}$`, "u"));
      await expect(
        page.getByRole("button", { name: `Environment: ${ENV}, protected` }),
      ).toBeVisible();
      // The address the tab took added no step: Back again returns to the list.
      await page.goBack();
      await expect(page).toHaveURL(/\/jobs$/u);
      await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();

      // All environments, chosen once, sticks across the tabs.
      await page.goForward();
      await page.goForward();
      await expect(page).toHaveURL(/\/jobs\/runs$/u);
      await page.goto("/jobs/runs");
      await expect(
        page.getByRole("button", { name: "Environment: All environments" }),
      ).toBeVisible();
      await expect(page).toHaveURL(/\/jobs\/runs$/u);
      await page.goto("/jobs");
      await expect(
        page.getByRole("button", { name: "Environment: All environments" }),
      ).toBeVisible();
    });

    test("a resume opens the run it continues, and the tab offers no start", async ({ page }) => {
      await openRuns(page);
      await page.getByRole("button", { name: "Show more" }).click();
      await expect(page.getByText(`selection ${SELECTION_PATTERNS.join(" ")}`)).toBeVisible();
      // The column headers sort the runs; none of them starts one.
      await expect(
        page.getByRole("button", { name: /run|start/i }).and(page.locator(":not(.sort-head)")),
      ).toHaveCount(0);
      await page.getByRole("link", { name: `resume of ${FAILED_ID.slice(0, 8)}` }).click();
      await expect(page).toHaveURL(new RegExp(`${runPage(FAILED_ID)}$`));
      await expect(page.getByRole("navigation", { name: "Jobs views" })).toHaveCount(0);
    });

    test("no horizontal scroll at 320 px, each list in its own region and the tabs wrapping", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 320, height: 640 });
      await openRuns(page);
      expect(await sidewaysOverflow(page)).toBeLessThanOrEqual(0);
      await scrollsInItself(page.getByRole("region", { name: "Runs listed" }));
      const tabs = page.getByRole("navigation", { name: "Jobs views" }).getByRole("link");
      for (const tab of await tabs.all()) {
        const box = await tab.boundingBox();
        expect(box && box.x + box.width).toBeLessThanOrEqual(320);
      }
      expect(await violations(page), "the Runs tab at 320 px").toEqual([]);

      await openDeploys(page);
      expect(await sidewaysOverflow(page)).toBeLessThanOrEqual(0);
      await scrollsInItself(page.getByRole("region", { name: `Deploys of ${ENV}` }));
      expect(await violations(page), "the Deploys tab at 320 px").toEqual([]);
      await page.getByRole("button", { name: ROLL_BACK }).click();
      await expect(
        page.getByRole("dialog").getByRole("button", { name: "Roll back" }),
      ).toBeVisible();
      expect(await sidewaysOverflow(page)).toBeLessThanOrEqual(0);
      expect(await violations(page), "the rollback dialog at 320 px").toEqual([]);
    });

    test("no read while the tab is hidden, on the Runs tab and of a rollback", async ({ page }) => {
      let runs = 0;
      let rollback = 0;
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (path === "/api/v1/runs") {
          runs += 1;
        }
        if (path.startsWith(`/api/v1/environments/${ENV}/deploys/0a1b2c3d`)) {
          rollback += 1;
        }
      });
      await page.clock.install();
      await openRuns(page);
      await expect.poll(() => runs).toBe(1);
      await showAs(page, "hidden");
      await page.clock.runFor(120_000);
      expect(runs, "no read of the runs while hidden").toBe(1);
      await showAs(page, "visible");
      await expect.poll(() => runs, { timeout: 2_000 }).toBe(2);

      await openDeploys(page);
      await page.getByRole("button", { name: ROLL_BACK }).click();
      await page.getByRole("dialog").getByRole("button", { name: "Roll back" }).click();
      await expect(
        page.getByText(`Rolling back ${REPOSITORY} in ${ENV} to 4f9d2a7…`),
      ).toBeVisible();
      await expect.poll(() => rollback).toBe(1);
      await showAs(page, "hidden");
      await page.clock.runFor(60_000);
      expect(rollback, "no read of the rollback while hidden").toBe(1);
      await showAs(page, "visible");
      await expect.poll(() => rollback, { timeout: 2_000 }).toBe(2);
      await expect(page.getByText(`Rolled back ${REPOSITORY} in ${ENV} to 4f9d2a7.`)).toBeVisible();
    });

    test("the tabs ask only for stubbed routes and log no console error", async ({ page }) => {
      const errors = consoleErrors(page);
      const requests: string[] = [];
      page.on("request", (request) => {
        requests.push(new URL(request.url()).pathname);
      });

      await openRuns(page);
      await page.getByRole("button", { name: "Show more" }).click();
      await expect(page.getByText(`selection ${SELECTION_PATTERNS.join(" ")}`)).toBeVisible();
      await page.goto("/jobs/deploys");
      await expect(page.getByText(`No deploy in ${DEV} yet.`)).toBeVisible();
      await openDeploys(page);
      await page.getByRole("button", { name: ROLL_BACK }).click();
      await page.getByRole("dialog").getByRole("button", { name: "Roll back" }).click();
      await expect(page.getByText(`Rolled back ${REPOSITORY} in ${ENV} to 4f9d2a7.`)).toBeVisible({
        timeout: 10_000,
      });

      const api = requests.filter((path) => path.startsWith("/api/"));
      expect(api).toContain(`/api/v1/environments/${ENV}/deployments`);
      expect(api.filter((path) => !JOBS_STUBBED.includes(path))).toEqual([]);
      expect(errors).toEqual([]);
    });
  });

  test.describe(`the job, run and task pages, ${theme}`, () => {
    let sent: string[] = [];

    test.beforeEach(async ({ page }) => {
      sent = await stubJobs(page);
      await page.addInitScript(
        ([key, value]) => {
          if (window.localStorage.getItem(key ?? "") === null) {
            window.localStorage.setItem(key ?? "", value ?? "");
          }
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
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

    test("every link is drawn in the accent, and the selected run's row in the accent's tint", async ({
      page,
    }) => {
      await openJob(page);
      const selected = page
        .getByRole("region", { name: `Runs of ${JOB}` })
        .locator("tr", { has: page.locator('a.row-name[aria-current="true"]') });
      await expect(selected).toHaveCount(1);
      const tint = await tokenColour(page, "--color-accent-bg");
      await expect(selected).toHaveCSS("background-color", tint);
      await expect(page.getByRole("link", { name: /open in repository/ })).toBeVisible();
      await expectAccentLinks(page, "the job page");

      await openRun(page, FAILED_ID);
      await expect(page.getByText(TIMEOUT_CAUSE)).toBeVisible();
      await expect(page.locator("a.jobs-code").first()).toBeVisible();
      await expectAccentLinks(page, "a failed run");

      await openTask(page);
      await expectAccentLinks(page, "a task");
    });

    test("the breadcrumbs lead from a task back to its run, its job and Jobs", async ({ page }) => {
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

    test("the log grows over three polls, then is complete and read no more", async ({ page }) => {
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
      await expect(page.getByRole("dialog").getByText("models/gold/fact_sales.sql")).toBeVisible();
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

for (const theme of ["light", "dark"] as const) {
  test.describe(`a job whose start waits, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await stubJobs(page);
      await stubWaitingJob(page);
      await page.addInitScript(
        ([key, value]) => {
          window.localStorage.setItem(key ?? "", value ?? "");
        },
        [KEY, JSON.stringify({ theme, language: "en" })],
      );
    });

    test("shows Waiting in All jobs, on its page and in Runs, with why on hover and focus", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      const errors = consoleErrors(page);
      await openList(page);
      const row = page
        .getByRole("region", { name: "Every job listed" })
        .getByRole("row")
        .filter({ has: page.getByRole("link", { name: WAITING_JOB }) });
      const pill = row.locator("[data-tip]");
      await expect(pill).toContainText("Waiting");
      await expect(row).toContainText("Succeeded");
      expect(await violations(page), "the list").toEqual([]);

      const tip = page.locator(".truncation-tip");
      await pill.hover();
      await expect(tip).toHaveText(WAITING_MESSAGE);
      await page.mouse.move(0, 0);
      await expect(tip).toBeHidden();
      await pill.focus();
      await expect(tip).toHaveText(WAITING_MESSAGE);
      await page.keyboard.press("Escape");
      await expect(tip).toBeHidden();

      // A click on the pill opens the job, as the rest of the row does.
      await pill.click();
      await expect(page.getByRole("heading", { level: 1, name: WAITING_JOB })).toBeVisible();
      const facts = page.locator(".jobs-waiting");
      await expect(facts).toContainText("Waiting");
      await expect(facts).toContainText(WAITING_MESSAGE);
      await expect(facts).toContainText("By hand");
      await expect(facts.getByRole("link", { name: "Open the run it waits for" })).toBeVisible();
      expect(await violations(page), "the job's page").toEqual([]);

      await page.goto("/jobs/runs?period=all");
      const runs = page.getByRole("region", { name: "Runs listed" });
      const first = runs.getByRole("row").nth(1);
      await expect(first.getByRole("link", { name: WAITING_JOB })).toBeVisible();
      await expect(first).toContainText("Waiting");
      await expect(first.getByRole("link", { name: /^waits for run / })).toBeVisible();
      // The pill sits before the job's link, whose layer covers the row: it must still be on top.
      await first.locator("[data-tip]").hover();
      await expect(tip).toHaveText(WAITING_MESSAGE);
      await page.mouse.move(0, 0);
      await expect(tip).toBeHidden();
      expect(await violations(page), "the Runs tab").toEqual([]);
      expect(errors).toEqual([]);
    });
  });
}
