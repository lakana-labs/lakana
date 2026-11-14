import { describe, expect, it } from "vitest";

import type { GraphOut } from "../../api";
import { assetNode, DOWN, graphEdge, graphNode, lineage, ROOT, UP, UP2 } from "../../test/table";
import {
  BOX_H,
  BOX_W,
  COL_GAP,
  depthQuery,
  jobLabel,
  layout,
  type LineageLayout,
  mergeGraphs,
  readDepths,
  ROW_GAP,
} from "./layout";

/** Nothing upstream or downstream: the table alone. */
const ALONE = lineage([]);

/**
 * The column of each drawn node, by id.
 *
 * @param drawn - The layout.
 * @returns The columns.
 */
function columns(drawn: LineageLayout): Record<string, number> {
  return Object.fromEntries(drawn.nodes.map((box) => [box.node.id, box.column]));
}

/**
 * One drawn box, by the node's id.
 *
 * @param drawn - The layout.
 * @param id - The node's id.
 * @returns The box.
 */
function box(drawn: LineageLayout, id: string): LineageLayout["nodes"][number] {
  const found = drawn.nodes.find((one) => one.node.id === id);
  if (found === undefined) {
    throw new Error(`${id} is not drawn`);
  }
  return found;
}

describe("layout", () => {
  it("puts upstream left and downstream right of the root by steps", () => {
    const drawn = layout(UP2, DOWN);

    expect(columns(drawn)).toEqual({
      "table:bronze.sales_raw": -4,
      "asset:analytics/staging.stg_sales": -3,
      "table:staging.stg_sales": -2,
      "connection:pg-sales": -2,
      "asset:analytics/gold.sales.fact_sales": -1,
      [ROOT.id]: 0,
      "asset:analytics/gold.sales.daily_sales": 1,
      "table:gold.sales.daily_sales": 2,
    });
    expect(box(drawn, "table:bronze.sales_raw").x).toBe(0);
    expect(box(drawn, ROOT.id).x).toBe(4 * (BOX_W + COL_GAP));
    expect(box(drawn, "table:gold.sales.daily_sales").x).toBe(6 * (BOX_W + COL_GAP));
    expect(drawn.width).toBe(7 * BOX_W + 6 * COL_GAP);
    // The boxes in order, left to right and top to bottom, as the text alternative lists them.
    expect(drawn.nodes.map((one) => one.column)).toEqual([-4, -3, -2, -2, -1, 0, 1, 2]);
  });

  it("places a node reached both ways once, on the upstream side when it is as near", () => {
    const both = graphNode("table", "gold.sales.returns", 1);
    const feeds = assetNode("gold.sales.fact_sales", 1);
    const fed = assetNode("gold.sales.returns", 1);
    const up = lineage(
      [feeds, both],
      [graphEdge("builds", feeds, ROOT), graphEdge("reads", feeds, both)],
    );
    const down = lineage(
      [fed, both],
      [graphEdge("reads", fed, ROOT), graphEdge("builds", fed, both)],
    );

    const drawn = layout(up, down);

    expect(drawn.nodes.filter((one) => one.node.id === both.id)).toHaveLength(1);
    expect(box(drawn, both.id).column).toBe(-2);
  });

  it("places a node reached both ways on the side where it is nearer", () => {
    const both = graphNode("table", "gold.sales.returns", 2);
    const middle = graphNode("table", "silver.sales", 1);
    const first = assetNode("gold.sales.fact_sales", 1);
    const second = assetNode("silver.sales", 2);
    const fed = assetNode("gold.sales.returns", 1);
    const up = lineage(
      [first, middle, second, both],
      [
        graphEdge("builds", first, ROOT),
        graphEdge("reads", first, middle),
        graphEdge("builds", second, middle),
        graphEdge("reads", second, both),
      ],
    );
    const down = lineage(
      [fed, both],
      [graphEdge("reads", fed, ROOT), graphEdge("builds", fed, both)],
    );

    expect(box(layout(up, down), both.id).column).toBe(2);
  });

  it("gives every node in a column its own row", () => {
    const s3 = graphNode("connection", "s3-exports", 1, { connection_kind: "s3" });
    const asset = UP.nodes[1] as (typeof UP.nodes)[number];
    const up: GraphOut = {
      ...UP,
      nodes: [...UP.nodes, s3],
      edges: [...UP.edges, graphEdge("uses", asset, s3)],
    };

    const drawn = layout(up, ALONE);

    const column = drawn.nodes.filter((one) => one.column === -2);
    expect(column.map((one) => [one.node.name, one.row])).toEqual([
      ["staging.stg_sales", 0],
      ["pg-sales", 1],
      ["s3-exports", 2],
    ]);
    for (const one of column) {
      for (const other of column) {
        if (one !== other) {
          expect(Math.abs(one.y - other.y)).toBeGreaterThanOrEqual(BOX_H + ROW_GAP);
        }
      }
    }
    expect(drawn.height).toBe(3 * BOX_H + 2 * ROW_GAP);
    // A column of one box sits in the middle of the tallest.
    expect(box(drawn, ROOT.id).y).toBe(BOX_H + ROW_GAP);
  });

  it("draws a connection beside its asset, away from the root, on either side", () => {
    const export_ = graphNode("connection", "sftp-partner", 1, { connection_kind: null });
    const fed = DOWN.nodes[1] as (typeof DOWN.nodes)[number];
    const down: GraphOut = {
      ...DOWN,
      nodes: [...DOWN.nodes, export_],
      edges: [...DOWN.edges, graphEdge("uses", fed, export_)],
    };

    const drawn = layout(UP, down);

    expect(box(drawn, "connection:pg-sales").column).toBe(-2);
    expect(box(drawn, export_.id).column).toBe(2);
  });

  it("draws a connection several assets use once, beside the nearest", () => {
    const shared = graphNode("connection", "pg-sales", 1, { connection_kind: "postgresql" });
    const far = assetNode("staging.stg_sales", 2);
    const near = UP.nodes[1] as (typeof UP.nodes)[number];
    const farFirst: GraphOut = {
      ...UP2,
      edges: [graphEdge("uses", far, shared), ...UP2.edges, graphEdge("uses", near, shared)],
    };

    const nearFirst: GraphOut = { ...UP2, edges: [...UP2.edges, graphEdge("uses", far, shared)] };

    for (const answer of [farFirst, nearFirst]) {
      const drawn = layout(answer, ALONE);
      expect(drawn.nodes.filter((one) => one.node.id === shared.id)).toHaveLength(1);
      expect(box(drawn, shared.id).column).toBe(-2);
    }
  });

  it("names the jobs that run each asset on the asset's box, and draws no box for a job", () => {
    const backfill = graphNode("job", "analytics/backfill", 1, { repository: "analytics" });
    const asset = UP.nodes[1] as (typeof UP.nodes)[number];
    const up: GraphOut = {
      ...UP,
      nodes: [...UP.nodes, backfill],
      edges: [...UP.edges, graphEdge("runs", backfill, asset)],
    };

    const drawn = layout(up, DOWN);

    expect(box(drawn, asset.id).jobs.map((job) => job.name)).toEqual([
      "analytics/backfill",
      "analytics/daily_sales",
    ]);
    expect(box(drawn, "asset:analytics/gold.sales.daily_sales").jobs).toEqual([]);
    expect(drawn.nodes.some((one) => one.node.kind === "job")).toBe(false);
  });

  it("draws the builds, reads and uses edges as straight lines between box sides", () => {
    const drawn = layout(UP, ALONE);
    const asset = box(drawn, "asset:analytics/gold.sales.fact_sales");
    const root = box(drawn, ROOT.id);
    const table = box(drawn, "table:staging.stg_sales");
    const connection = box(drawn, "connection:pg-sales");

    expect(drawn.edges).toEqual([
      { x1: asset.x + BOX_W, y1: asset.y + BOX_H / 2, x2: root.x, y2: root.y + BOX_H / 2 },
      { x1: table.x + BOX_W, y1: table.y + BOX_H / 2, x2: asset.x, y2: asset.y + BOX_H / 2 },
      {
        x1: connection.x + BOX_W,
        y1: connection.y + BOX_H / 2,
        x2: asset.x,
        y2: asset.y + BOX_H / 2,
      },
    ]);
  });

  it("draws the table alone when nothing is upstream or downstream", () => {
    const drawn = layout(ALONE, ALONE);

    expect(drawn.nodes.map((one) => [one.node.id, one.x, one.y])).toEqual([[ROOT.id, 0, 0]]);
    expect(drawn.edges).toEqual([]);
    expect([drawn.width, drawn.height]).toEqual([BOX_W, BOX_H]);
  });

  it("draws nothing without the table", () => {
    const empty = { nodes: [], edges: [], next_cursor: null };

    expect(layout(empty, empty)).toEqual({ nodes: [], edges: [], width: 0, height: 0 });
  });

  it("leaves out what no edge reaches, and its edges", () => {
    const loose = graphNode("table", "gold.loose", 1);
    const looseAsset = assetNode("gold.loose", 1);
    const lonely = graphNode("connection", "pg-old", 1, { connection_kind: "postgresql" });
    const other = graphNode("measure", "gold.sales.fact_sales.amount", 1);
    const up = lineage(
      [loose, looseAsset, lonely, other],
      [
        graphEdge("builds", looseAsset, loose),
        graphEdge("uses", looseAsset, lonely),
        graphEdge("measures", other, ROOT),
        // A job on a later page of the answer.
        { kind: "runs", source: "job:analytics/elsewhere", target: ROOT.id },
      ],
    );

    const drawn = layout(up, ALONE);

    expect(drawn.nodes.map((one) => one.node.id)).toEqual([ROOT.id]);
    expect(drawn.nodes[0]?.jobs).toEqual([]);
    expect(drawn.edges).toEqual([]);
  });
});

describe("depthQuery", () => {
  it("keeps the depths in the address, leaving out one step", () => {
    expect(depthQuery({ up: 1, down: 1 }).toString()).toBe("");
    expect(depthQuery({ up: 2, down: 1 }).toString()).toBe("up=2");
    expect(depthQuery({ up: 3, down: 10 }).toString()).toBe("up=3&down=10");
  });
});

describe("readDepths", () => {
  it("reads one step each way when the address names none", () => {
    expect(readDepths(new URLSearchParams())).toEqual({ up: 1, down: 1 });
  });

  it("reads the steps the address names", () => {
    expect(readDepths(new URLSearchParams("up=2&down=10"))).toEqual({ up: 2, down: 10 });
  });

  it.each(["abc", "0", "-1", "2.5", "", "1e1", " 2"])("reads %j as one step", (value) => {
    expect(readDepths(new URLSearchParams({ up: value, down: value }))).toEqual({
      up: 1,
      down: 1,
    });
  });

  it("caps at ten steps", () => {
    expect(readDepths(new URLSearchParams("up=11&down=999999999999999999999"))).toEqual({
      up: 10,
      down: 10,
    });
  });
});

describe("jobLabel", () => {
  it("splits a job's name at the last slash into the job and its repository", () => {
    expect(jobLabel(graphNode("job", "analytics/daily_sales", 1))).toEqual({
      job: "daily_sales",
      repository: "analytics",
    });
  });

  it("shows a job name without a slash whole", () => {
    expect(jobLabel(graphNode("job", "daily_sales", 1))).toEqual({
      job: "daily_sales",
      repository: null,
    });
  });
});

describe("mergeGraphs", () => {
  it("keeps each node once by its id and each edge once by kind, source and target", () => {
    const merged = mergeGraphs(UP2, UP);

    expect(merged.nodes).toEqual(UP2.nodes);
    expect(merged.edges).toEqual(UP2.edges);
  });

  it("puts what is upstream first, then what is downstream", () => {
    const merged = mergeGraphs(UP, DOWN);

    expect(merged.nodes.map((node) => node.id)).toEqual([
      ...UP.nodes.map((node) => node.id),
      ...DOWN.nodes.slice(1).map((node) => node.id),
    ]);
    expect(merged.edges).toEqual([...UP.edges, ...DOWN.edges]);
    expect(merged.next_cursor).toBeNull();
  });

  it("keeps an edge of another kind between the same nodes", () => {
    const table = graphNode("table", "a.b", 1);
    const merged = mergeGraphs(
      lineage([table], [graphEdge("reads", ROOT, table)]),
      lineage([table], [graphEdge("builds", ROOT, table), graphEdge("reads", table, ROOT)]),
    );

    expect(merged.edges.map((edge) => edge.kind)).toEqual(["reads", "builds", "reads"]);
  });

  it("says it is cut when either side is", () => {
    expect(mergeGraphs({ ...UP, next_cursor: "c1" }, DOWN).next_cursor).toBe("c1");
    expect(mergeGraphs(UP, { ...DOWN, next_cursor: "c2" }).next_cursor).toBe("c2");
  });
});
