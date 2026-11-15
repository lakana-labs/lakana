import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { dropped, manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { renderShell } from "../../test/render";
import { table } from "../../test/table";

const ADDRESS = "/catalog?view=dropped&env=prod";
const UNDROP = "/api/v1/environments/prod/namespaces/gold.sales/tables";
const ENVIRONMENT_LIST = "GET /api/v1/environments/prod/dropped-tables";
const TAKEN = "A table fact_returns already exists in gold.sales. Choose another name.";

/**
 * The list's rows below the head.
 *
 * @returns The rows.
 */
function rows(): HTMLElement[] {
  return within(screen.getByRole("region", { name: "Dropped tables" }))
    .getAllByRole("row")
    .slice(1);
}

/**
 * The requests the interface sent that change something.
 *
 * @param sent - Every request.
 * @returns The writes.
 */
function writes(sent: readonly { method: string; path: string; body: unknown }[]) {
  return sent.filter((request) => request.method !== "GET");
}

describe("DroppedView", () => {
  it("lists each dropped table with its namespace, its drop time and until when it can be restored", async () => {
    const { server } = renderShell(ADDRESS, { routes: manageRoutes() });

    expect(await screen.findByRole("region", { name: "Dropped tables" })).toBeInTheDocument();
    const [row] = rows();
    expect(row).toHaveTextContent("fact_returns");
    expect(row).toHaveTextContent("gold.sales");
    const times = [...(row as HTMLElement).querySelectorAll("time")].map((time) =>
      time.getAttribute("datetime"),
    );
    expect(times).toEqual(["2026-11-14T09:30:00Z", "2026-11-21T09:30:00Z"]);
    expect(screen.getByRole("columnheader", { name: "Restorable until" })).toBeInTheDocument();
    expect(server.sent.map((request) => request.path)).toContain(
      "/api/v1/environments/prod/dropped-tables?limit=100",
    );
  });

  it("pages with Show more and the body's cursor", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? {
                status: 200,
                body: { items: [dropped({ name: "second", uuid: "u2" })], next_cursor: null },
              }
            : {
                status: 200,
                body: { items: [dropped({ name: "first", uuid: "u1" })], next_cursor: "c1" },
              },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(rows()).toHaveLength(2);
    });
    expect(server.sent.at(-1)?.path).toContain("cursor=c1");
    expect(screen.getByText("second")).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("moves the focus to the result line when Show more brings an empty last page", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: (request) =>
          request.path.includes("cursor=c1")
            ? { status: 200, body: { items: [], next_cursor: null } }
            : {
                status: 200,
                body: { items: [dropped({ name: "first", uuid: "u1" })], next_cursor: "c1" },
              },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(document.querySelector(".dropped-view .catalog-result")).toHaveFocus();
    });
  });

  it("does not say nothing is left while more pages exist after the last row is undropped", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: {
          status: 200,
          body: { items: [dropped({ name: "first", uuid: "u1" })], next_cursor: "c1" },
        },
        [`POST ${UNDROP}/first/undrop`]: { status: 200, body: table({ name: "first" }) },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Undrop gold.sales.first" }));

    await screen.findByText(/Restored gold.sales.first/);
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
    expect(screen.queryByText(/Nothing was dropped/)).not.toBeInTheDocument();
  });

  it("keeps Undrop waiting while the environment's protection is read", async () => {
    const protection = held();
    const { server, user } = renderShell(ADDRESS, {
      routes: { ...manageRoutes(), "GET /api/v1/environments/prod": protection.route },
    });
    const button = await screen.findByRole("button", {
      name: /^Undrop gold\.sales\.fact_returns/,
    });

    await user.click(button);

    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(writes(server.sent)).toHaveLength(0);
    protection.release({ status: 200, body: { name: "prod", is_protected: true } });
    await waitFor(() => {
      expect(button).not.toHaveAttribute("aria-disabled", "true");
    });
    await user.click(button);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(writes(server.sent)).toHaveLength(0);
  });

  it("says when a table can no longer be restored and offers no Undrop", async () => {
    renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: {
          status: 200,
          body: { items: [dropped({ restorable: false })], next_cursor: null },
        },
      },
    });

    await screen.findByRole("region", { name: "Dropped tables" });

    expect(rows()[0]).toHaveTextContent("No longer restorable");
    expect(screen.queryByRole("button", { name: /Undrop/ })).not.toBeInTheDocument();
  });

  it("offers Undrop only when actions lists undrop", async () => {
    renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: {
          status: 200,
          body: {
            items: [
              dropped({ name: "mine", uuid: "u1" }),
              dropped({ name: "theirs", uuid: "u2", actions: [] }),
            ],
            next_cursor: null,
          },
        },
      },
    });

    await screen.findByRole("region", { name: "Dropped tables" });

    expect(screen.getAllByRole("button", { name: /^Undrop/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Undrop gold.sales.mine" })).toBeInTheDocument();
  });

  it("undrops at once, removes the row and says Restored with a link to the table's page", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [`POST ${UNDROP}/fact_returns/undrop`]: {
          status: 200,
          body: table({ name: "fact_returns" }),
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Undrop gold.sales.fact_returns" }));

    const status = await screen.findByText(/Restored gold.sales.fact_returns/);
    expect(status).toHaveAttribute("role", "status");
    expect(within(status).getByRole("link", { name: "Open the table" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold.sales/fact_returns",
    );
    expect(status).toHaveFocus();
    expect(screen.queryByRole("region", { name: "Dropped tables" })).not.toBeInTheDocument();
    expect(screen.getByText(/Nothing was dropped in prod/)).toBeInTheDocument();
    expect(writes(server.sent)).toHaveLength(1);
    expect(writes(server.sent)[0]?.body).toEqual({ new_name: null, break_glass_reason: null });
  });

  it("offers Restore as only when the name is taken", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [`POST ${UNDROP}/fact_returns/undrop`]: (request) =>
          (request.body as { new_name: string | null }).new_name === null
            ? { status: 409, body: { type: "AlreadyExistsError", detail: TAKEN } }
            : { status: 200, body: table({ name: "returns_old" }) },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Undrop gold.sales.fact_returns" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(TAKEN);
    await user.type(within(dialog).getByRole("textbox", { name: "Restore as" }), "returns_old");
    await user.click(within(dialog).getByRole("button", { name: "Undrop" }));

    expect(await screen.findByText(/Restored gold.sales.returns_old/)).toHaveFocus();
    expect(writes(server.sent).at(-1)?.body).toEqual({
      new_name: "returns_old",
      break_glass_reason: null,
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows only the server's sentence beside the row for any other conflict", async () => {
    const { user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [`POST ${UNDROP}/fact_returns/undrop`]: {
          status: 409,
          body: {
            type: "ConflictError",
            detail: "The table is being rebuilt. Try again when the run has finished.",
          },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Undrop gold.sales.fact_returns" }));

    expect(await within(rows()[0] as HTMLElement).findByRole("alert")).toHaveTextContent(
      "The table is being rebuilt. Try again when the run has finished.",
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undrop gold.sales.fact_returns" })).toBeEnabled();
  });

  it("asks for a reason before it sends in a protected environment", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        "GET /api/v1/environments/prod": {
          status: 200,
          body: { name: "prod", is_protected: true },
        },
        [`POST ${UNDROP}/fact_returns/undrop`]: {
          status: 200,
          body: table({ name: "fact_returns" }),
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Undrop gold.sales.fact_returns" }));

    const dialog = await screen.findByRole("dialog");
    expect(writes(server.sent)).toHaveLength(0);
    await user.type(
      await within(dialog).findByRole("textbox", { name: /Why you break the protection of prod/ }),
      "Dropped by mistake",
    );
    await user.click(within(dialog).getByRole("button", { name: "Undrop" }));

    expect(await screen.findByText(/Restored gold.sales.fact_returns/)).toBeInTheDocument();
    expect(writes(server.sent)[0]?.body).toEqual({
      new_name: null,
      break_glass_reason: "Dropped by mistake",
    });
  });

  it("uses the namespace route and says dropped from the namespace", async () => {
    const { server } = renderShell(`${ADDRESS}&ns=gold.sales`, { routes: manageRoutes() });

    await screen.findByRole("region", { name: "Dropped tables" });

    expect(screen.getByText("Tables dropped from gold.sales.")).toBeInTheDocument();
    const paths = server.sent.map((request) => request.path);
    expect(paths).toContain("/api/v1/environments/prod/namespaces/gold.sales/dropped-tables");
    expect(paths.some((path) => path.includes("/prod/dropped-tables"))).toBe(false);
  });

  it("shows the server's refusal when the environment may not be read, with Try again", async () => {
    const { server, user } = renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: {
          status: 403,
          body: {
            type: "ForbiddenError",
            detail: "You may not read prod. Ask an administrator for access.",
          },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You may not read prod. Ask an administrator for access.",
    );
    await user.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => {
      expect(
        server.sent.filter((request) => request.path.includes("/dropped-tables")),
      ).toHaveLength(2);
    });
  });

  it("marks a bidi character in a dropped table's name", async () => {
    renderShell(ADDRESS, {
      routes: {
        ...manageRoutes(),
        [ENVIRONMENT_LIST]: {
          status: 200,
          body: { items: [dropped({ name: "fact‮sales" })], next_cursor: null },
        },
      },
    });

    await screen.findByRole("region", { name: "Dropped tables" });

    expect(rows()[0]).toHaveTextContent("U+202E");
    expect(
      screen.getByRole("button", { name: /Undrop gold\.sales\.factU\+202Esales/ }),
    ).toBeInTheDocument();
  });

  it("shows the result line Dropped when it arrives from a drop, and gives it the focus", async () => {
    const { router } = renderShell(ADDRESS, { routes: manageRoutes() });
    await screen.findByRole("region", { name: "Dropped tables" });

    await act(async () => {
      await router.navigate(`${ADDRESS}&ns=gold.sales`, {
        state: { dropped: "gold.sales.fact_sales" },
      });
    });

    const line = await screen.findByText("Dropped gold.sales.fact_sales.");
    expect(line).toHaveAttribute("role", "status");
    expect(line).toHaveFocus();
  });
});
