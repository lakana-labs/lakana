import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { FailedRunOut } from "../api/jobs";
import { aFailedRun, aFailure, aJobRun } from "../test/jobs";
import { type FakeRoute, fakeServer } from "../test/server";
import { FailedRuns } from "./FailedRuns";

const FAILED = "GET /api/v1/runs/failed";

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * The failed runs as the server answers them.
 *
 * @param items - The rows.
 * @param truncated - Whether the server cut the list.
 * @returns The route.
 */
function failedRoute(items: readonly FailedRunOut[], truncated = false): FakeRoute {
  return { status: 200, body: { items, truncated } };
}

/** A failed run of no job of the repository `finance` in `dev`, which failed to build. */
const OF_NO_JOB = aFailedRun({
  run: aJobRun({
    id: "r-adhoc",
    environment: "dev",
    repository: "finance",
    job: null,
    trigger: null,
    status: "failed",
    tasks: [],
    failed_tasks: 0,
    total_tasks: 0,
    ended_tasks: 0,
    error_message: "The project could not be built: lakana.yaml line 4.",
    finished_at: "2026-11-17T03:00:00Z",
    actions: ["cancel", "resume"],
  }),
  task: null,
  failure: null,
});

/**
 * Renders the section over a server.
 *
 * @param route - What the failed runs answer.
 * @param environment - The environment chosen.
 * @param query - The search.
 * @returns The server, the change handler and a user-event session.
 */
function renderFailed(route: FakeRoute, environment: string | null = null, query = "") {
  const server = fakeServer({ [FAILED]: route });
  const onChanged = vi.fn();
  render(
    <MemoryRouter>
      <FailedRuns environment={environment} query={query} reloadKey={0} onChanged={onChanged} />
    </MemoryRouter>,
  );
  return { server, onChanged, user: userEvent.setup() };
}

/**
 * The body rows of the section's table.
 *
 * @returns The rows.
 */
function bodyRows(): HTMLElement[] {
  return within(screen.getByRole("region", { name: "Runs that failed" }))
    .getAllByRole("row")
    .slice(1);
}

describe("FailedRuns", () => {
  it("shows exactly the servers failed runs in its order", async () => {
    const newer = aFailedRun({
      run: { ...aFailedRun().run, id: "r-weekly", job: "weekly_report" },
    });
    const { server } = renderFailed(failedRoute([newer, aFailedRun(), OF_NO_JOB]), "prod");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Failed, needs attention (3)" }),
    ).toBeInTheDocument();
    expect(bodyRows().map((row) => within(row).getAllByRole("link")[0]?.textContent)).toEqual([
      "weekly_report",
      "nightly_sales",
      "finance",
    ]);
    expect(server.sent[0]?.path).toBe("/api/v1/runs/failed?environment=prod");
  });

  it("shows the code, the cause and the next step on the row", async () => {
    renderFailed(failedRoute([aFailedRun({ failure: aFailure("build_failed") })]));

    await screen.findByRole("region", { name: "Runs that failed" });
    const [row] = bodyRows();
    if (row === undefined) {
      throw new Error("a row expected");
    }
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(row).toHaveTextContent(
      "build_failed (opens in a new tab): The task failed with build_failed. — Fix the cause and resume the run.",
    );
    expect(row).toHaveTextContent(when("2026-11-17T01:14:00Z"));
    expect(row).toHaveTextContent("Environment prod");
    expect(within(row).getByRole("link", { name: /^build_failed/u })).toHaveAttribute(
      "href",
      expect.stringContaining("reference/why-did-it-fail/"),
    );
  });

  it("shows a run's own message when no task failed", async () => {
    renderFailed(failedRoute([OF_NO_JOB]));

    expect(
      await screen.findByText("The project could not be built: lakana.yaml line 4."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "finance" })).toHaveAttribute(
      "href",
      "/jobs/dev/runs/r-adhoc",
    );
  });

  it("says the reference's internal cause when the run has neither a failed task nor a message", async () => {
    renderFailed(
      failedRoute([
        aFailedRun({ run: { ...OF_NO_JOB.run, error_message: null }, task: null, failure: null }),
      ]),
    );

    await screen.findByRole("region", { name: "Runs that failed" });
    const [row] = bodyRows();
    expect(row).toHaveTextContent(
      "internal (opens in a new tab): The platform failed in a way it did not expect. — Open the run;",
    );
  });

  it("Logs opens the failing task, or the run without one", async () => {
    renderFailed(failedRoute([aFailedRun(), OF_NO_JOB]));

    expect(
      await screen.findByRole("link", { name: "Logs of the run of nightly_sales" }),
    ).toHaveAttribute("href", `/jobs/prod/runs/${aFailedRun().run.id}/tasks/silver.orders`);
    expect(screen.getByRole("link", { name: "Logs of the run of finance" })).toHaveAttribute(
      "href",
      "/jobs/dev/runs/r-adhoc",
    );
    expect(screen.getByRole("link", { name: "Logs of the run of finance" })).toHaveTextContent(
      "Logs",
    );
  });

  it("offers Resume only when the actions list it", async () => {
    const readOnly = aFailedRun({
      run: { ...aFailedRun().run, id: "r-read", job: "weekly_report", actions: [] },
    });
    const { server, onChanged, user } = renderFailed(failedRoute([aFailedRun(), readOnly]));
    server.route({
      [`POST /api/v1/environments/prod/runs/${aFailedRun().run.id}/resume`]: {
        status: 201,
        body: aJobRun({ id: "r-new", status: "queued" }),
      },
    });

    const resume = await screen.findByRole("button", { name: "Resume the run of nightly_sales" });
    expect(resume).toHaveTextContent("Resume");
    expect(screen.queryByRole("button", { name: "Resume the run of weekly_report" })).toBeNull();
    await user.click(resume);
    expect(await screen.findByText("Resumed the failed part in a new run.")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("offers no Resume for a run that failed without a failed task", async () => {
    renderFailed(failedRoute([OF_NO_JOB]));

    await screen.findByRole("link", { name: "finance" });
    expect(screen.queryByRole("button", { name: /^Resume/u })).toBeNull();
  });

  it("says when the server cut the list", async () => {
    renderFailed(failedRoute([aFailedRun()], true));

    expect(
      await screen.findByText("More failed runs than shown; choose an environment."),
    ).toBeInTheDocument();
  });

  it("says no job failed in the last 7 days", async () => {
    const { server } = renderFailed(failedRoute([]));

    expect(await screen.findByText("No job failed in the last 7 days.")).toBeInTheDocument();
    expect(server.sent[0]?.path).toBe("/api/v1/runs/failed");
  });

  it("narrows the rows by the search only", async () => {
    renderFailed(failedRoute([aFailedRun(), OF_NO_JOB]), null, "fin");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Failed, needs attention (1)" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "finance" })).toBeInTheDocument();
  });
});
