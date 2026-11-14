import { describe, expect, it } from "vitest";

import { fakeServer } from "../test/server";
import {
  DEFINITION,
  definitionFile,
  DOWN,
  FILTERS,
  GRANTS,
  GRANTS_PATH,
  HEALTH,
  LINEAGE_PATH,
  MASKS,
  QUERIES_PATH,
  QUERY_ID,
  queryStatus,
  RESULT,
  TABLE,
  TABLE_PATH,
  UP,
} from "../test/table";
import {
  ACTIVE_STATUSES,
  cancelQuery,
  describeColumn,
  describeTable,
  getDefinition,
  getDefinitionFile,
  getLineage,
  getQueryStatus,
  getResultPage,
  getTable,
  getTableHealth,
  listColumnMasks,
  listGrantsAt,
  listRowFilters,
  myPermissions,
  submitPreview,
  tableApiPath,
} from "./table";

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

  it("sets a table's texts in one language and answers the changed table", async () => {
    const changed = { ...TABLE, description: "New." };
    const server = fakeServer({ [`PATCH ${TABLE_PATH}`]: { status: 200, body: changed } });

    expect(await describeTable(REF, { description: "New.", label: null }, "de")).toEqual(changed);

    expect(server.sent[0]?.path).toBe(`${TABLE_PATH}?lang=de`);
    expect(server.sent[0]?.body).toEqual({ description: "New.", label: null });
  });

  it("sets a column's texts, encoding the column's name", async () => {
    const server = fakeServer({
      [`PUT ${TABLE_PATH}/columns/50% off/description`]: { status: 200, body: TABLE },
    });

    expect(await describeColumn(REF, "50% off", { description: null }, "en")).toEqual(TABLE);

    expect(server.sent[0]?.path).toBe(`${TABLE_PATH}/columns/50%25%20off/description?lang=en`);
    expect(server.sent[0]?.body).toEqual({ description: null });
  });

  it("reads where a table is defined, and its file only from the file's own path", async () => {
    const file = definitionFile();
    const server = fakeServer({
      [`GET ${TABLE_PATH}/definition`]: { status: 200, body: DEFINITION },
      [`GET ${TABLE_PATH}/definition/file`]: { status: 200, body: file },
    });

    expect(await getDefinition(REF)).toEqual(DEFINITION);
    expect(await getDefinitionFile(REF)).toEqual(file);

    expect(server.sent.map((request) => request.path)).toEqual([
      `${TABLE_PATH}/definition`,
      `${TABLE_PATH}/definition/file`,
    ]);
  });

  it("queues a preview with no body and answers its query's id", async () => {
    const server = fakeServer({
      [`POST ${TABLE_PATH}/preview`]: { status: 202, body: { query_id: QUERY_ID } },
    });

    expect(await submitPreview(REF)).toBe(QUERY_ID);

    expect(server.sent[0]?.path).toBe(`${TABLE_PATH}/preview`);
    expect(server.sent[0]?.body).toBeUndefined();
  });

  it("polls a query, reads its page as JSON of at most 100 rows and cancels it, in the background", async () => {
    const server = fakeServer({
      [`GET ${QUERIES_PATH}/${QUERY_ID}`]: { status: 200, body: queryStatus("RUNNING") },
      [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: { status: 200, body: RESULT },
      [`POST ${QUERIES_PATH}/${QUERY_ID}/cancel`]: { status: 200, body: queryStatus("CANCELLED") },
    });

    expect(await getQueryStatus("prod", QUERY_ID)).toEqual(queryStatus("RUNNING"));
    expect(await getResultPage("prod", QUERY_ID)).toEqual(RESULT);
    await expect(cancelQuery("prod", QUERY_ID)).resolves.toBeUndefined();

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      `GET ${QUERIES_PATH}/${QUERY_ID}`,
      `GET ${QUERIES_PATH}/${QUERY_ID}/result?format=json&limit=100`,
      `POST ${QUERIES_PATH}/${QUERY_ID}/cancel`,
    ]);
    expect(server.sent.every((request) => request.headers["x-lakana-background"] === "1")).toBe(
      true,
    );
  });

  it("names the active states of a run in the server's upper case", () => {
    expect(ACTIVE_STATUSES).toEqual(["QUEUED", "RUNNING"]);
  });

  it("lists the grants on exactly one level, at most 200, and says whether the server has more", async () => {
    const server = fakeServer({
      [`GET ${GRANTS_PATH}`]: (request) => ({
        status: 200,
        body: GRANTS,
        ...(request.path.includes("namespace=gold&")
          ? { headers: { "X-Next-Cursor": "abc" } }
          : {}),
      }),
    });

    expect(
      await listGrantsAt({ environment: "prod", namespace: "gold.sales", table: "fact_sales" }),
    ).toEqual({ grants: GRANTS, more: false });
    expect(await listGrantsAt({ environment: "prod", namespace: "gold", table: null })).toEqual({
      grants: GRANTS,
      more: true,
    });
    await listGrantsAt({ environment: "prod", namespace: null, table: null });

    expect(server.sent.map((request) => request.path)).toEqual([
      `${GRANTS_PATH}?environment=prod&namespace=gold.sales&table=fact_sales&limit=200`,
      `${GRANTS_PATH}?environment=prod&namespace=gold&limit=200`,
      `${GRANTS_PATH}?environment=prod&limit=200`,
    ]);
  });

  it("lists a table's row filters and column masks by its environment, namespace and name", async () => {
    const server = fakeServer({
      "GET /api/v1/row-filters": { status: 200, body: FILTERS },
      "GET /api/v1/column-masks": { status: 200, body: MASKS },
    });

    expect(await listRowFilters(REF)).toEqual(FILTERS);
    expect(await listColumnMasks(REF)).toEqual(MASKS);

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/row-filters?environment=prod&namespace=gold.sales&table=fact_sales",
      "/api/v1/column-masks?environment=prod&namespace=gold.sales&table=fact_sales",
    ]);
  });
});
