import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { references } from "../../test/manage";
import { renderShell } from "../../test/render";
import { columnTexts, sortBy, sortState } from "../../test/sorting";
import { TABLE_PATH } from "../../test/table";

const TAB = "/catalog/prod/gold.sales/fact_sales/references";
const ROUTE = `GET ${TABLE_PATH}/references`;
const [OUTGOING] = references().outgoing as [ReturnType<typeof references>["outgoing"][number]];

/**
 * The panel under a heading, once the references have arrived.
 *
 * @param name - The heading's text.
 * @returns The panel.
 */
async function panel(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 2, name });
  return heading.closest("section") as HTMLElement;
}

/**
 * The incoming reference of `gold.sales.fact_returns`, with the column it matches.
 *
 * @returns The reference.
 */
function incomingRow(): ReturnType<typeof references>["incoming"][number] {
  return {
    id: "gold.sales.fact_returns/ref_sale_id",
    table: "gold.sales.fact_returns",
    column: "sale_id",
    target: "gold.sales.fact_sales",
    target_column: "id",
    target_exists: true,
    at: null,
  };
}

describe("ReferencesTab", () => {
  it("lists what the table references: column → target.column, each target a link to its page", async () => {
    renderShell(TAB, { routes: { [ROUTE]: { status: 200, body: references() } } });

    const out = await panel("This table references");
    const row = within(out).getByRole("row", { name: /store_id/ });
    const link = within(row).getByRole("link", { name: "gold.dim_store" });
    expect(link).toHaveAttribute("href", "/catalog/prod/gold/dim_store");
    expect(row).toHaveTextContent("store_id → gold.dim_store.store_id");
    expect(out.querySelector("table")).toHaveClass("list", "list-static");
  });

  it("sorts each direction by its columns, each under its own names in the address", async () => {
    const other = {
      ...incomingRow(),
      id: "gold.sales.alpha/ref",
      table: "gold.sales.alpha",
      column: "zz_id",
    };
    const { user, router } = renderShell(TAB, {
      routes: {
        [ROUTE]: {
          status: 200,
          body: references({
            outgoing: [OUTGOING, { ...OUTGOING, id: "o2", column: "amount_id" }],
            incoming: [incomingRow(), other],
          }),
        },
      },
    });

    const out = await panel("This table references");
    await sortBy(user, out, "Reference");
    expect(columnTexts(out)[0]).toMatch(/^amount_id/);
    expect(router.state.location.search).toBe("?outSort=reference");
    expect(within(out).queryByRole("button", { name: "Note" })).toBeNull();

    const into = await panel("Referenced by");
    await sortBy(user, into, "Table");
    expect(columnTexts(into)).toEqual(["gold.sales.alpha", "gold.sales.fact_returns"]);
    await sortBy(user, into, "Reference");
    expect(sortState(into, "Reference")).toBe("ascending");
    expect(columnTexts(into)).toEqual(["gold.sales.fact_returns", "gold.sales.alpha"]);
  });

  it("lists the tables that reference it, each linked", async () => {
    renderShell(TAB, {
      routes: { [ROUTE]: { status: 200, body: references({ incoming: [incomingRow()] }) } },
    });

    const into = await panel("Referenced by");
    const link = within(into).getByRole("link", { name: "gold.sales.fact_returns" });
    expect(link).toHaveAttribute("href", "/catalog/prod/gold.sales/fact_returns");
    expect(within(into).getByRole("row", { name: /sale_id/ })).toHaveTextContent("sale_id → id");
  });

  it("says a target does not exist when target_exists is false, with no link", async () => {
    const missing = {
      ...OUTGOING,
      target: "gold.dim_gone",
      target_exists: false,
    };
    renderShell(TAB, {
      routes: { [ROUTE]: { status: 200, body: references({ outgoing: [missing] }) } },
    });

    const out = await panel("This table references");
    expect(within(out).getByText("gold.dim_gone", { exact: false })).toBeVisible();
    expect(within(out).getByText("This table does not exist.")).toBeVisible();
    expect(within(out).queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the time column of a history target", async () => {
    const history = { ...OUTGOING, at: "sold_at" };
    renderShell(TAB, {
      routes: { [ROUTE]: { status: 200, body: references({ outgoing: [history] }) } },
    });

    expect(await screen.findByText("valid at sold_at")).toBeVisible();
  });

  it("counts what is hidden in each direction", async () => {
    renderShell(TAB, {
      routes: {
        [ROUTE]: { status: 200, body: references({ hidden_incoming: 2, hidden_outgoing: 1 }) },
      },
    });

    expect(await screen.findByText("2 more from tables you may not read")).toBeVisible();
    expect(screen.getByText("1 more points at a table you may not read")).toBeVisible();
  });

  it("says the header declares no references and none point here", async () => {
    renderShell(TAB, {
      routes: {
        [ROUTE]: { status: 200, body: references({ outgoing: [], incoming: [] }) },
      },
    });

    expect(
      await screen.findByText("The table's header declares no references and none point here."),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
  });

  it("shows ReadFailed with Try again on a failed read", async () => {
    let calls = 0;
    const { user } = renderShell(TAB, {
      routes: {
        [ROUTE]: () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The catalog broke." } }
            : { status: 200, body: references() };
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalog broke.");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await panel("This table references")).toBeVisible();
    expect(document.querySelector(".references-tab")).toHaveFocus();
  });

  it("marks invisible characters in a name", async () => {
    const odd = { ...OUTGOING, column: "a‮b" };
    renderShell(TAB, {
      routes: { [ROUTE]: { status: 200, body: references({ outgoing: [odd] }) } },
    });

    const out = await panel("This table references");
    expect(out).not.toHaveTextContent("‮");
  });
});
