import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

const AT = "2026-11-10T08:00:00Z";

const RECENT = [
  { kind: "table", environment: "prod", ref: "gold.station_ranking", opened_at: AT },
  { kind: "query", environment: "dev", ref: "3f9c2a1b-77d0-4c1e-9a55-0c4f5b2e8d11", opened_at: AT },
  { kind: "job", environment: "prod", ref: "analytics/daily_sales", opened_at: AT },
  { kind: "run", environment: "prod", ref: "8d2e4f60-1b3a-4c5d-8e9f-a0b1c2d3e4f5", opened_at: AT },
];

/**
 * The recently opened region.
 *
 * @returns The region.
 */
async function region(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Recently opened" });
}

describe("Recent", () => {
  it("invites to the Catalog while nothing was opened", async () => {
    const { user, router } = renderShell("/");

    const list = await region();
    expect(await within(list).findByText(/Nothing opened yet/)).toBeVisible();
    // Nothing records an open before the Catalog, Queries and Jobs screens arrive: say so.
    expect(list).toHaveTextContent(
      "Nothing opened yet. The tables, queries, jobs and runs you open will show here once the Catalog, Queries and Jobs pages arrive in a later release.",
    );
    await user.click(within(list).getByRole("link", { name: "Find a table in the Catalog" }));
    // The Catalog is loaded on demand: the router waits for its chunk before it moves.
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/catalog");
    });
  });

  it.each([
    ["gold.station_ranking", "Table", "prod", "/catalog"],
    ["Query 3f9c2a1b", "Query", "dev", "/queries"],
    ["analytics/daily_sales", "Job", "prod", "/jobs"],
    ["Run 8d2e4f60", "Run", "prod", "/jobs"],
  ])("lists %s with its kind and environment, and opens it", async (name, kind, env, path) => {
    const { user, router, server } = renderShell("/", {
      routes: { "GET /api/v1/principals/admin/recent": { status: 200, body: RECENT } },
    });

    const link = await within(await region()).findByRole("link", { name });
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText(kind)).toBeInTheDocument();
    expect(within(row).getByText(env)).toHaveClass("env-label");
    expect(server.sent.some((request) => request.path === "/api/v1/principals/admin/recent")).toBe(
      true,
    );

    await user.click(link);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(path);
    });
    cleanup();
  });
});
