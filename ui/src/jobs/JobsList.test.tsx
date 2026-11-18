import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { preloadJobsScreen } from "../test/jobs";
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
});
