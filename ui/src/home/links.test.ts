import { describe, expect, it } from "vitest";

import type { AttentionKind } from "../api";
import { attentionPath, isReleaseVersion, objectPath, releaseNotesUrl } from "./links";

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

  it.each([
    ["run_failed", "prod", "nightly_ranking", "run-1", "/jobs/prod/runs/run-1"],
    [
      "deploy_failed",
      "prod",
      "team/analytics",
      "deploy-1",
      "/jobs/deploys?environment=prod&repository=team%2Fanalytics",
    ],
    ["deploy_failed", null, "analytics", "deploy-1", "/jobs/deploys?repository=analytics"],
    ["table_late", "prod", "gold.sales", "gold.sales", "/catalog/prod/gold/sales"],
    ["check_failed", "dev", "gold.sales/not_null_id", "run-2", "/jobs/dev/runs/run-2"],
    ["query_failed", "dev", "Query 3f9c2a1b", "3f9c2a1b", "/queries/dev/runs/3f9c2a1b"],
    ["recovery_link_open", null, "sam", "link-1", "/settings/sign-in/recovery-links"],
    ["worker_lost", null, "worker-2", "w-2", "/settings/status"],
  ] as const)(
    "opens a %s row at its failure or its place (%s, %s)",
    (kind: AttentionKind, environment, title, ref, path) => {
      const item = {
        kind,
        environment,
        title,
        ref,
        at: "2026-11-21T08:00:00Z",
        count: 1,
        since: null,
      };
      expect(attentionPath(item)).toBe(path);
    },
  );

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
