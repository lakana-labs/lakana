import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { aFailedRun, aJob, aJobRun, preloadJobsScreen } from "../test/jobs";
import { jobsListRoutes } from "../test/jobsList";
import { renderShell } from "../test/render";

beforeAll(preloadJobsScreen);

afterEach(() => {
  window.localStorage.clear();
});

describe("JobsList", () => {
  it("has the heading Jobs, no breadcrumb, the filter and the search", async () => {
    renderShell("/jobs", { routes: jobsListRoutes() });

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(
      await screen.findByRole("button", { name: "Environment: All environments" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search jobs" })).toBeInTheDocument();
    expect(document.querySelectorAll(".button-primary")).toHaveLength(0);
  });

  it("shows Running now, then Failed, then All jobs", async () => {
    renderShell("/jobs", { routes: jobsListRoutes() });

    await screen.findByRole("heading", { level: 2, name: /^All jobs/u });
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Running now (0)", "Failed, needs attention (0)", "All jobs"]);
  });

  it("says how to get access when no environment is listed", async () => {
    const { server } = renderShell("/jobs", {
      routes: { ...jobsListRoutes(), "GET /api/v1/environments": { status: 200, body: [] } },
    });

    expect(
      await screen.findByText(
        "You may not read any environment yet. Ask an administrator for a role in one.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana job list --env <environment>")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/runs"))).toBe(false);
  });

  it("says why the environments could not be read, with Try again", async () => {
    const { user, server } = renderShell("/jobs", {
      routes: {
        ...jobsListRoutes(),
        "GET /api/v1/environments": { status: 500, body: { detail: "The catalog is down." } },
      },
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/./u);
    server.route(jobsListRoutes());
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", { level: 2, name: /^Running now/u }),
    ).toBeInTheDocument();
  });

  it("the filter chosen is remembered and named in the address", async () => {
    const { user, router } = renderShell("/jobs", { routes: jobsListRoutes() });

    await user.click(await screen.findByRole("button", { name: "Environment: All environments" }));
    await user.click(screen.getByRole("option", { name: /prod/u }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("?environment=prod");
    });
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("prod");
    expect(
      screen.getByRole("button", { name: "Environment: prod, protected" }),
    ).toBeInTheDocument();
    expect(within(document.body).queryByRole("listbox")).toBeNull();
  });

  it("the search narrows all three sections", async () => {
    const weekly = aJobRun({
      id: "r-weekly",
      job: "weekly_report",
      repository: "finance",
      status: "running",
      tasks: [],
      finished_at: null,
    });
    const { user, router } = renderShell("/jobs", {
      routes: jobsListRoutes({
        running: [aJobRun({ status: "running", tasks: [], finished_at: null }), weekly],
        failed: [aFailedRun()],
        prod: [aJob(), aJob({ name: "weekly_report", repository: "finance" })],
      }),
    });

    await screen.findByRole("heading", { level: 2, name: "Running now (2)" });
    await user.type(screen.getByRole("searchbox", { name: "Search jobs" }), "week");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Running now (1)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Failed, needs attention (0)" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Nothing here matches the search.")).toBeInTheDocument();
    const jobs = screen.getByRole("region", { name: "Every job listed" });
    expect(
      within(jobs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["weekly_report"]);
    expect(router.state.location.search).toBe("?q=week");
  });

  it("an action reads the sections again", async () => {
    const { user, server } = renderShell("/jobs", {
      routes: {
        ...jobsListRoutes({ prod: [aJob()] }),
        "POST /api/v1/environments/prod/jobs/nightly_sales/runs": {
          status: 201,
          body: { run: aJobRun({ id: "r-new", status: "queued" }), waiting: null },
        },
      },
    });

    const run = await screen.findByRole("button", { name: "Run nightly_sales" });
    const reads = (path: string): number =>
      server.sent.filter((request) => request.path.split("?")[0] === path).length;
    await waitFor(() => {
      expect(reads("/api/v1/runs")).toBe(1);
    });
    await user.click(run);

    await waitFor(() => {
      expect(reads("/api/v1/runs")).toBe(2);
    });
    expect(reads("/api/v1/runs/failed")).toBe(2);
    expect(reads("/api/v1/environments/prod/jobs")).toBe(2);
    expect(reads("/api/v1/environments/dev/jobs")).toBe(2);
  });

  it("a new filter drops the previous filter's rows at once and reads only the chosen environment", async () => {
    const devRun = aJobRun({
      id: "r-dev",
      environment: "dev",
      job: "orders_hourly",
      status: "running",
      tasks: [],
      finished_at: null,
      actions: ["cancel"],
    });
    const devFailed = aFailedRun({
      run: { ...aFailedRun().run, id: "f-dev", environment: "dev", job: "orders_hourly" },
    });
    const routes = jobsListRoutes({
      running: [devRun],
      failed: [devFailed],
      dev: [aJob({ name: "orders_hourly", environment: "dev" })],
    });
    let chosen = false;
    const prodHeld = (request: { path: string }) =>
      request.path.includes("environment=prod") ? new Promise<never>(() => undefined) : undefined;
    const { user, server } = renderShell("/jobs", {
      routes: {
        ...routes,
        "GET /api/v1/runs": (request) =>
          prodHeld(request) ?? { status: 200, body: { items: [devRun], next_cursor: null } },
        "GET /api/v1/runs/failed": (request) =>
          prodHeld(request) ?? { status: 200, body: { items: [devFailed], truncated: false } },
        // Answered while all environments are shown, held once prod is chosen.
        "GET /api/v1/environments/prod/jobs": () =>
          chosen ? new Promise<never>(() => undefined) : { status: 200, body: [] },
      },
    });

    await screen.findByRole("heading", { level: 2, name: "Running now (1)" });
    await waitFor(() => {
      expect(screen.getAllByRole("link", { name: "orders_hourly" })).toHaveLength(3);
    });
    const before = server.sent.length;
    chosen = true;

    await user.click(screen.getByRole("button", { name: "Environment: All environments" }));
    await user.click(screen.getByRole("option", { name: /prod/u }));

    // While the chosen environment is read, nothing of the previous filter shows: no row, no
    // count and no control.
    await waitFor(() => {
      expect(screen.queryAllByRole("link", { name: "orders_hourly" })).toHaveLength(0);
    });
    expect(screen.queryByRole("button", { name: /orders_hourly/u })).toBeNull();
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Running now (0)", "Failed, needs attention (0)", "All jobs"]);
    expect(screen.queryByRole("region", { name: "Runs running now" })).toBeNull();
    await waitFor(() => {
      expect(server.sent.length).toBeGreaterThanOrEqual(before + 3);
    });
    const after = server.sent.slice(before).map((request) => request.path);
    expect(after).toContain("/api/v1/runs?environment=prod&status=queued%2Crunning&limit=50");
    expect(after).toContain("/api/v1/runs/failed?environment=prod");
    expect(after).toContain("/api/v1/environments/prod/jobs");
    expect(after.some((path) => path.includes("dev"))).toBe(false);
  });

  it("a failed first read under a new filter says so at once", async () => {
    const { user } = renderShell("/jobs", {
      routes: {
        ...jobsListRoutes({
          running: [aJobRun({ status: "running", tasks: [], finished_at: null })],
        }),
        "GET /api/v1/runs": (request) =>
          request.path.includes("environment=dev")
            ? { status: 503, body: { detail: "The server is starting." } }
            : {
                status: 200,
                body: {
                  items: [aJobRun({ status: "running", tasks: [], finished_at: null })],
                  next_cursor: null,
                },
              },
      },
    });

    await screen.findByRole("heading", { level: 2, name: "Running now (1)" });
    await user.click(screen.getByRole("button", { name: "Environment: All environments" }));
    await user.click(screen.getByRole("option", { name: "dev" }));

    expect(await screen.findByText("The server is starting.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "nightly_sales" })).toBeNull();
  });

  it("names no forgotten environment in any request", async () => {
    window.localStorage.setItem("lakana.jobs.environment", "staging");
    const { server } = renderShell("/jobs", { routes: jobsListRoutes() });

    expect(
      await screen.findByRole("button", { name: "Environment: All environments" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(server.sent.some((request) => request.path.startsWith("/api/v1/runs/failed"))).toBe(
        true,
      );
    });
    await screen.findByRole("region", { name: "Every job listed" }).catch(() => undefined);
    expect(server.sent.filter((request) => request.path.includes("staging"))).toEqual([]);
    expect(server.sent.map((request) => request.path)).toContain("/api/v1/runs/failed");
  });
});
