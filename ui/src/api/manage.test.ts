import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import {
  dropArtifact,
  dropTable,
  getReferences,
  getRetention,
  isProtected,
  listDropped,
  listExpiring,
  listOrphans,
  maintainTable,
  MAINTENANCE_DEFAULTS,
  rebuildTable,
  setRetention,
  undropTable,
  type RetentionRule,
} from "./manage";
import type { TableRef } from "./table";

import { dropped, manageRoutes, orphan, references, REBUILT, retention } from "../test/manage";
import { fakeServer, withoutSessionReads } from "../test/server";
import { TABLE } from "../test/table";

const REF: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const BASE = "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";

/**
 * Splits a sent path into its path and query.
 *
 * @param path - The path with its query.
 * @returns The path and its query parameters.
 */
function parts(path: string): { pathname: string; query: URLSearchParams } {
  const url = new URL(path, "http://x");
  return { pathname: url.pathname, query: url.searchParams };
}

describe("manage api", () => {
  it("drops with purge and a break-glass reason in the query", async () => {
    const server = fakeServer(manageRoutes());

    await dropTable(REF, { purge: true, breakGlassReason: "fix it now please" });

    const { pathname, query } = parts(withoutSessionReads(server)[0]?.path ?? "");
    expect(withoutSessionReads(server)[0]?.method).toBe("DELETE");
    expect(pathname).toBe(BASE);
    expect(query.get("purge")).toBe("true");
    expect(query.get("break_glass_reason")).toBe("fix it now please");
  });

  it("drops without a query when neither is set", async () => {
    const server = fakeServer(manageRoutes());

    await dropTable(REF, { purge: false, breakGlassReason: null });

    expect(withoutSessionReads(server)[0]?.path).toBe(BASE);
  });

  it("undrops with new_name and break_glass_reason in the body", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      [`POST ${BASE}/undrop`]: { status: 200, body: TABLE },
    });

    expect(await undropTable(REF, { newName: "old", breakGlassReason: null })).toEqual(TABLE);

    expect(withoutSessionReads(server)[0]?.method).toBe("POST");
    expect(withoutSessionReads(server)[0]?.path).toBe(`${BASE}/undrop`);
    expect(withoutSessionReads(server)[0]?.body).toEqual({
      new_name: "old",
      break_glass_reason: null,
    });
  });

  it("rebuilds with the reason, and as break-glass sends it in the query too", async () => {
    const server = fakeServer(manageRoutes());

    expect(await rebuildTable(REF, "schema changed", false)).toEqual(REBUILT);
    await rebuildTable(REF, "schema changed", true);

    expect(withoutSessionReads(server)[0]?.path).toBe(`${BASE}/rebuild`);
    expect(withoutSessionReads(server)[0]?.body).toEqual({ reason: "schema changed" });
    const second = parts(withoutSessionReads(server)[1]?.path ?? "");
    expect(second.pathname).toBe(`${BASE}/rebuild`);
    expect(second.query.get("break_glass_reason")).toBe("schema changed");
    expect(withoutSessionReads(server)[1]?.body).toEqual({ reason: "schema changed" });
  });

  it("maintains with snake_case options and answers the query id", async () => {
    const server = fakeServer(manageRoutes());

    expect(await maintainTable(REF, MAINTENANCE_DEFAULTS)).toBe("q-1");

    expect(withoutSessionReads(server)[0]?.path).toBe(`${BASE}/maintenance`);
    expect(withoutSessionReads(server)[0]?.body).toEqual({
      keep_days: 7,
      keep_last: 5,
      dry_run: false,
      older_than_hours: 72,
    });
  });

  it("reads retention", async () => {
    const server = fakeServer(manageRoutes());

    expect(await getRetention(REF)).toEqual(retention());

    expect(withoutSessionReads(server)[0]?.path).toBe(`${BASE}/retention`);
  });

  it.each<[RetentionRule, unknown]>([
    [{ days: 30 }, { days: 30 }],
    [{ on: "2026-12-01" }, { on: "2026-12-01" }],
    [{ never: true }, { never: true }],
  ])("sets retention %j", async (rule, body) => {
    const server = fakeServer(manageRoutes());

    expect(await setRetention(REF, rule)).toEqual(retention());

    expect(withoutSessionReads(server)[0]?.method).toBe("PUT");
    expect(withoutSessionReads(server)[0]?.path).toBe(`${BASE}/retention`);
    expect(withoutSessionReads(server)[0]?.body).toEqual(body);
  });

  it("reads references", async () => {
    const server = fakeServer(manageRoutes());

    expect(await getReferences(REF)).toEqual(references());

    expect(withoutSessionReads(server)[0]?.path).toBe(`${BASE}/references`);
  });

  it("drops an artifact through the artifact route", async () => {
    const server = fakeServer(manageRoutes());
    const ref = { environment: "prod", namespace: "ml", name: "model" };

    await dropArtifact(ref, null);
    await dropArtifact(ref, "clean up now please");

    expect(withoutSessionReads(server)[0]?.method).toBe("DELETE");
    expect(withoutSessionReads(server)[0]?.path).toBe(
      "/api/v1/environments/prod/namespaces/ml/artifacts/model",
    );
    expect(parts(withoutSessionReads(server)[1]?.path ?? "").query.get("break_glass_reason")).toBe(
      "clean up now please",
    );
  });

  it("reads whether an environment is protected", async () => {
    const server = fakeServer(manageRoutes());

    expect(await isProtected("prod")).toBe(false);
    server.route({
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
    });
    expect(await isProtected("prod")).toBe(true);
  });

  it.each([403, 404])(
    "reads an unreadable environment's protection as null (%i)",
    async (status) => {
      fakeServer({ "GET /api/v1/environments/prod": { status, body: { detail: "no" } } });

      expect(await isProtected("prod")).toBeNull();
    },
  );

  it("throws any other failure when reading protection", async () => {
    fakeServer({ "GET /api/v1/environments/prod": { status: 500, body: { detail: "boom" } } });

    await expect(isProtected("prod")).rejects.toBeInstanceOf(ApiError);
  });

  it("lists dropped tables of the environment with its cursor from the body", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod/dropped-tables": {
        status: 200,
        body: { items: [dropped()], next_cursor: "c2" },
      },
    });

    expect(await listDropped("prod", null, "c1")).toEqual({
      items: [dropped()],
      nextCursor: "c2",
    });

    expect(withoutSessionReads(server)[0]?.path).toBe(
      "/api/v1/environments/prod/dropped-tables?limit=100&cursor=c1",
    );
  });

  it("lists dropped tables of the environment without a cursor on the first page", async () => {
    const server = fakeServer(manageRoutes());

    await listDropped("prod", null, null);

    expect(withoutSessionReads(server)[0]?.path).toBe(
      "/api/v1/environments/prod/dropped-tables?limit=100",
    );
  });

  it("lists dropped tables of one namespace as one page", async () => {
    const server = fakeServer(manageRoutes());

    expect(await listDropped("prod", "gold.sales", null)).toEqual({
      items: [dropped()],
      nextCursor: null,
    });

    expect(withoutSessionReads(server)[0]?.path).toBe(
      "/api/v1/environments/prod/namespaces/gold.sales/dropped-tables",
    );
  });

  it("lists expiring tables with the namespace and the cursor from X-Next-Cursor", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod/expiring-tables": {
        status: 200,
        body: [retention()],
        headers: { "X-Next-Cursor": "e2" },
      },
    });

    expect(await listExpiring("prod", "gold", null)).toEqual({
      items: [retention()],
      nextCursor: "e2",
    });
    await listExpiring("prod", null, "e1");

    const first = parts(withoutSessionReads(server)[0]?.path ?? "");
    expect(first.pathname).toBe("/api/v1/environments/prod/expiring-tables");
    expect(first.query.get("namespace")).toBe("gold");
    expect(first.query.get("limit")).toBe("100");
    expect(first.query.has("cursor")).toBe(false);
    const second = parts(withoutSessionReads(server)[1]?.path ?? "");
    expect(second.query.has("namespace")).toBe(false);
    expect(second.query.get("cursor")).toBe("e1");
  });

  it("lists orphans with their hidden count", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod/orphans": {
        status: 200,
        body: { orphans: [orphan()], hidden: 2, next_cursor: "o2" },
      },
    });

    expect(await listOrphans("prod", "o1")).toEqual({
      items: [orphan()],
      hidden: 2,
      nextCursor: "o2",
    });

    expect(withoutSessionReads(server)[0]?.path).toBe(
      "/api/v1/environments/prod/orphans?limit=100&cursor=o1",
    );
  });

  it("encodes every part of a path", async () => {
    const server = fakeServer({
      "DELETE /api/v1/environments/my env/namespaces/a b/tables/t 1": { status: 204 },
    });

    await dropTable(
      { environment: "my env", namespace: "a b", name: "t 1" },
      { purge: false, breakGlassReason: null },
    );

    expect(withoutSessionReads(server)[0]?.path).toBe(
      "/api/v1/environments/my%20env/namespaces/a%20b/tables/t%201",
    );
  });
});
