import { describe, expect, it } from "vitest";

import { ATTENTION_OBJECTS, isReleaseVersion, objectPath, releaseNotesUrl } from "./links";

describe("links", () => {
  it.each([
    ["table", "prod", "gold.station_ranking", "/catalog/prod/gold/station_ranking"],
    ["worker", null, "w-2", "/settings/status"],
    ["recovery_link", null, "link-1", "/settings/sign-in/recovery-links"],
  ] as const)(
    "opens a %s at its page or, until it has one, at its area",
    (kind, environment, ref, path) => {
      expect(objectPath(kind, environment, ref)).toBe(path);
    },
  );

  it("a job opens its page", () => {
    expect(objectPath("job", "prod", "team/analytics/daily_sales")).toBe(
      "/jobs/prod/job/team%2Fanalytics/daily_sales",
    );
    expect(objectPath("job", "prod", "daily_sales")).toBeNull();
  });

  it("a run and a check open the run's page", () => {
    expect(objectPath("run", "prod", "8d2e4f60")).toBe("/jobs/prod/runs/8d2e4f60");
    expect(objectPath("check", "prod", "8d2e4f60")).toBe("/jobs/prod/runs/8d2e4f60");
  });

  it("without an environment they open the Jobs area", () => {
    expect(objectPath("job", null, "a/b")).toBe("/jobs");
    expect(objectPath("run", null, "8d2e4f60")).toBe("/jobs");
    expect(objectPath("check", null, "8d2e4f60")).toBe("/jobs");
  });

  it("a query opens its run on the Queries screen", () => {
    expect(objectPath("query", "dev", "3f9c2a1b")).toBe("/queries/dev/runs/3f9c2a1b");
    expect(objectPath("query", "my env", "a/b")).toBe("/queries/my%20env/runs/a%2Fb");
    expect(objectPath("query", null, "3f9c2a1b")).toBe("/queries");
  });

  it("links a table on Home to its page", () => {
    expect(objectPath("table", "prod", "gold.sales.fact_sales")).toBe(
      "/catalog/prod/gold.sales/fact_sales",
    );
  });

  it("shows a table without an environment or a dot as plain text", () => {
    expect(objectPath("table", null, "x.y")).toBeNull();
    expect(objectPath("table", "prod", "x")).toBeNull();
  });

  it("maps every attention kind to the object it opens", () => {
    expect(ATTENTION_OBJECTS).toEqual({
      run_failed: "run",
      table_late: "table",
      check_failed: "check",
      query_failed: "query",
      recovery_link_open: "recovery_link",
      worker_lost: "worker",
    });
  });

  it.each([
    ["0.23.0", true],
    ["10.0.12", true],
    ["0.23.1.dev4+g8dc908d7", false],
    ["0+unknown", false],
    ["v0.23.0", false],
    ["0.23", false],
  ])("knows whether %s is a release", (version, release) => {
    expect(isReleaseVersion(version)).toBe(release);
    expect(releaseNotesUrl(version)).toBe(
      release ? `https://github.com/lakana-labs/lakana/releases/tag/v${version}` : null,
    );
  });
});
