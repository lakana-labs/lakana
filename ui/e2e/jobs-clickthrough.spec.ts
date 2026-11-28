/**
 * The click-through checks of the Jobs screen that the review of M7 parts 4–6 made by hand: the
 * job page's filters in the browser's history and the selected run's weight, the dialogs'
 * sentences, where the focus goes after a cancel, a start that waits, the links' look, the focus ring, and the chunks each page loads. The stubs are
 * those of `jobs.ts`.
 *
 * @packageDocumentation
 */
import { expect, type Locator, type Page, test } from "@playwright/test";

import { expectRingOnEveryStop } from "./focus-ring";
import { ENV, FAILED_ID, JOB, REPOSITORY, RUNNING_ID, stubJobs, TIMED_OUT } from "./jobs";

/** The job's page. */
const JOB_PAGE = `/jobs/${ENV}/job/${REPOSITORY}/${JOB}`;

/** The older deploy's Roll back to this…, by its accessible name. */
const ROLL_BACK = `Roll back to this… (${REPOSITORY} at 4f9d2a7)`;

/**
 * Where the page is: its path and query.
 *
 * @param page - The page.
 * @returns The address without the origin.
 */
function where(page: Page): string {
  const url = new URL(page.url());
  return `${url.pathname}${url.search}`;
}

/**
 * Presses Back once for each address before the last, newest first, and requires each in turn.
 *
 * @param page - The page.
 * @param seen - The addresses in the order they were reached.
 */
async function backThrough(page: Page, seen: readonly string[]): Promise<void> {
  for (let at = seen.length - 2; at >= 0; at -= 1) {
    await page.goBack();
    await expect.poll(() => where(page)).toBe(seen[at]);
  }
}

/**
 * Whether a link is underlined and drawn in the accent.
 *
 * @param link - The link.
 * @returns Its decoration and whether its colour is the accent's.
 */
function linkLook(link: Locator): Promise<{ underline: boolean; accent: boolean }> {
  return link.evaluate((node) => {
    const probe = document.createElement("span");
    probe.style.color = getComputedStyle(document.documentElement).getPropertyValue(
      "--color-accent",
    );
    document.body.append(probe);
    const accent = getComputedStyle(probe).color;
    probe.remove();
    const style = getComputedStyle(node);
    return {
      underline: style.textDecorationLine.includes("underline"),
      accent: style.color === accent,
    };
  });
}

test.describe("the Jobs screen, checked by clicking through", () => {
  test.beforeEach(async ({ page }) => {
    await stubJobs(page);
  });

  test("on a job's page each filter and run chosen goes into the address, Back undoes them one by one, and the chosen run's row is tinted and heavier", async ({
    page,
  }) => {
    await page.goto(JOB_PAGE);
    const runs = page.getByRole("region", { name: `Runs of ${JOB}` });
    await expect(runs).toBeVisible();
    await expect(page.getByRole("button", { name: /Period/u })).toContainText("Last 30 days");
    const seen = [where(page)];

    const another = runs.locator("a.row-name").nth(1);
    await another.click();
    await expect(page).toHaveURL(/[?&]run=/u);
    seen.push(where(page));
    const chosen = runs.locator('a.row-name[aria-current="true"]');
    await expect(chosen).toHaveCount(1);
    const tint = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.style.backgroundColor = getComputedStyle(document.documentElement).getPropertyValue(
        "--color-accent-bg",
      );
      document.body.append(probe);
      const colour = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return colour;
    });
    await expect(
      runs.locator("tr", { has: page.locator('a.row-name[aria-current="true"]') }),
    ).toHaveCSS("background-color", tint);
    // Read in the page through a CSS locator, not through `runs`: Firefox crashes when Playwright
    // reads an aria-labelledby element's references in both its own world and the page's (a
    // named getByRole, then evaluateAll). Mozilla bug 2090055; Playwright #43967, fixed in its
    // Firefox build 1575 and later.
    const weights = await page.locator(".jobs-runs-scroll a.row-name").evaluateAll((links) =>
      links.map((link) => ({
        chosen: link.getAttribute("aria-current") === "true",
        weight: Number(getComputedStyle(link).fontWeight),
      })),
    );
    const heavy = weights.find((one) => one.chosen)?.weight ?? 0;
    expect(weights.filter((one) => !one.chosen).every((one) => one.weight < heavy)).toBe(true);

    await page.getByRole("button", { name: /Status/u, expanded: false }).click();
    await page.getByRole("option", { name: "Failed" }).click();
    await expect(page).toHaveURL(/[?&]status=failed/u);
    seen.push(where(page));
    await page.getByRole("button", { name: /Period/u }).click();
    await page.getByRole("option", { name: "Last 7 days" }).click();
    await expect(page).toHaveURL(/[?&]period=7d/u);
    seen.push(where(page));

    await backThrough(page, seen);
  });

  test("the Cancel and Pause dialogs say what happens, and a cancelled run's page gives the focus to its heading", async ({
    page,
  }) => {
    await page.goto(JOB_PAGE);
    await page.getByRole("button", { name: "Pause schedule" }).click();
    const pause = page.getByRole("dialog", { name: `Pause the schedule of ${JOB}?` });
    await expect(pause).toContainText(
      "No run starts on schedule until the schedule is resumed. A run that is running finishes.",
    );
    await expect(pause.getByRole("button")).toHaveText(["Pause schedule", "Cancel", "×"]);
    await expect(pause.getByRole("button", { name: "Close dialog" })).toBeVisible();
    await page.keyboard.press("Escape");

    await page.goto(`/jobs/${ENV}/runs/${RUNNING_ID}`);
    const cancelRun = page.getByRole("button", { name: "Cancel run" });
    await cancelRun.click();
    const ask = page.getByRole("dialog", { name: `Cancel the run of ${JOB}?` });
    await expect(ask).toContainText(
      "Running tasks stop and waiting tasks do not start. Finished tasks stay done.",
    );
    await expect(ask.getByRole("button")).toHaveText(["Cancel run", "Cancel", "×"]);
    await ask.getByRole("button", { name: "Cancel run" }).click();
    await expect(page.locator(".jobs-facts-list")).toContainText("Cancelled", { timeout: 10_000 });
    await expect(cancelRun).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 1 })).toBeFocused();
  });

  test("a run cancelled under Running now leaves it, and the focus goes to the section's heading", async ({
    page,
  }) => {
    await page.goto("/jobs");
    const running = page.getByRole("region", { name: "Runs running now" });
    await expect(running.getByRole("link", { name: JOB })).toBeVisible();
    await running.getByRole("button", { name: `Cancel the run of ${JOB}` }).click();
    await page
      .getByRole("dialog", { name: `Cancel the run of ${JOB}?` })
      .getByRole("button", { name: "Cancel run" })
      .click();
    await expect(running.getByRole("link", { name: JOB })).toHaveCount(0, { timeout: 10_000 });
    await expect(page.getByRole("heading", { level: 2, name: /^Running now/u })).toBeFocused();
  });

  test("a start that waits says why, and its link, underlined in the accent, opens the run it waits for", async ({
    page,
  }) => {
    await page.route(
      (url) => url.pathname === `/api/v1/environments/${ENV}/jobs/${JOB}/runs`,
      (route) =>
        route.fulfill({
          status: 202,
          json: {
            run: null,
            waiting: {
              waits_for_run_id: RUNNING_ID,
              message: `A run of ${REPOSITORY} is running; one run of a project at a time.`,
            },
          },
        }),
    );
    await page.goto(JOB_PAGE);
    await page.getByRole("button", { name: "Run now" }).click();
    await expect(
      page.getByText(
        `The start waits: A run of ${REPOSITORY} is running; one run of a project at a time.`,
      ),
    ).toBeVisible();
    const link = page.getByRole("link", { name: "Open the run it waits for" }).first();
    expect(await linkLook(link)).toEqual({ underline: true, accent: true });
    await link.click();
    await expect(page).toHaveURL(new RegExp(`/jobs/${ENV}/runs/${RUNNING_ID}$`, "u"));
  });

  test("a failure's code and a resume's origin are links underlined in the accent", async ({
    page,
  }) => {
    await page.goto(`/jobs/${ENV}/runs/${FAILED_ID}`);
    const code = page.locator("a.jobs-code").first();
    await expect(code).toBeVisible();
    expect(await linkLook(code)).toEqual({ underline: true, accent: true });

    await page.goto("/jobs/runs");
    await page.getByRole("button", { name: "Show more" }).click();
    const resume = page.getByRole("link", { name: `resume of ${FAILED_ID.slice(0, 8)}` });
    await expect(resume).toBeVisible();
    expect(await linkLook(resume)).toEqual({ underline: true, accent: true });
  });

  test("an attempt's unmeasured figures show a dash, not an empty cell", async ({ page }) => {
    await page.goto(`/jobs/${ENV}/runs/${FAILED_ID}/tasks/${TIMED_OUT}`);
    const attempts = page.getByRole("region", { name: `Attempts of ${TIMED_OUT}` });
    // The first attempt measured nothing: Rows, Peak memory, Processor time and Bytes read are
    // the sixth, eighth, ninth and tenth cells.
    const row = attempts.getByRole("row").nth(1).getByRole("cell");
    for (const index of [5, 7, 8, 9]) {
      const cell = row.nth(index);
      await expect(cell.getByText("–")).toHaveAttribute("aria-hidden", "true");
      await expect(cell.getByText("Not measured")).toHaveClass("visually-hidden");
    }
  });

  test("every Tab stop draws a focus ring on the list, a job, a run, the Runs and Deploys tabs and the rollback dialog", async ({
    page,
  }) => {
    for (const [path, ready] of [
      ["/jobs", page.getByRole("region", { name: "Every job listed" })],
      [JOB_PAGE, page.getByRole("region", { name: `Runs of ${JOB}` })],
      [`/jobs/${ENV}/runs/${FAILED_ID}`, page.getByRole("region", { name: "Tasks of the run" })],
      ["/jobs/runs", page.getByRole("region", { name: "Runs listed" })],
      [`/jobs/deploys?environment=${ENV}`, page.getByRole("button", { name: ROLL_BACK })],
    ] as const) {
      await page.goto(path);
      await expect(ready).toBeVisible();
      await expectRingOnEveryStop(page, 8);
    }
    await page.getByRole("button", { name: ROLL_BACK }).click();
    const dialog = page.getByRole("dialog", { name: `Roll back ${REPOSITORY} in ${ENV}?` });
    await expect(dialog.getByRole("button", { name: "Roll back" })).toBeVisible();
    for (const button of await dialog.getByRole("button").all()) {
      await button.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(button).toBeFocused();
      expect(
        await button.evaluate((node) => {
          const style = getComputedStyle(node);
          return style.outlineStyle !== "none" || style.boxShadow !== "none";
        }),
        "the dialog's buttons draw a ring",
      ).toBe(true);
    }
  });

  test("Home loads none of the Jobs chunks, and each loads when its page or dialog first opens", async ({
    page,
  }) => {
    const scripts: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "script") {
        scripts.push(new URL(request.url()).pathname);
      }
    });
    const loaded = (name: string): number =>
      scripts.filter((path) => new RegExp(`/${name}-[^/]+\\.js$`, "u").test(path)).length;
    const chunks = ["JobsScreen", "JobsList", "RunsTab", "DeploysTab", "RollbackDialog"];

    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    expect(chunks.map(loaded), "Home").toEqual([0, 0, 0, 0, 0]);

    const views = page.getByRole("navigation", { name: "Jobs views" });
    await page
      .getByRole("navigation", { name: "Areas" })
      .getByRole("link", { name: "Jobs" })
      .click();
    await expect(page.getByRole("region", { name: "Every job listed" })).toBeVisible();
    expect(chunks.map(loaded), "the list").toEqual([1, 1, 0, 0, 0]);

    await views.getByRole("link", { name: "Runs" }).click();
    await expect(page.getByRole("region", { name: "Runs listed" })).toBeVisible();
    expect(chunks.map(loaded), "the Runs tab").toEqual([1, 1, 1, 0, 0]);

    await views.getByRole("link", { name: "Deploys" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Deploys" })).toBeVisible();
    expect(chunks.map(loaded), "the Deploys tab").toEqual([1, 1, 1, 1, 0]);

    await page.goto(`/jobs/deploys?environment=${ENV}`);
    scripts.length = 0;
    await page.getByRole("button", { name: ROLL_BACK }).click();
    await expect(page.getByRole("dialog").getByRole("button", { name: "Roll back" })).toBeVisible();
    expect(loaded("RollbackDialog"), "the rollback dialog").toBe(1);
  });
});
