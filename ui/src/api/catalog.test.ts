import { describe, expect, it } from "vitest";

import { getTableTree, listTables, TABLE_SORTS, type ListTablesQuery } from "./catalog";

import { catalogRoutes, PAGE, TREE } from "../test/catalog";
import { fakeServer } from "../test/server";

const QUERY: ListTablesQuery = {
  environment: null,
  namespace: null,
  q: "",
  sort: "name",
  order: "asc",
  cursor: null,
};

describe("catalog api", () => {
  it("asks for the list with its filters in a fixed order and leaves out what is empty", async () => {
    const server = fakeServer(catalogRoutes());

    expect(
      await listTables({
        environment: "prod",
        namespace: "gold.sales",
        q: "store_id",
        sort: "rows",
        order: "desc",
        cursor: null,
      }),
    ).toEqual(PAGE);
    await listTables(QUERY);

    expect(server.sent[0]?.path).toBe(
      "/api/v1/tables?environment=prod&namespace=gold.sales&q=store_id&sort=rows&order=desc&limit=50",
    );
    expect(server.sent[1]?.path).toBe("/api/v1/tables?sort=name&order=asc&limit=50");
  });

  it("sends the cursor of the next page", async () => {
    const server = fakeServer(catalogRoutes());

    await listTables({ ...QUERY, cursor: "abc" });

    expect(server.sent[0]?.path).toBe("/api/v1/tables?sort=name&order=asc&limit=50&cursor=abc");
  });

  it("reads the tree", async () => {
    const server = fakeServer(catalogRoutes());

    expect(await getTableTree()).toEqual(TREE);
    expect(server.sent[0]?.path).toBe("/api/v1/tables/tree");
  });

  it("lists the sort keys in the order of the columns", () => {
    expect(TABLE_SORTS).toEqual([
      "name",
      "owner",
      "health",
      "rows",
      "size",
      "last_write",
      "last_read",
      "created",
    ]);
  });
});
