import { describe, expect, it } from "vitest";

import { jobPath, jobRunPath, splitJobRef, taskPath } from "./paths";

describe("the Jobs addresses", () => {
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

  it("a ref without a slash is null", () => {
    expect(splitJobRef("daily")).toBeNull();
  });
});
