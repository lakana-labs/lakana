import { describe, expect, it } from "vitest";

import { fakeServer } from "../test/server";
import { DOWN, HEALTH, LINEAGE_PATH, TABLE, UP } from "../test/table";
import { getLineage, getTable, getTableHealth, myPermissions, tableApiPath } from "./table";

const REF = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };

describe("table", () => {
  it("reads a table by environment, dotted namespace and name", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales": {
        status: 200,
        body: TABLE,
      },
    });

    expect(await getTable(REF)).toEqual(TABLE);
    expect(server.sent[0]?.path).toBe(
      "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales",
    );
  });

  it("encodes what a segment cannot hold", () => {
    expect(tableApiPath({ environment: "a/b", namespace: "x y", name: "50%" }, "/health")).toBe(
      "/api/v1/environments/a%2Fb/namespaces/x%20y/tables/50%25/health",
    );
  });

  it("asks what the person may do on the table", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/admin/permissions": {
        status: 200,
        body: { permissions: [] },
      },
      "GET /api/v1/principals/kim lee/permissions": { status: 200, body: { permissions: [] } },
    });

    await myPermissions("admin", {
      environment: "prod",
      namespace: "gold.sales",
      table: "fact_sales",
    });
    await myPermissions("kim lee", { environment: "prod" });

    expect(server.sent[0]?.path).toBe(
      "/api/v1/principals/admin/permissions?environment=prod&namespace=gold.sales&table=fact_sales",
    );
    expect(server.sent[1]?.path).toBe("/api/v1/principals/kim%20lee/permissions?environment=prod");
  });

  it("reads health for seven days, without the readers when asked", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales/health": {
        status: 200,
        body: HEALTH,
      },
    });

    expect(await getTableHealth(REF, { readers: false })).toEqual(HEALTH);
    await getTableHealth(REF, { readers: true });

    expect(server.sent[0]?.path).toMatch(/\/health\?days=7&readers=false$/);
    expect(server.sent[1]?.path).toMatch(/\/health\?days=7$/);
  });

  it("reads one step of lineage from a node, up and down, at most 500 nodes", async () => {
    const server = fakeServer({
      [`GET ${LINEAGE_PATH}`]: (request) => ({
        status: 200,
        body: request.path.includes("direction=up") ? UP : DOWN,
      }),
    });

    expect(await getLineage("prod", "table:abc", "up", 1)).toEqual(UP);
    expect(await getLineage("prod", "table:abc", "down", 1)).toEqual(DOWN);

    expect(server.sent[0]?.path).toBe(
      `${LINEAGE_PATH}?node=table%3Aabc&direction=up&depth=1&limit=500`,
    );
    expect(server.sent[1]?.path).toBe(
      `${LINEAGE_PATH}?node=table%3Aabc&direction=down&depth=1&limit=500`,
    );
  });
});
