import { screen, within } from "@testing-library/react";
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

  it("an unknown path under /jobs is not found", async () => {
    renderShell("/jobs/nothing");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });
});
