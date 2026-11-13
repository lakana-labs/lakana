import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { GraphNodeOut, GraphOut } from "../../api";
import { assetNode, DOWN, graphEdge, graphNode, lineage, ROOT, UP } from "../../test/table";
import { layout } from "./layout";
import { LineageList } from "./LineageList";

/**
 * Lists two lineage answers inside a router.
 *
 * @param up - What feeds the table.
 * @param down - What the table feeds.
 * @returns The list.
 */
function list(up: GraphOut, down: GraphOut = lineage([])): HTMLElement {
  const router = createMemoryRouter(
    [{ path: "*", element: <LineageList up={up} down={down} environment="prod" /> }],
    { initialEntries: ["/catalog/prod/gold.sales/fact_sales/lineage"] },
  );
  render(<RouterProvider router={router} />);
  return screen.getByRole("list", { name: "Lineage as a list" });
}

/**
 * Each item's own text, without the lists inside it, in document order.
 *
 * @param root - The list.
 * @returns The texts.
 */
function items(root: HTMLElement): string[] {
  return [...root.querySelectorAll("li")].map((item) =>
    [...item.childNodes]
      .filter((child) => child.nodeName !== "UL")
      .map((child) => child.textContent)
      .join(""),
  );
}

describe("LineageList", () => {
  it("lists the same nodes and edges as the drawing", () => {
    const root = list(UP, DOWN);

    expect(items(root)).toEqual([
      "staging.stg_sales",
      "gold.sales.fact_sales (this table)",
      "built by job daily_sales (asset gold.sales.fact_sales), which reads staging.stg_sales",
      "uses connection pg-sales (PostgreSQL)",
      "gold.sales.daily_sales",
      "built by asset gold.sales.daily_sales (run by no job), which reads gold.sales.fact_sales",
    ]);
    const text = root.textContent;
    for (const box of layout(UP, DOWN).nodes) {
      expect(text).toContain(box.node.name);
    }
  });

  it("links tables to their page and leaves jobs and connections as text", () => {
    const root = list(UP, DOWN);

    expect(
      within(root)
        .getAllByRole("link")
        .map((link) => [link.textContent, link.getAttribute("href")]),
    ).toEqual([
      ["staging.stg_sales", "/catalog/prod/staging/stg_sales"],
      ["staging.stg_sales", "/catalog/prod/staging/stg_sales"],
      ["gold.sales.daily_sales", "/catalog/prod/gold.sales/daily_sales"],
    ]);
  });

  it("names every job of an asset, and every table it reads", () => {
    const asset = UP.nodes[1] as GraphNodeOut;
    const backfill = graphNode("job", "analytics/backfill", 1);
    const orders = graphNode("table", "staging.stg_orders", 1);
    const up: GraphOut = {
      ...UP,
      nodes: [...UP.nodes, backfill, orders],
      edges: [...UP.edges, graphEdge("runs", backfill, asset), graphEdge("reads", asset, orders)],
    };

    expect(items(list(up))).toContain(
      "built by jobs backfill, daily_sales (asset gold.sales.fact_sales), which reads staging.stg_orders, staging.stg_sales",
    );
  });

  it("says what builds a table that reads nothing, and lists an asset that builds no table", () => {
    const seed = assetNode("gold.sales.fact_sales", 1);
    const loose = assetNode("gold.sales.not_there_yet", 1);
    const up = lineage([seed], [graphEdge("builds", seed, ROOT)]);
    const down = lineage([loose], [graphEdge("reads", loose, ROOT)]);

    expect(items(list(up, down))).toEqual([
      "gold.sales.fact_sales (this table)",
      "built by asset gold.sales.fact_sales (run by no job)",
      "built by asset gold.sales.not_there_yet (run by no job), which reads gold.sales.fact_sales",
    ]);
  });

  it("names a connection that is not set up, and an unknown kind as it came", () => {
    const asset = UP.nodes[1] as GraphNodeOut;
    const old = graphNode("connection", "pg-old", 1, { connection_kind: null });
    const ftp = graphNode("connection", "ftp-drop", 1, { connection_kind: "ftp\u202E" });
    const up = lineage(
      [asset, old, ftp],
      [
        graphEdge("builds", asset, ROOT),
        graphEdge("uses", asset, old),
        graphEdge("uses", asset, ftp),
      ],
    );

    const root = list(up);

    expect(items(root)).toEqual([
      "gold.sales.fact_sales (this table)",
      "built by asset gold.sales.fact_sales (run by no job)",
      "uses connection ftp-drop (ftpU+202E)",
      "uses connection pg-old (Connection not set up)",
    ]);
    expect(root.querySelector(".char-mark")?.textContent).toBe("U+202E");
  });

  it("shows names with markup as text and marks invisible characters", () => {
    const asset = UP.nodes[1] as GraphNodeOut;
    const strange = graphNode("table", "staging.<b>x</b>\u202E", 1);
    const nodot = graphNode("table", "nodot", 1);
    const up = lineage(
      [asset, strange, nodot],
      [
        graphEdge("builds", asset, ROOT),
        graphEdge("reads", asset, strange),
        graphEdge("reads", asset, nodot),
      ],
    );

    const root = list(up);

    expect(root.querySelector("b")).toBeNull();
    expect(root.textContent).toContain("staging.<b>x</b>U+202E");
    // A name that cannot be split into namespace and name is text, not a link.
    expect(within(root).queryByRole("link", { name: "nodot" })).toBeNull();
  });
});
