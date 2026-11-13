import { describe, expect, it } from "vitest";

import { DOWN, lineage, ROOT, UP } from "../../test/table";
import { neighbourTables, rootOf } from "./lineage";

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
