import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderShell } from "../test/render";

const AT = "2026-11-10T08:00:00Z";

const ITEMS = [
  { kind: "run_failed", environment: "prod", title: "daily_sales", ref: "run-1", at: AT },
  {
    kind: "table_late",
    environment: "prod",
    title: "gold.station_ranking",
    ref: "gold.station_ranking",
    at: AT,
  },
  { kind: "check_failed", environment: "dev", title: "not_null_station_id", ref: "run-2", at: AT },
  { kind: "query_failed", environment: "dev", title: "Query 3f9c2a1b", ref: "3f9c2a1b-q", at: AT },
  { kind: "recovery_link_open", environment: null, title: "sam", ref: "link-1", at: AT },
  { kind: "worker_lost", environment: null, title: "worker-2.internal", ref: "w-2", at: AT },
];

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
  it("says Nothing needs your attention when the list is empty", async () => {
    renderShell("/");

    expect(await within(await region()).findByText("Nothing needs your attention.")).toBeVisible();
  });

  it.each([
    ["daily_sales", "Run failed", "prod", "/jobs"],
    ["gold.station_ranking", "Late", "prod", "/catalog"],
    ["not_null_station_id", "Check failed", "dev", "/jobs"],
    ["Query 3f9c2a1b", "Query failed", "dev", "/queries"],
    ["sam", "Recovery link open", "Instance", "/access/recovery-links"],
    ["worker-2.internal", "Worker down", "Instance", "/settings/status"],
  ])(
    "labels every row with its environment and opens its placeholder (%s)",
    async (title, kind, env, path) => {
      const { user, router } = renderShell("/", {
        routes: {
          "GET /api/v1/home/attention": { status: 200, body: { items: ITEMS, truncated: false } },
        },
      });

      const list = await region();
      const link = await within(list).findByRole("link", { name: title });
      const row = link.closest("tr") as HTMLElement;
      expect(within(row).getByText(kind)).toBeInTheDocument();
      expect(within(row).getByText(env)).toHaveClass("env-label");

      await user.click(link);
      await waitFor(() => {
        expect(router.state.location.pathname).toBe(path);
      });
      cleanup();
    },
  );

  it("says when older rows were left out", async () => {
    renderShell("/", {
      routes: {
        "GET /api/v1/home/attention": { status: 200, body: { items: ITEMS, truncated: true } },
      },
    });

    expect(await within(await region()).findByText("Showing the newest 6.")).toBeVisible();
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
