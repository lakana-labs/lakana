import { describe, expect, it } from "vitest";

import {
  deploysPath,
  jobPath,
  jobRunPath,
  jobsListPath,
  runsTabPath,
  splitJobRef,
  taskPath,
} from "./paths";

describe("the Jobs addresses", () => {
  it("the list, of all environments or filtered to one", () => {
    expect(jobsListPath(null)).toBe("/jobs");
    expect(jobsListPath("prod")).toBe("/jobs?environment=prod");
    expect(jobsListPath("my env/a")).toBe("/jobs?environment=my+env%2Fa");
  });

  it("the Runs tab, filtered by environment and states", () => {
    expect(runsTabPath({})).toBe("/jobs/runs");
    expect(runsTabPath({ environment: null, status: [] })).toBe("/jobs/runs");
    expect(runsTabPath({ environment: "my env", status: ["queued", "running"] })).toBe(
      "/jobs/runs?environment=my+env&status=queued%2Crunning",
    );
  });

  it("the Deploys tab, filtered by environment and repository", () => {
    expect(deploysPath(null)).toBe("/jobs/deploys");
    expect(deploysPath("prod")).toBe("/jobs/deploys?environment=prod");
    expect(deploysPath("prod", "team/analytics")).toBe(
      "/jobs/deploys?environment=prod&repository=team%2Fanalytics",
    );
    expect(deploysPath(null, "analytics")).toBe("/jobs/deploys?repository=analytics");
  });

  it("builds each address with every part encoded", () => {
    expect(jobPath("my env", "team/analytics", "nightly sales")).toBe(
      "/jobs/my%20env/job/team%2Fanalytics/nightly%20sales",
    );
    expect(jobRunPath("prod", "a/b")).toBe("/jobs/prod/runs/a%2Fb");
    expect(taskPath("prod", "r1", "silver.sales")).toBe("/jobs/prod/runs/r1/tasks/silver.sales");
    expect(taskPath("prod", "r1", "a b/c")).toBe("/jobs/prod/runs/r1/tasks/a%20b%2Fc");
  });

  it("splits a job ref at its last slash", () => {
    expect(splitJobRef("team/analytics/nightly_sales")).toEqual({
      repository: "team/analytics",
      job: "nightly_sales",
    });
    expect(splitJobRef("analytics/daily")).toEqual({ repository: "analytics", job: "daily" });
  });

  it("a ref without a slash, or with an empty part, is null", () => {
    expect(splitJobRef("daily")).toBeNull();
    expect(splitJobRef("analytics/")).toBeNull();
    expect(splitJobRef("/daily")).toBeNull();
  });
});
