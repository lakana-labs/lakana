import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { JobOut } from "../api/jobs";
import { aJob } from "../test/jobs";
import { type FakeRoute, fakeServer } from "../test/server";
import { expectServerSorts } from "../test/sorting";
import { AllJobs } from "./AllJobs";

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * Shows where the router is, so a test can read it.
 *
 * @returns The location as text.
 */
function Where() {
  const location = useLocation();
  return <output aria-label="where">{location.pathname}</output>;
}

/**
 * The route of an environment's jobs.
 *
 * @param environment - The environment.
 * @returns The route's key.
 */
const jobsOf = (environment: string): string => `GET /api/v1/environments/${environment}/jobs`;

/**
 * Renders All jobs over a server.
 *
 * @param environments - The environments shown.
 * @param routes - What the server answers.
 * @param query - The search.
 * @returns The server, the change handler and a user-event session.
 */
function renderAll(environments: readonly string[], routes: Record<string, FakeRoute>, query = "") {
  const server = fakeServer(routes);
  const onChanged = vi.fn();
  render(
    <MemoryRouter initialEntries={["/jobs"]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <AllJobs
                environments={environments}
                query={query}
                reloadKey={0}
                onChanged={onChanged}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return { server, onChanged, user: userEvent.setup() };
}

/**
 * The names in the rows of the section's table, in order.
 *
 * @returns The names, each with its environment.
 */
function names(): string[] {
  return within(screen.getByRole("region", { name: "Every job listed" }))
    .getAllByRole("row")
    .slice(1)
    .map((row) => {
      const cells = within(row).getAllByRole("cell");
      return `${cells[0]?.textContent ?? ""} ${cells[1]?.textContent ?? ""} ${cells[2]?.textContent ?? ""}`;
    });
}

/**
 * The row of a job.
 *
 * @param job - The job's name, as its link reads.
 * @returns The row.
 */
function rowOf(job: string): HTMLElement {
  const row = screen.getByRole("link", { name: job }).closest("tr");
  if (row === null) {
    throw new Error(`no row for ${job}`);
  }
  return row;
}

const BY_HAND: Partial<JobOut> = {
  cron: null,
  timezone: null,
  next_run_at: null,
  triggers: [{ kind: "on_demand" }],
};

describe("AllJobs", () => {
  it("lists jobs of every environment sorted by name, repository and environment", async () => {
    renderAll(["dev", "prod"], {
      [jobsOf("dev")]: {
        status: 200,
        body: [aJob({ environment: "dev" }), aJob({ name: "audit", environment: "dev" })],
      },
      [jobsOf("prod")]: {
        status: 200,
        body: [aJob(), aJob({ repository: "accounts" })],
      },
    });

    await screen.findByRole("region", { name: "Every job listed" });
    expect(names()).toEqual([
      "audit analytics Environment dev",
      "nightly_sales accounts Environment prod",
      "nightly_sales analytics Environment dev",
      "nightly_sales analytics Environment prod",
    ]);
    expect(screen.getAllByRole("link", { name: "nightly_sales" })[0]).toHaveAttribute(
      "href",
      "/jobs/prod/job/accounts/nightly_sales",
    );
  });

  it("shows the schedule in words, paused in words, and by hand without one", async () => {
    renderAll(["prod"], {
      [jobsOf("prod")]: {
        status: 200,
        body: [
          aJob(),
          aJob({ name: "weekly_report", paused: true, next_run_at: null }),
          aJob({ name: "backfill", ...BY_HAND }),
          aJob({ name: "odd", cron: "*/5 1-3 * * *" }),
        ],
      },
    });

    await screen.findByRole("region", { name: "Every job listed" });
    expect(rowOf("nightly_sales")).toHaveTextContent("daily 02:00 Europe/Berlin");
    expect(rowOf("nightly_sales")).toHaveTextContent(when("2026-11-18T01:00:00Z"));
    expect(rowOf("weekly_report")).toHaveTextContent("daily 02:00 Europe/Berlin · paused");
    expect(rowOf("weekly_report")).not.toHaveTextContent(when("2026-11-18T01:00:00Z"));
    expect(rowOf("backfill")).toHaveTextContent("by hand");
    expect(within(rowOf("odd")).getByText("*/5 1-3 * * * Europe/Berlin")).toHaveClass(
      "jobs-verbatim",
    );
  });

  it("says who paused a schedule and since when on hover and to a screen reader", async () => {
    renderAll(["prod"], {
      [jobsOf("prod")]: {
        status: 200,
        body: [
          aJob({
            paused: true,
            state: "paused",
            next_run_at: null,
            paused_by: "maria.\u202Ekeller",
            paused_at: "2026-11-16T08:30:00Z",
          }),
          aJob({
            name: "orphaned",
            paused: true,
            state: "paused",
            next_run_at: null,
            paused_by: null,
            paused_at: "2026-11-16T08:30:00Z",
          }),
        ],
      },
    });

    await screen.findByRole("region", { name: "Every job listed" });
    const sentence = `Paused since ${when("2026-11-16T08:30:00Z")} by maria.U+202Ekeller.`;
    const schedule = within(rowOf("nightly_sales")).getAllByRole("cell")[3] as HTMLElement;
    expect(schedule).toHaveTextContent("daily 02:00 Europe/Berlin · paused");
    const word = schedule.querySelector("[data-tip]");
    expect(word).toHaveAttribute("data-tip", sentence);
    // The element that takes the focus reads the sentence whole, as the Waiting pill does.
    expect(word).toHaveTextContent(`paused ${sentence}`);
    expect(word?.querySelector(".visually-hidden")).toHaveTextContent(sentence);
    const orphaned = within(rowOf("orphaned")).getAllByRole("cell")[3] as HTMLElement;
    expect(orphaned.querySelector("[data-tip]")).toHaveAttribute(
      "data-tip",
      `Paused since ${when("2026-11-16T08:30:00Z")} by an account since deleted.`,
    );
  });

  it("shows the last run with glyph and word, and never without one", async () => {
    renderAll(["prod"], {
      [jobsOf("prod")]: {
        status: 200,
        body: [
          aJob({ last_run_status: "failed" }),
          aJob({ name: "fresh", last_run_id: null, last_run_status: null, last_run_at: null }),
        ],
      },
    });

    await screen.findByRole("region", { name: "Every job listed" });
    const last = within(rowOf("nightly_sales")).getAllByRole("cell")[4] as HTMLElement;
    expect(within(last).getByText("Failed")).toBeInTheDocument();
    expect(last.querySelector("svg")).not.toBeNull();
    expect(last).toHaveTextContent(when("2026-11-17T01:00:00Z"));
    expect(within(rowOf("fresh")).getAllByRole("cell")[4]).toHaveTextContent("never");
  });

  it("shows Waiting as the next run of a job whose start waits, with why on hover (#786)", async () => {
    const message = "A start of this job waits for run 1b2c3d4e of this project to finish.";
    const { user } = renderAll(["prod"], {
      [jobsOf("prod")]: {
        status: 200,
        body: [
          aJob({
            state: "waiting",
            waiting_for: { code: "job_waits", message },
            waits_for_run_id: "1b2c3d4e",
            waiting_since: "2026-11-17T09:12:00Z",
            waiting_trigger: "manual",
          }),
          aJob({
            name: "about_to_start",
            state: "waiting to start",
            waiting_for: { code: "job_starts_soon", message: "It begins within a few seconds." },
          }),
          aJob({ name: "fresh", last_run_id: null, last_run_status: null, last_run_at: null }),
        ],
      },
    });

    await screen.findByRole("region", { name: "Every job listed" });
    const cells = within(rowOf("nightly_sales")).getAllByRole("cell");
    // The last result stays; the next run is the start that waits.
    expect(within(cells[4] as HTMLElement).getByText("Succeeded")).toBeInTheDocument();
    const next = cells[5] as HTMLElement;
    expect(within(next).getByText("Waiting")).toBeInTheDocument();
    expect(next).not.toHaveTextContent(when("2026-11-18T01:00:00Z"));
    expect(next.querySelector("[data-tip]")).toHaveAttribute("data-tip", message);
    expect(next).toHaveTextContent(message);
    const soon = within(rowOf("about_to_start")).getAllByRole("cell")[5] as HTMLElement;
    expect(within(soon).getByText("Waiting")).toBeInTheDocument();
    const fresh = within(rowOf("fresh")).getAllByRole("cell");
    expect(fresh[4]).toHaveTextContent("never");
    expect(fresh[5]).not.toHaveTextContent("Waiting");
    expect(fresh[5]).toHaveTextContent(when("2026-11-18T01:00:00Z"));

    // A click on the pill opens the job, as the rest of the row does.
    await user.click(within(next).getByText("Waiting"));
    expect(screen.getByLabelText("where")).toHaveTextContent(
      "/jobs/prod/job/analytics/nightly_sales",
    );
  });

  it("a click inside a row's dialog never opens the row", async () => {
    const { user } = renderAll(["prod"], { [jobsOf("prod")]: { status: 200, body: [aJob()] } });

    await user.click(
      await screen.findByRole("button", { name: "Pause the schedule of nightly_sales" }),
    );
    const dialog = await screen.findByRole("dialog");
    // The dialog is a portal: React hands its clicks to the row it was opened from.
    await user.click(within(dialog).getByText("Pause the schedule of nightly_sales?"));
    await user.click(document.querySelector(".palette-overlay") as HTMLElement);

    expect(screen.getByLabelText("where")).toHaveTextContent(/^\/jobs$/u);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Run starts the job and Pause asks first, each named by the row", async () => {
    const { server, onChanged, user } = renderAll(["prod"], {
      [jobsOf("prod")]: {
        status: 200,
        body: [aJob(), aJob({ name: "readonly_job", actions: [] })],
      },
      "POST /api/v1/environments/prod/jobs/nightly_sales/runs": {
        status: 201,
        body: { run: { id: "r-new" }, waiting: null },
      },
      "POST /api/v1/environments/prod/jobs/nightly_sales/pause": {
        status: 200,
        body: aJob({ paused: true }),
      },
    });

    const run = await screen.findByRole("button", { name: "Run nightly_sales" });
    expect(run).toHaveTextContent(/^Run$/u);
    expect(run).not.toHaveClass("button-primary");
    expect(within(rowOf("readonly_job")).queryByRole("button")).toBeNull();
    await user.click(run);
    expect(await screen.findByText("Started a run of nightly_sales.")).toBeInTheDocument();
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      repository: "analytics",
    });
    expect(onChanged).toHaveBeenCalledTimes(1);

    const pause = screen.getByRole("button", { name: "Pause the schedule of nightly_sales" });
    expect(pause).toHaveTextContent(/^Pause$/u);
    await user.click(pause);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Pause the schedule of nightly_sales?");
    await user.click(within(dialog).getByRole("button", { name: "Pause schedule" }));
    expect(await screen.findByText("Paused the schedule of nightly_sales.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Resume the schedule of nightly_sales" }),
    ).toHaveTextContent(/^Resume$/u);
    expect(rowOf("nightly_sales")).toHaveTextContent("· paused");
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("says a start that waits, with the run it waits for", async () => {
    const { user } = renderAll(["prod"], {
      [jobsOf("prod")]: { status: 200, body: [aJob()] },
      "POST /api/v1/environments/prod/jobs/nightly_sales/runs": {
        status: 202,
        body: { run: null, waiting: { waits_for_run_id: "r-busy", message: "A run is active." } },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Run nightly_sales" }));
    expect(await screen.findByText(/The start waits: A run is active\./u)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the run it waits for" })).toHaveAttribute(
      "href",
      "/jobs/prod/runs/r-busy",
    );
  });

  it("Resume acts at once on a paused schedule", async () => {
    const { user } = renderAll(["prod"], {
      [jobsOf("prod")]: { status: 200, body: [aJob({ paused: true, next_run_at: null })] },
      "POST /api/v1/environments/prod/jobs/nightly_sales/resume": { status: 200, body: aJob() },
    });

    await user.click(
      await screen.findByRole("button", { name: "Resume the schedule of nightly_sales" }),
    );
    expect(await screen.findByText("Resumed the schedule of nightly_sales.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(rowOf("nightly_sales")).not.toHaveTextContent("· paused");
  });

  it("says nothing of a start answered without a run or a wait", async () => {
    const { onChanged, user } = renderAll(["prod"], {
      [jobsOf("prod")]: { status: 200, body: [aJob()] },
      "POST /api/v1/environments/prod/jobs/nightly_sales/runs": {
        status: 201,
        body: { run: null, waiting: null },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Run nightly_sales" }));
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(screen.queryByText(/Started a run/u)).toBeNull();
  });

  it("a control never opens the row", async () => {
    const { user } = renderAll(["prod"], {
      [jobsOf("prod")]: { status: 200, body: [aJob()] },
      "POST /api/v1/environments/prod/jobs/nightly_sales/runs": {
        status: 201,
        body: { run: { id: "r-new" }, waiting: null },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Run nightly_sales" }));
    await screen.findByText("Started a run of nightly_sales.");
    expect(screen.getByLabelText("where")).toHaveTextContent(/^\/jobs$/u);
    expect(screen.getByRole("button", { name: "Run nightly_sales" })).toHaveFocus();
  });

  it("keeps the other environments when one cannot be read", async () => {
    const { server, user } = renderAll(["dev", "prod"], {
      [jobsOf("dev")]: { status: 200, body: [aJob({ environment: "dev" })] },
      [jobsOf("prod")]: { status: 500, body: { detail: "The catalog is down." } },
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^The jobs of prod could not be read: ./u);
    expect(names()).toEqual(["nightly_sales analytics Environment dev"]);

    server.route({ [jobsOf("prod")]: { status: 200, body: [aJob()] } });
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(names()).toHaveLength(2);
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reads at most four environments at once", async () => {
    const environments = Array.from({ length: 9 }, (_, index) => `env${String(index)}`);
    let open = 0;
    let most = 0;
    const releases: (() => void)[] = [];
    const routes = Object.fromEntries(
      environments.map((environment) => [
        jobsOf(environment),
        () => {
          open += 1;
          most = Math.max(most, open);
          return new Promise<{ status: number; body: unknown }>((resolve) => {
            releases.push(() => {
              open -= 1;
              resolve({ status: 200, body: [aJob({ environment })] });
            });
          });
        },
      ]),
    );
    renderAll(environments, routes);

    for (let answered = 0; answered < environments.length; answered += 1) {
      await waitFor(() => {
        expect(releases.length).toBeGreaterThan(answered);
      });
      await act(async () => {
        releases[answered]?.();
        await Promise.resolve();
      });
    }

    await screen.findByRole("region", { name: "Every job listed" });
    expect(names()).toHaveLength(9);
    expect(most).toBe(4);
  });

  it("says no job is deployed, with the how-to link", async () => {
    renderAll(["dev", "prod"], {
      [jobsOf("dev")]: { status: 200, body: [] },
      [jobsOf("prod")]: { status: 200, body: [] },
    });

    expect(
      await screen.findByText(/^No job is deployed in an environment you may read\./u),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Run a job on a schedule/u })).toHaveAttribute(
      "href",
      expect.stringContaining("how-to/run-a-job-on-a-schedule/"),
    );
  });

  it("says no job is deployed in the one environment chosen", async () => {
    renderAll(["dev"], { [jobsOf("dev")]: { status: 200, body: [] } });

    expect(await screen.findByText("No job is deployed in dev.")).toBeInTheDocument();
  });

  it("marks hidden characters of an environment's name in its sentences", async () => {
    renderAll(["d\u200Bev"], { [jobsOf("d\u200Bev")]: { status: 200, body: [] } });
    expect(await screen.findByText("No job is deployed in dU+200Bev.")).toBeInTheDocument();
  });

  it("marks hidden characters of an environment's name when its jobs cannot be read", async () => {
    renderAll(["pr\u202Eod"], {
      [jobsOf("pr\u202Eod")]: { status: 500, body: { detail: "Down." } },
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^The jobs of prU\+202Eod could not be read: /u);
    expect(alert.textContent).not.toContain("\u202E");
  });

  it("says nothing more when every environment failed", async () => {
    renderAll(["dev"], { [jobsOf("dev")]: { status: 500, body: { detail: "Down." } } });

    expect(await screen.findByRole("alert")).toHaveTextContent(/^The jobs of dev could not/u);
    expect(screen.queryByText(/No job is deployed/u)).toBeNull();
  });

  it("narrows the jobs to the search, by name or repository", async () => {
    renderAll(
      ["prod"],
      {
        [jobsOf("prod")]: {
          status: 200,
          body: [aJob(), aJob({ name: "ledger", repository: "finance" })],
        },
      },
      "Finance",
    );

    await screen.findByRole("region", { name: "Every job listed" });
    expect(names()).toEqual(["ledger finance Environment prod"]);
  });

  it("says nothing matches the search", async () => {
    renderAll(["prod"], { [jobsOf("prod")]: { status: 200, body: [aJob()] } }, "zzz");

    expect(await screen.findByText("Nothing here matches the search.")).toBeInTheDocument();
  });
});

describe("AllJobs' sort", () => {
  it("sorts by repository and environment in each read and in the merge; not by schedule", async () => {
    const { server, user } = renderAll(["dev", "prod"], {
      [jobsOf("dev")]: { status: 200, body: [aJob({ environment: "dev", repository: "zoo" })] },
      [jobsOf("prod")]: { status: 200, body: [aJob({ environment: "prod" })] },
    });
    await screen.findByRole("region", { name: "Every job listed" });
    const region = (): HTMLElement => screen.getByRole("region", { name: "Every job listed" });
    await expectServerSorts(user, region, server.sent, "/api/v1/environments/", [
      ["Repository", "sort=repository&order=asc"],
    ]);
    expect(names()[0]).toBe("nightly_sales analytics Environment prod");
    await expectServerSorts(user, region, server.sent, "/api/v1/environments/", [
      ["Environment", "sort=environment&order=asc"],
    ]);
    expect(within(region()).queryByRole("button", { name: "Schedule" })).toBeNull();
  });

  it("sorts every environment's jobs by the column pressed, a job without a value last", async () => {
    const { server, user } = renderAll(["dev", "prod"], {
      [jobsOf("dev")]: {
        status: 200,
        body: [
          aJob({ name: "audit", environment: "dev", last_run_at: "2026-11-16T01:00:00Z" }),
          aJob({ environment: "dev", last_run_status: null, last_run_at: null }),
        ],
      },
      [jobsOf("prod")]: {
        status: 200,
        body: [aJob({ last_run_at: "2026-11-17T01:00:00Z" })],
      },
    });
    await screen.findByRole("region", { name: "Every job listed" });
    expect(screen.getByRole("columnheader", { name: /Name/u })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );

    await user.click(screen.getByRole("button", { name: /Last run/u }));
    await waitFor(() => {
      expect(names()).toEqual([
        "nightly_sales analytics Environment prod",
        "audit analytics Environment dev",
        "nightly_sales analytics Environment dev",
      ]);
    });
    expect(server.sent.at(-1)?.path).toContain("sort=last_run&order=desc");

    await user.click(screen.getByRole("button", { name: /Last run/u }));
    await waitFor(() => {
      expect(names()[0]).toBe("audit analytics Environment dev");
    });
    // Never run is last in either direction.
    expect(names().at(-1)).toBe("nightly_sales analytics Environment dev");

    await user.click(screen.getByRole("button", { name: /^Name/u }));
    await waitFor(() => {
      expect(names()[0]).toBe("audit analytics Environment dev");
    });
  });
});
