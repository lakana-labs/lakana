import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { manageRoutes, retention } from "../../test/manage";
import { renderShell } from "../../test/render";

const ADDRESS = "/catalog?view=expiring&env=prod";
const LIST = "GET /api/v1/environments/prod/expiring-tables";

/**
 * The list's rows below the head.
 *
 * @returns The rows.
 */
function rows(): HTMLElement[] {
  return within(screen.getByRole("region", { name: "Expiring tables" }))
    .getAllByRole("row")
    .slice(1);
}

describe("ExpiringView", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse("2026-11-16T06:00:00Z"), toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("lists each table with when it expires and why", async () => {
    renderShell(ADDRESS, { routes: manageRoutes() });

    await screen.findByRole("region", { name: "Expiring tables" });
    const [row] = rows();
    expect(within(row as HTMLElement).getByRole("link", { name: "fact_sales" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold.sales/fact_sales",
    );
    expect(row).toHaveTextContent("gold.sales");
    const time = row?.querySelector("time");
    expect(time?.getAttribute("datetime")).toBe("2026-12-12T06:00:00Z");
    expect(time).toHaveTextContent("in 26 days");
    expect(row).not.toHaveTextContent("just now");
    expect(row).toHaveTextContent("30 days after its last activity, set for the table by ana");
  });

  it("says Due now for a table already past its expiry", async () => {
    renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: { status: 200, body: [retention({ expires_at: "2020-01-01T00:00:00Z" })] },
      },
    });

    await screen.findByRole("region", { name: "Expiring tables" });

    expect(rows()[0]).toHaveTextContent("Due now");
  });

  it("sends the namespace filter and pages with X-Next-Cursor", async () => {
    const { server, user } = renderShell(`${ADDRESS}&ns=gold.sales`, {
      routes: {
        ...manageRoutes(),
        [LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? { status: 200, body: [retention({ name: "second", uuid: "u2" })] }
            : {
                status: 200,
                body: [retention({ name: "first", uuid: "u1" })],
                headers: { "X-Next-Cursor": "c1" },
              },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    });
    const paths = server.sent.map((request) => request.path);
    expect(paths[paths.length - 2]).toContain("namespace=gold.sales");
    expect(paths.at(-1)).toContain("cursor=c1");
    expect(paths.at(-1)).toContain("namespace=gold.sales");
  });

  it("puts the focus on the first new row when Show more brings the last page", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? { status: 200, body: [retention({ name: "second", uuid: "u2" })] }
            : {
                status: 200,
                body: [retention({ name: "first", uuid: "u1" })],
                headers: { "X-Next-Cursor": "c1" },
              },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(screen.getByRole("link", { name: "second" })).toHaveFocus();
    });
  });

  it("puts the focus on the list when Show more brings an empty last page", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? { status: 200, body: [] }
            : {
                status: 200,
                body: [retention({ name: "first", uuid: "u1" })],
                headers: { "X-Next-Cursor": "c1" },
              },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Expiring tables" })).toHaveFocus();
    });
  });

  it("says nothing expires soon in the environment", async () => {
    renderShell(ADDRESS, { routes: { ...manageRoutes(), [LIST]: { status: 200, body: [] } } });

    expect(await screen.findByText("Nothing expires soon in prod.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Expiring tables" })).not.toBeInTheDocument();
  });

  it("shows the server's sentence when the read fails, and reads again on Try again", async () => {
    let calls = 0;
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { type: "InternalError", detail: "Try later." } }
            : { status: 200, body: [retention()] };
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("region", { name: "Expiring tables" })).toBeInTheDocument();
  });

  it("keeps the rows and shows the sentence when Show more fails; a table without an expiry says Not known", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? { status: 500, body: { type: "InternalError", detail: "Try again in a moment." } }
            : {
                status: 200,
                body: [retention({ expires_at: null })],
                headers: { "X-Next-Cursor": "c1" },
              },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in a moment.");
    expect(rows()[0]).toHaveTextContent("Not known");
  });
});
