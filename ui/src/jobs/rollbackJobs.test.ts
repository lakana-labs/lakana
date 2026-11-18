import { describe, expect, it } from "vitest";

import { rollbackJobs } from "./rollbackJobs";

describe("rollbackJobs", () => {
  it("marks kept, removed and back, sorted", () => {
    expect(
      rollbackJobs(["weekly_report", "daily_sales", "orders_hourly"], ["daily_sales", "backfill"]),
    ).toEqual([
      { job: "backfill", change: "back" },
      { job: "daily_sales", change: "kept" },
      { job: "orders_hourly", change: "removed" },
      { job: "weekly_report", change: "removed" },
    ]);
  });

  it("two empty sets give none", () => {
    expect(rollbackJobs([], [])).toEqual([]);
  });
});
