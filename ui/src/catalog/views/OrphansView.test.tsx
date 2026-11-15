import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { manageRoutes, orphan } from "../../test/manage";
import { renderShell } from "../../test/render";

const ADDRESS = "/catalog?view=orphans&env=prod";
const LIST = "GET /api/v1/environments/prod/orphans";
const TABLE = "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/old_totals";

/**
 * Wraps orphans in the body the server answers with.
 *
 * @param orphans - The orphans.
 * @param hidden - How many the person may not read.
 * @param next - The cursor of the next page.
 * @returns The answer.
 */
function body(orphans: unknown[], hidden = 0, next: string | null = null) {
  return { status: 200, body: { orphans, hidden, next_cursor: next } };
}

/**
 * The list's rows below the head.
 *
 * @returns The rows.
 */
function rows(): HTMLElement[] {
  return within(screen.getByRole("region", { name: "Orphans" }))
    .getAllByRole("row")
    .slice(1);
}

describe("OrphansView", () => {
  it("lists each orphan with its kind and the repository that last built it", async () => {
    renderShell(ADDRESS, { routes: manageRoutes() });

    await screen.findByRole("region", { name: "Orphans" });
    const [row] = rows();
    expect(row).toHaveTextContent("old_totals");
    expect(row).toHaveTextContent("gold.sales");
    expect(row).toHaveTextContent("Table");
    expect(row).toHaveTextContent("sales-project");
    expect(screen.getByText("Orphans are listed for the whole of prod.")).toBeInTheDocument();
  });

  it("offers Drop only when actions lists drop, and reloads the page after a drop", async () => {
    let calls = 0;
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [TABLE]: { status: 204 },
        [LIST]: () => {
          calls += 1;
          return calls === 1
            ? body([orphan(), orphan({ table: "gold.sales.kept", actions: [] })])
            : body([orphan({ table: "gold.sales.kept", actions: [] })]);
        },
      },
    });

    await screen.findByRole("region", { name: "Orphans" });
    expect(screen.getAllByRole("button", { name: /^Drop / })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Drop gold.sales.old_totals" }));
    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(rows()).toHaveLength(1);
    });
    expect(screen.getByText("Dropped gold.sales.old_totals.")).toHaveFocus();
    expect(server.sent.filter((request) => request.method === "DELETE")).toHaveLength(1);
  });

  it("drops an artifact orphan through the artifact route", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: body([orphan({ table: "ml.model", kind: "ARTIFACT" })]),
      },
    });

    await user.click(await screen.findByRole("button", { name: "Drop ml.model" }));
    await user.click(await screen.findByRole("button", { name: "Drop the artifact" }));

    await waitFor(() => {
      expect(
        server.sent.some(
          (request) =>
            request.method === "DELETE" &&
            request.path === "/api/v1/environments/prod/namespaces/ml/artifacts/model",
        ),
      ).toBe(true);
    });
  });

  it("sends a dropped orphan to the Dropped view instead of offering Drop", async () => {
    renderShell(`${ADDRESS}&ns=gold.sales`, {
      routes: { ...manageRoutes(), [LIST]: body([orphan({ exists: false, actions: ["drop"] })]) },
    });

    await screen.findByRole("region", { name: "Orphans" });

    expect(screen.queryByRole("button", { name: /^Drop / })).not.toBeInTheDocument();
    expect(rows()[0]).toHaveTextContent("Dropped; it can still be restored");
    expect(
      screen.getByRole("link", { name: "Restore gold.sales.old_totals from Dropped" }),
    ).toHaveAttribute("href", "/catalog?env=prod&ns=gold.sales&view=dropped");
  });

  it("says how many orphans are hidden from you", async () => {
    renderShell(ADDRESS, { routes: { ...manageRoutes(), [LIST]: body([orphan()], 2) } });

    expect(
      await screen.findByText("2 more orphans are not shown: you may not read them."),
    ).toBeInTheDocument();
  });

  it("says the list covers the whole environment when a namespace is chosen", async () => {
    renderShell(`${ADDRESS}&ns=gold.sales`, { routes: manageRoutes() });

    expect(
      await screen.findByText("Orphans are listed for the whole of prod, not only gold.sales."),
    ).toBeInTheDocument();
  });

  it("says there are no orphans in the environment", async () => {
    renderShell(ADDRESS, { routes: { ...manageRoutes(), [LIST]: body([]) } });

    expect(await screen.findByText("No project left an orphan in prod.")).toBeInTheDocument();
  });

  it("pages with Show more", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? body([orphan({ table: "gold.sales.b" })])
            : body([orphan({ table: "gold.sales.a" })], 0, "c1"),
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    });
    expect(server.sent.at(-1)?.path).toContain("cursor=c1");
  });

  it("marks a bidi character in an orphan's repository", async () => {
    renderShell(ADDRESS, {
      routes: { ...manageRoutes(), [LIST]: body([orphan({ repository: "sales‮repo" })]) },
    });

    await screen.findByRole("region", { name: "Orphans" });

    expect(rows()[0]).toHaveTextContent("salesU+202Erepo");
  });

  it("shows an unknown kind, offers no Drop for a name without a namespace, and Cancel keeps the list", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: body([orphan({ kind: null }), orphan({ table: "loose" })]),
      },
    });

    await screen.findByRole("region", { name: "Orphans" });
    expect(rows()[0]).toHaveTextContent("Not known");
    expect(screen.getAllByRole("button", { name: /^Drop / })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Drop gold.sales.old_totals" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(server.sent.filter((request) => request.method === "DELETE")).toHaveLength(0);
    expect(rows()).toHaveLength(2);
  });

  it("keeps the rows and shows the sentence when Show more fails", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? { status: 500, body: { type: "InternalError", detail: "Try again in a moment." } }
            : body([orphan()], 0, "c1"),
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in a moment.");
    expect(rows()).toHaveLength(1);
  });

  it("shows the server's sentence when the first read fails", async () => {
    renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [LIST]: { status: 500, body: { type: "InternalError", detail: "Try later." } },
      },
    });

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
