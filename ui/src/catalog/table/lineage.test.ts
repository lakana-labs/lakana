import { describe, expect, it } from "vitest";

import { DOWN, graphEdge, graphNode, lineage, ROOT, UP, UP2 } from "../../test/table";
import { mergeGraphs, neighbourTables, rootOf } from "./lineage";

describe("rootOf", () => {
  it("is the node at distance 0", () => {
    expect(rootOf(UP)).toEqual(ROOT);
  });

  it("is null when the answer has no start", () => {
    expect(rootOf({ nodes: [], edges: [], next_cursor: null })).toBeNull();
  });
});

describe("neighbourTables", () => {
  it("keeps the tables, in order, and leaves out the start, jobs and connections", () => {
    expect(neighbourTables(UP).map((node) => node.name)).toEqual(["staging.stg_sales"]);
    expect(neighbourTables(DOWN).map((node) => node.name)).toEqual(["gold.sales.daily_sales"]);
  });

  it("is empty when nothing else is there", () => {
    expect(neighbourTables(lineage([]))).toEqual([]);
  });

  it("keeps every table when the answer has no start", () => {
    const graph = {
      nodes: [{ id: "table:x", kind: "table", name: "a.b", distance: 1 }],
      edges: [],
      next_cursor: null,
    };

    expect(neighbourTables(graph).map((node) => node.name)).toEqual(["a.b"]);
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
