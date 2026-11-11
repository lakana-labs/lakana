import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

const AT = "2026-11-10T08:00:00Z";

const WORK = {
  repositories: [
    {
      name: "analytics",
      environments: [
        { environment: "dev", last_deploy_status: "succeeded", last_deploy_at: AT },
        { environment: "prod", last_deploy_status: null, last_deploy_at: null },
      ],
    },
  ],
  jobs: [
    {
      environment: "dev",
      repository: "analytics",
      job: "daily_sales",
      last_run_status: "failed",
      last_run_at: AT,
    },
    {
      environment: "prod",
      repository: "analytics",
      job: "station_ranking",
      last_run_status: null,
      last_run_at: null,
    },
  ],
};

/**
 * The Your work region.
 *
 * @returns The region.
 */
async function region(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Your work" });
}

describe("YourWork", () => {
  it("lists the person's jobs as rows that open them", async () => {
    const { user, router } = renderShell("/", {
      routes: { "GET /api/v1/home/work": { status: 200, body: WORK } },
    });

    const work = await region();
    const job = await within(work).findByRole("link", { name: "daily_sales" });
    const row = job.closest("tr") as HTMLElement;
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(within(row).getByText("dev")).toHaveClass("env-label");
    expect(within(row).getByText("analytics")).toBeInTheDocument();
    const never = within(work).getByRole("link", { name: "station_ranking" }).closest("tr");
    expect(within(never as HTMLElement).getByText("Not run yet")).toBeInTheDocument();

    await user.click(job);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/jobs");
    });
  });

  it("lists the person's repositories with their last deploy per environment", async () => {
    renderShell("/", { routes: { "GET /api/v1/home/work": { status: 200, body: WORK } } });

    const work = await region();
    const repository = await within(work).findByRole("listitem", { name: "analytics" });
    expect(within(repository).getByText("Succeeded")).toBeInTheDocument();
    expect(within(repository).getByText("Not deployed yet")).toBeInTheDocument();
    expect(within(repository).getByText("prod")).toHaveClass("env-label");
  });

  it("shows repositories without jobs, and jobs without repositories", async () => {
    renderShell("/", {
      routes: { "GET /api/v1/home/work": { status: 200, body: { ...WORK, jobs: [] } } },
    });

    const work = await region();
    expect(await within(work).findByRole("listitem", { name: "analytics" })).toBeVisible();
    expect(within(work).queryByRole("table")).not.toBeInTheDocument();
    cleanup();

    renderShell("/", {
      routes: { "GET /api/v1/home/work": { status: 200, body: { ...WORK, repositories: [] } } },
    });

    const jobs = await region();
    expect(await within(jobs).findByRole("table")).toBeVisible();
    expect(within(jobs).queryByRole("list", { name: "Your repositories" })).not.toBeInTheDocument();
  });

  it("says what shows here while there is nothing", async () => {
    renderShell("/");

    expect(await within(await region()).findByText(/Nothing yet/)).toBeVisible();
  });
});
