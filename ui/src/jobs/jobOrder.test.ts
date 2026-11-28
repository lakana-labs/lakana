import { describe, expect, it } from "vitest";

import { aJob } from "../test/jobs";
import { jobOrder } from "./useJobsOf";

const ALPHA = aJob({ name: "alpha", last_run_status: "failed", next_run_at: null });
const BETA = aJob({
  name: "beta",
  last_run_status: "succeeded",
  next_run_at: "2026-11-18T01:00:00Z",
});
const GAMMA = aJob({ name: "gamma", last_run_status: null, next_run_at: "2026-11-17T01:00:00Z" });

/**
 * Sorts the three jobs and names them.
 *
 * @param order - The order.
 * @returns The names in order.
 */
function names(order: ReturnType<typeof jobOrder>): string[] {
  return [BETA, GAMMA, ALPHA].sort(order).map((job) => job.name);
}

describe("jobOrder", () => {
  it("orders by name either way, and by name without a sort", () => {
    expect(names(jobOrder(null))).toEqual(["alpha", "beta", "gamma"]);
    expect(names(jobOrder({ sort: "name", order: "desc" }))).toEqual(["gamma", "beta", "alpha"]);
  });

  it("orders by a value, a job without one last in either direction", () => {
    expect(names(jobOrder({ sort: "status", order: "asc" }))).toEqual(["alpha", "beta", "gamma"]);
    expect(names(jobOrder({ sort: "status", order: "desc" }))).toEqual(["beta", "alpha", "gamma"]);
    expect(names(jobOrder({ sort: "next_run", order: "asc" }))).toEqual(["gamma", "beta", "alpha"]);
    expect(names(jobOrder({ sort: "next_run", order: "desc" }))).toEqual([
      "beta",
      "gamma",
      "alpha",
    ]);
  });

  it("orders equal values by name", () => {
    const twin = aJob({ name: "aaa", last_run_status: "failed" });
    expect(
      [ALPHA, twin].sort(jobOrder({ sort: "status", order: "desc" })).map((j) => j.name),
    ).toEqual(["aaa", "alpha"]);
  });

  it("compares times as instants: a whole second is before a fraction of it", () => {
    const whole = aJob({ name: "whole", last_run_at: "2026-11-17T10:00:00Z" });
    const later = aJob({ name: "later", last_run_at: "2026-11-17T10:00:00.500Z" });
    expect(
      [whole, later].sort(jobOrder({ sort: "last_run", order: "desc" })).map((j) => j.name),
    ).toEqual(["later", "whole"]);
  });

  it("orders by repository and environment", () => {
    const shop = aJob({ name: "a", repository: "Shop", environment: "prod", cron: "0 2 * * *" });
    const books = aJob({ name: "b", repository: "books", environment: "dev", cron: null });
    const hourly = aJob({
      name: "c",
      repository: "ml",
      environment: "Dev2",
      cron: "0 * * * *",
      timezone: null,
    });
    const jobs = [shop, books, hourly];
    const by = (sort: string, order: "asc" | "desc" = "asc"): string[] =>
      [...jobs].sort(jobOrder({ sort, order })).map((job) => job.name);
    expect(by("repository")).toEqual(["b", "c", "a"]);
    expect(by("environment")).toEqual(["b", "c", "a"]);
  });
});
