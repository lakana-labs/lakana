import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { preloadJobsScreen } from "../test/jobs";
import { renderShell } from "../test/render";

const AT = "2026-11-10T08:00:00Z";

/**
 * A row of the server's answer that happened once.
 *
 * @param kind - What it reports.
 * @param environment - Its environment, or null.
 * @param title - Its name.
 * @param ref - What it opens.
 * @returns The row.
 */
function once(kind: string, environment: string | null, title: string, ref: string) {
  return { kind, environment, title, ref, at: AT, count: 1, since: null };
}

const ITEMS = [
  once("run_failed", "prod", "daily_sales", "run-1"),
  once("deploy_failed", "prod", "analytics", "deploy-1"),
  once("table_late", "prod", "gold.station_ranking", "gold.station_ranking"),
  once("check_failed", "dev", "not_null_station_id", "run-2"),
  once("query_failed", "dev", "Query 3f9c2a1b", "3f9c2a1b-q"),
  once("recovery_link_open", null, "sam", "link-1"),
  once("worker_lost", null, "worker-2.internal", "w-2"),
];

/**
 * Answers the attention route with these rows.
 *
 * @param items - The rows.
 * @param truncated - Whether the server cut more.
 * @returns The route.
 */
function answering(items: readonly object[], truncated = false) {
  return { "GET /api/v1/home/attention": { status: 200, body: { items, truncated } } };
}

/**
 * The attention region.
 *
 * @returns The region.
 */
async function region(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Needs your attention" });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Attention", () => {
  beforeAll(preloadJobsScreen);

  it("says Nothing needs your attention when the list is empty", async () => {
    renderShell("/");

    expect(await within(await region()).findByText("Nothing needs your attention.")).toBeVisible();
  });

  it.each([
    [0, "daily_sales", "Run failed", "prod", "/jobs/prod/runs/run-1"],
    [
      1,
      "analytics",
      "Deploy failed",
      "prod",
      "/jobs/deploys?environment=prod&repository=analytics",
    ],
    [2, "gold.station_ranking", "Late", "prod", "/catalog/prod/gold/station_ranking"],
    [3, "not_null_station_id", "Check failed", "dev", "/jobs/dev/runs/run-2"],
    [4, "Query 3f9c2a1b", "Query failed", "dev", "/queries/dev/runs/3f9c2a1b-q"],
    [5, "sam", "Recovery link open", "Instance", "/settings/sign-in/recovery-links"],
    [6, "worker-2.internal", "Worker down", "Instance", "/settings/status"],
  ])(
    "labels every row with its environment and state and opens its place (row %i, %s)",
    async (first, title, kind, env, path) => {
      const { user, router } = renderShell("/", { routes: answering(ITEMS.slice(first)) });

      const list = await region();
      const link = await within(list).findByRole("link", { name: title });
      const row = link.closest("tr") as HTMLElement;
      expect(within(row).getByText(kind).closest(".pill")).not.toBeNull();
      expect(within(row).getByText(env)).toHaveClass("env-label");

      await user.click(link);
      await waitFor(() => {
        expect(router.state.location.pathname + router.state.location.search).toBe(path);
      });
      cleanup();
    },
  );

  it("shows the newest five, then Show all N to the failed runs", async () => {
    const { user, router } = renderShell("/", { routes: answering(ITEMS) });

    const list = await region();
    await within(list).findByRole("link", { name: "daily_sales" });
    const names = within(list)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("link")[0]?.textContent);
    expect(names).toEqual([
      "daily_sales",
      "analytics",
      "gold.station_ranking",
      "not_null_station_id",
      "Query 3f9c2a1b",
    ]);
    expect(within(list).getByText("Newest first · top 5")).toHaveClass("panel-subtitle");

    await user.click(within(list).getByRole("link", { name: "Show all 7" }));
    await waitFor(() => {
      expect(router.state.location.pathname + router.state.location.search).toBe(
        "/jobs/runs?status=failed",
      );
    });
  });

  it("offers no Show all for five rows or fewer", async () => {
    renderShell("/", { routes: answering(ITEMS.slice(0, 5)) });

    const list = await region();
    await within(list).findByRole("link", { name: "daily_sales" });
    expect(within(list).queryByRole("link", { name: /^Show all/ })).toBeNull();
  });

  it("says Show all 50+ when the server cut the list", async () => {
    const many = Array.from({ length: 50 }, (_, index) =>
      once("run_failed", "prod", `job_${String(index)}`, `run-${String(index)}`),
    );
    renderShell("/", { routes: answering(many, true) });

    expect(await within(await region()).findByRole("link", { name: "Show all 50+" })).toBeVisible();
  });

  it.each([
    ["2026-11-17T12:00:00Z", "Failed 3 times since Tuesday"],
    [
      "2026-11-21T09:00:00Z",
      `Failed 3 times since ${new Intl.DateTimeFormat("en", { timeStyle: "short" }).format(new Date("2026-11-21T09:00:00Z"))}`,
    ],
    ["2026-11-10T12:00:00Z", "Failed 3 times since Nov 10, 2026"],
  ])("names a failure in a row with its count and since when (%s)", async (since, line) => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-21T10:00:00Z") });
    renderShell("/", {
      routes: answering([
        { ...once("run_failed", "prod", "nightly_ranking", "run-9"), count: 3, since },
      ]),
    });

    const list = await region();
    const row = (await within(list).findByRole("link", { name: "nightly_ranking" })).closest(
      "tr",
    ) as HTMLElement;
    expect(within(row).getByText(line)).toBeVisible();
  });

  it("polls in the background every 60 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { server } = renderShell("/");
    await within(await region()).findByText("Nothing needs your attention.");
    const polls = (): number =>
      server.sent.filter((request) => request.path === "/api/v1/home/attention").length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_000);
    });
    expect(polls()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(polls()).toBe(2);
    const sent = server.sent.filter((request) => request.path === "/api/v1/home/attention");
    expect(sent.every((request) => request.headers["x-lakana-background"] === "1")).toBe(true);
  });
});
