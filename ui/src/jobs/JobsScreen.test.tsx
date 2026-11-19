import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { jobsListRoutes } from "../test/jobsList";
import { renderShell } from "../test/render";

describe("the Jobs screen", () => {
  it("/jobs shows the list of jobs and runs", async () => {
    renderShell("/jobs", { routes: jobsListRoutes() });

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { level: 2, name: /^Running now/u }),
    ).toBeInTheDocument();
  });

  it("routes a job, a run and a task to their pages", async () => {
    const { router } = renderShell("/jobs/prod/job/team%2Fanalytics/nightly");
    expect(await screen.findByRole("heading", { level: 1, name: "nightly" })).toBeInTheDocument();

    await router.navigate("/jobs/prod/runs/r1");
    expect(await screen.findByRole("heading", { level: 1, name: "Run" })).toBeInTheDocument();

    await router.navigate("/jobs/prod/runs/r1/tasks/silver.sales");
    expect(
      await screen.findByRole("heading", { level: 1, name: "silver.sales" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run" })).toHaveAttribute("href", "/jobs/prod/runs/r1");
  });

  it("shows the tabs Jobs, Runs and Deploys on the tab pages and not on a detail page", async () => {
    const { router } = renderShell("/jobs", { routes: jobsListRoutes() });
    const tabs = await screen.findByRole("navigation", { name: "Jobs views" });
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Jobs", "Runs", "Deploys"]);
    expect(within(tabs).getByRole("link", { name: "Jobs" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await router.navigate("/jobs/runs");
    expect(await screen.findByRole("heading", { level: 1, name: "Runs" })).toBeInTheDocument();
    expect(document.title).toBe("Runs · Lakana");
    expect(
      within(screen.getByRole("navigation", { name: "Jobs views" })).getByRole("link", {
        name: "Runs",
      }),
    ).toHaveAttribute("aria-current", "page");

    await router.navigate("/jobs/deploys");
    expect(await screen.findByRole("heading", { level: 1, name: "Deploys" })).toBeInTheDocument();

    await router.navigate("/jobs/prod/runs/r1");
    expect(await screen.findByRole("heading", { level: 1, name: "Run" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Jobs views" })).toBeNull();
  });

  it("each tab keeps the environment", async () => {
    renderShell("/jobs/runs?environment=prod&status=failed", { routes: jobsListRoutes() });

    const tabs = await screen.findByRole("navigation", { name: "Jobs views" });
    expect(within(tabs).getByRole("link", { name: "Jobs" })).toHaveAttribute(
      "href",
      "/jobs?environment=prod",
    );
    expect(within(tabs).getByRole("link", { name: "Runs" })).toHaveAttribute(
      "href",
      "/jobs/runs?environment=prod",
    );
    expect(within(tabs).getByRole("link", { name: "Deploys" })).toHaveAttribute(
      "href",
      "/jobs/deploys?environment=prod",
    );
  });

  it("the Runs tab opens on the environment the Jobs list was filtered by, and All sticks", async () => {
    const { user, router } = renderShell("/jobs", { routes: jobsListRoutes() });
    await user.click(await screen.findByRole("button", { name: "Environment: All environments" }));
    await user.click(screen.getByRole("option", { name: /prod/u }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?environment=prod");
    });

    // A bare address of the tab, as a bookmark or the sidebar opens it, takes the remembered one
    // in its own place: Back returns to the list.
    await router.navigate("/jobs/runs");
    // The tab's code is loaded on demand, the first time.
    expect(
      await screen.findByRole("heading", { level: 1, name: "Runs" }, { timeout: 5_000 }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Environment: prod, protected" }),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe("?environment=prod");
    await router.navigate(-1);
    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/jobs");

    // All environments chosen on the tab is remembered: the list and the tab open on all.
    await router.navigate("/jobs/runs");
    await screen.findByRole("heading", { level: 1, name: "Runs" });
    await user.click(await screen.findByRole("button", { name: "Environment: prod, protected" }));
    await user.click(screen.getByRole("option", { name: "All environments" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("all");
    await router.navigate("/jobs");
    await screen.findByRole("heading", { level: 1, name: "Jobs" });
    expect(
      await screen.findByRole("button", { name: "Environment: All environments" }),
    ).toBeInTheDocument();
    await router.navigate("/jobs/runs");
    await screen.findByRole("heading", { level: 1, name: "Runs" });
    expect(
      await screen.findByRole("button", { name: "Environment: All environments" }),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
  });

  it("an unknown path under /jobs is not found", async () => {
    renderShell("/jobs/nothing");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });
});
