import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

describe("the Jobs screen", () => {
  it("/jobs still shows the area's page with its command line", async () => {
    renderShell("/jobs");

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByText("lakana job list")).toBeInTheDocument();
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
