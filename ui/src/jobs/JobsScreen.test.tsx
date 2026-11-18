import { screen } from "@testing-library/react";
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

  it("an unknown path under /jobs is not found", async () => {
    renderShell("/jobs/runs");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });
});
