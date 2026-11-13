import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../../test/render";
import { table, TABLE, TABLE_PATH } from "../../test/table";
import { columnJoins, columnRole } from "./ColumnsTab";

const PAGE = "/catalog/prod/gold.sales/fact_sales/columns";

/**
 * The columns table's rows, once the page has arrived.
 *
 * @returns The rows without the header.
 */
async function rows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Table columns" });
  return within(region).getAllByRole("row").slice(1);
}

describe("columnRole", () => {
  it("is key for a key column, measure for a measure column, else null", () => {
    expect(columnRole(TABLE, "sale_id")).toBe("key");
    expect(columnRole(TABLE, "amount")).toBe("measure");
    expect(columnRole(TABLE, "note")).toBeNull();
  });

  it("is null for a table without a model", () => {
    expect(columnRole(table({ model: null }), "sale_id")).toBeNull();
  });
});

describe("columnJoins", () => {
  it("lists the references of the column", () => {
    expect(columnJoins(TABLE, "store_id").map((join) => join.target)).toEqual(["gold.dim_store"]);
    expect(columnJoins(TABLE, "sale_id")).toEqual([]);
  });

  it("is empty for a table without a model", () => {
    expect(columnJoins(table({ model: null }), "store_id")).toEqual([]);
  });
});

describe("ColumnsTab", () => {
  it("shows every column with type, nullability, role, joins, description and terms", async () => {
    const schema = TABLE.schema.map((column) =>
      column.name === "sale_id"
        ? { ...column, description: "The sale's id.", terms: [{ key: "t1", name: "Sale" }] }
        : column,
    );
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema }) } },
    });

    const all = await rows();
    expect(all).toHaveLength(7);
    const cells = (row: HTMLElement): (string | null)[] =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent);
    expect(cells(all[0] as HTMLElement)).toEqual([
      "sale_id",
      "long",
      "Never",
      "Key",
      "",
      "The sale's id.",
      "Sale",
    ]);
    expect(cells(all[1] as HTMLElement).slice(0, 4)).toEqual(["store_id", "long", "Allowed", ""]);
    expect(cells(all[1] as HTMLElement)[4]).toContain("gold.dim_store.store_id");
    expect(cells(all[3] as HTMLElement)[3]).toBe("Measure");
  });

  it("links a join to the table it points at when that exists", async () => {
    renderShell(PAGE);

    await rows();

    expect(screen.getByRole("link", { name: "gold.dim_store.store_id" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold/dim_store",
    );
  });

  it("names a join whose target does not exist in plain text", async () => {
    const model = {
      ...(TABLE.model as NonNullable<typeof TABLE.model>),
      references: [
        {
          ...(TABLE.model?.references[0] as NonNullable<typeof TABLE.model>["references"][number]),
          target_exists: false,
        },
      ],
    };
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ model }) } },
    });

    await rows();

    expect(screen.getByText("gold.dim_store.store_id")).toBeVisible();
    expect(screen.queryByRole("link", { name: "gold.dim_store.store_id" })).not.toBeInTheDocument();
  });

  it("shows a column with a role, a description and terms as plain text", async () => {
    const schema = [
      {
        ...(TABLE.schema[0] as (typeof TABLE.schema)[number]),
        description: "<b>bold</b>",
        terms: [
          { key: "a", name: "Sale" },
          { key: "b", name: "Revenue" },
        ],
      },
    ];
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema }) } },
    });

    const [row] = await rows();

    expect(row).toHaveTextContent("<b>bold</b>");
    expect(row?.querySelector("b")).toBeNull();
    expect(row).toHaveTextContent("Sale, Revenue");
  });

  it("says a table has no columns", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema: [] }) } },
    });

    expect(await screen.findByText("This table has no columns.")).toBeVisible();
  });

  it("scrolls a wide schema in its own region", async () => {
    renderShell(PAGE);

    const region = await screen.findByRole("region", { name: "Table columns" });

    expect(region).toHaveAttribute("tabindex", "0");
    expect(within(region).getByRole("table")).toBeInTheDocument();
  });

  it("reads nothing but the table", async () => {
    const { server } = renderShell(PAGE);
    await rows();

    expect(
      server.sent.filter(
        (request) => request.path.includes("/health") || request.path.includes("lineage"),
      ),
    ).toEqual([]);
  });
});
