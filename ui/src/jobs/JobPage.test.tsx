import { screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { aJob, aJobRun, aTask } from "../test/jobs";
import type { FakeRoute } from "../test/server";
import { renderShell } from "../test/render";

const ENV = "/api/v1/environments/prod";
const PAGE = "/jobs/prod/job/analytics/nightly_sales";
const NEWEST = aJobRun({ id: "r2", created_at: "2026-11-17T01:00:00Z" });
const OLDER = aJobRun({ id: "r1", created_at: "2026-11-16T01:00:00Z" });

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * The server of a job with two runs.
 *
 * @param more - Routes beyond them.
 * @returns The routes.
 */
function jobRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    [`GET ${ENV}/jobs/nightly_sales`]: { status: 200, body: aJob() },
    [`GET ${ENV}/runs`]: {
      status: 200,
      body: [NEWEST, OLDER].map((run) => ({ ...run, tasks: [] })),
    },
    [`GET ${ENV}/runs/r2`]: { status: 200, body: NEWEST },
    [`GET ${ENV}/runs/r1`]: { status: 200, body: OLDER },
    ...more,
  };
}

describe("JobPage", () => {
  it("has the breadcrumb Jobs › env › job and Run now as its one primary action", async () => {
    renderShell(PAGE, { routes: jobRoutes() });

    expect(
      await screen.findByRole("heading", { level: 1, name: "nightly_sales" }),
    ).toBeInTheDocument();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/jobs");
    expect(within(trail).getByRole("link", { name: "prod" })).toHaveAttribute("href", "/jobs");
    const runNow = await screen.findByRole("button", { name: "Run now" });
    expect(document.querySelectorAll(".button-primary")).toEqual(
      expect.objectContaining({ length: 1 }),
    );
    expect(runNow).toHaveClass("button-primary");
    expect(screen.getByRole("button", { name: "Pause schedule" })).not.toHaveClass(
      "button-primary",
    );
  });

  it("selects the run in the address, else the newest", async () => {
    const first = renderShell(PAGE, { routes: jobRoutes() });

    expect(
      await screen.findByRole("heading", { level: 2, name: `Run of ${when(NEWEST.created_at)}` }),
    ).toBeInTheDocument();

    await first.router.navigate(`${PAGE}?run=r1`);
    expect(
      await screen.findByRole("heading", { level: 2, name: `Run of ${when(OLDER.created_at)}` }),
    ).toBeInTheDocument();
    expect(first.server.sent.some((request) => request.path === `${ENV}/runs/r1`)).toBe(true);
  });

  it("after Run now the new run is selected and the focus stays on Run now", async () => {
    const started = aJobRun({ id: "r3", status: "queued", created_at: "2026-11-17T09:00:00Z" });
    const { router, user } = renderShell(PAGE, {
      routes: jobRoutes({
        [`POST ${ENV}/jobs/nightly_sales/runs`]: {
          status: 201,
          body: { run: started, waiting: null },
        },
        [`GET ${ENV}/runs/r3`]: { status: 200, body: started },
      }),
    });
    const runNow = await screen.findByRole("button", { name: "Run now" });

    await user.click(runNow);

    expect(await screen.findByText("Started a run of nightly_sales.")).toHaveAttribute(
      "role",
      "status",
    );
    await waitFor(() => {
      expect(router.state.location.search).toBe("?run=r3");
    });
    expect(
      await screen.findByRole("heading", { level: 2, name: `Run of ${when(started.created_at)}` }),
    ).toBeInTheDocument();
    expect(runNow).toHaveFocus();
  });

  it("says a waiting start with the run it waits for", async () => {
    const { user } = renderShell(PAGE, {
      routes: jobRoutes({
        [`POST ${ENV}/jobs/nightly_sales/runs`]: {
          status: 202,
          body: {
            run: null,
            waiting: { waits_for_run_id: "r2", message: "A run of nightly_sales is running." },
          },
        },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Run now" }));

    expect(
      await screen.findByText("The start waits: A run of nightly_sales is running."),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open the run it waits for" })[0]).toHaveAttribute(
      "href",
      "/jobs/prod/runs/r2",
    );
  });

  it("says nothing of a start answered without a run or a wait", async () => {
    const { user } = renderShell(PAGE, {
      routes: jobRoutes({
        [`POST ${ENV}/jobs/nightly_sales/runs`]: {
          status: 202,
          body: { run: null, waiting: null },
        },
      }),
    });
    const runNow = await screen.findByRole("button", { name: "Run now" });

    await user.click(runNow);

    await waitFor(() => {
      expect(runNow).not.toHaveAttribute("data-pending");
    });
    expect(document.querySelector(".page > .jobs-note")).toBeEmptyDOMElement();
  });

  it("says that the schedule is paused, and shows Resume schedule", async () => {
    const { user } = renderShell(PAGE, {
      routes: jobRoutes({
        [`POST ${ENV}/jobs/nightly_sales/pause`]: {
          status: 200,
          body: aJob({ paused: true, state: "paused", next_run_at: null }),
        },
        [`POST ${ENV}/jobs/nightly_sales/resume`]: { status: 200, body: aJob() },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Pause schedule" }));
    const confirm = within(screen.getByRole("dialog")).getByRole("button", {
      name: "Pause schedule",
    });
    await user.click(confirm);

    expect(await screen.findByText("Paused the schedule of nightly_sales.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume schedule" }));
    expect(await screen.findByText("Resumed the schedule of nightly_sales.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause schedule" })).toBeInTheDocument();
  });

  it("Resume selects the new run and gives the focus to its heading", async () => {
    const failed = aJobRun({
      id: "r2",
      status: "failed",
      failed_tasks: 1,
      actions: ["resume"],
      tasks: [aTask({ status: "failed" })],
    });
    const next = aJobRun({ id: "r9", status: "queued", created_at: "2026-11-17T09:30:00Z" });
    const { router, user } = renderShell(PAGE, {
      routes: jobRoutes({
        [`GET ${ENV}/runs/r2`]: { status: 200, body: failed },
        [`POST ${ENV}/runs/r2/resume`]: { status: 201, body: next },
        [`GET ${ENV}/runs/r9`]: { status: 200, body: next },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Resume" }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("?run=r9");
    });
    const heading = await screen.findByRole("heading", {
      level: 2,
      name: `Run of ${when(next.created_at)}`,
    });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(await screen.findByText("Resumed the failed part in a new run.")).toBeInTheDocument();
  });

  it("records the opened job for Home, and shows the job when that fails", async () => {
    const { server } = renderShell(PAGE, {
      routes: jobRoutes({ "POST /api/v1/principals/admin/recent": { status: 500 } }),
    });

    await screen.findByRole("button", { name: "Run now" });
    await waitFor(() => {
      expect(
        server.sent.filter((request) => request.path === "/api/v1/principals/admin/recent"),
      ).toHaveLength(1);
    });
    const record = server.sent.find(
      (request) => request.path === "/api/v1/principals/admin/recent",
    );
    expect(record?.body).toEqual({
      kind: "job",
      environment: "prod",
      ref: "analytics/nightly_sales",
    });
  });

  it("a job that no longer exists says so with the servers sentence", async () => {
    const { server } = renderShell(PAGE, {
      routes: {
        [`GET ${ENV}/jobs/nightly_sales`]: {
          status: 404,
          body: {
            type: "NotFoundError",
            detail: "No job nightly_\u202Esales is deployed in prod.",
          },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No job nightly_U+202Esales is deployed in prod.",
    );
    expect(screen.getByRole("heading", { level: 1, name: "nightly_sales" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
    expect(screen.getByRole("link", { name: "Go to Jobs" })).toHaveAttribute("href", "/jobs");
    expect(server.sent.some((request) => request.path.endsWith("/recent"))).toBe(false);
  });

  it("the facts, the runs and the run wrap at 320 px", async () => {
    renderShell(PAGE, { routes: jobRoutes() });

    await screen.findByRole("heading", { level: 2, name: `Run of ${when(NEWEST.created_at)}` });
    const panels = document.querySelectorAll(".jobs-panels > .flat-panel");
    expect(panels).toHaveLength(3);
    expect(document.querySelector(".jobs-head-actions")).not.toBeNull();
    expect(document.querySelectorAll(".jobs-panels .list-scroll")).toHaveLength(2);
  });
});
