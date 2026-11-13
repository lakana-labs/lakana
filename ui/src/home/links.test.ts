import { describe, expect, it } from "vitest";

import { ATTENTION_OBJECTS, isReleaseVersion, objectPath, releaseNotesUrl } from "./links";

describe("links", () => {
  it.each([
    ["table", "prod", "gold.station_ranking", "/catalog/prod/gold/station_ranking"],
    ["query", "dev", "3f9c2a1b", "/queries"],
    ["job", "prod", "analytics/daily_sales", "/jobs"],
    ["run", "prod", "8d2e4f60", "/jobs"],
    ["check", "prod", "8d2e4f60", "/jobs"],
    ["worker", null, "w-2", "/settings/status"],
    ["recovery_link", null, "link-1", "/access/recovery-links"],
  ] as const)(
    "opens a %s at its page or, until it has one, at its area",
    (kind, environment, ref, path) => {
      expect(objectPath(kind, environment, ref)).toBe(path);
    },
  );

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
