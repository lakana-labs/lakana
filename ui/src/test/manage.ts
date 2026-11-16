/**
 * What the server answers the catalog-management calls with, for tests: the lists of dropped,
 * expiring and orphaned tables, a table's references and retention, and a rebuild.
 *
 * @packageDocumentation
 */
import type {
  DroppedTableOut,
  OrphanOut,
  TableReferencesOut,
  TableRebuildOut,
  TableRetentionOut,
} from "../api/manage";
import type { FakeRoute } from "./server";

/**
 * A dropped table, as the server lists it.
 *
 * @param overrides - The fields that differ.
 * @returns The dropped table.
 */
export function dropped(overrides: Partial<DroppedTableOut> = {}): DroppedTableOut {
  return {
    name: "fact_returns",
    namespace: "gold.sales",
    uuid: "5c1d7e90-2f3a-4b6c-9d8e-0a1b2c3d4e5f",
    kind: "TABLE",
    dropped_at: "2026-11-14T09:30:00Z",
    purge_after: "2026-11-21T09:30:00Z",
    restorable: true,
    actions: ["undrop"],
    ...overrides,
  };
}

/**
 * A table's retention, as the server answers it.
 *
 * @param overrides - The fields that differ.
 * @returns The retention.
 */
export function retention(overrides: Partial<TableRetentionOut> = {}): TableRetentionOut {
  return {
    name: "fact_sales",
    namespace: "gold.sales",
    uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
    source: "table",
    days: 30,
    on: null,
    never: false,
    set_by: "ana",
    set_at: "2026-11-01T08:00:00Z",
    last_activity_at: "2026-11-12T06:00:00Z",
    expires_at: "2026-12-12T06:00:00Z",
    kept: null,
    ...overrides,
  };
}

/**
 * An orphaned table, as the server lists it.
 *
 * @param overrides - The fields that differ.
 * @returns The orphan.
 */
export function orphan(overrides: Partial<OrphanOut> = {}): OrphanOut {
  return {
    repository: "sales-project",
    table: "gold.sales.old_totals",
    kind: "TABLE",
    exists: true,
    actions: ["drop"],
    ...overrides,
  };
}

/**
 * A table's references, as the server answers them.
 *
 * @param overrides - The fields that differ.
 * @returns The references.
 */
export function references(overrides: Partial<TableReferencesOut> = {}): TableReferencesOut {
  return {
    table: "gold.sales.fact_sales",
    outgoing: [
      {
        id: "gold.sales.fact_sales/ref_store_id",
        table: "gold.sales.fact_sales",
        column: "store_id",
        target: "gold.dim_store",
        target_column: "store_id",
        target_exists: true,
        at: null,
      },
    ],
    hidden_outgoing: 0,
    incoming: [],
    hidden_incoming: 0,
    ...overrides,
  };
}

/** What a rebuild answers: the run that builds the table again. */
export const REBUILT: TableRebuildOut = {
  table: "gold.sales.fact_sales",
  run_id: "9f1c2a3b-4d5e-4f60-8a7b-1c2d3e4f5a6b",
  repository: "sales-project",
  path: "assets/fact_sales.py",
};

/**
 * The routes of environment `prod` for the catalog-management calls, with the exact bodies the
 * server answers: a list of one dropped table, one expiring table and one orphan, the
 * references and retention of `gold.sales.fact_sales`, the answers of the writes, and the
 * creation of any namespace, the deletion of `silver`, and the registration of any external
 * table in `bronze`, whose columns are `order_id` and `amount`.
 *
 * @returns The routes, keyed `"METHOD /path"`.
 */
export function manageRoutes(): Record<string, FakeRoute> {
  const table = "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";
  return {
    "GET /api/v1/environments/prod": {
      status: 200,
      body: { name: "prod", is_protected: false },
    },
    "GET /api/v1/environments/prod/dropped-tables": {
      status: 200,
      body: { items: [dropped()], next_cursor: null },
    },
    "GET /api/v1/environments/prod/namespaces/gold.sales/dropped-tables": {
      status: 200,
      body: [dropped()],
    },
    "GET /api/v1/environments/prod/expiring-tables": { status: 200, body: [retention()] },
    "GET /api/v1/environments/prod/orphans": {
      status: 200,
      body: { orphans: [orphan()], hidden: 0, next_cursor: null },
    },
    [`GET ${table}/references`]: { status: 200, body: references() },
    [`GET ${table}/retention`]: { status: 200, body: retention() },
    [`PUT ${table}/retention`]: { status: 200, body: retention() },
    [`DELETE ${table}`]: { status: 204 },
    [`POST ${table}/rebuild`]: { status: 202, body: REBUILT },
    [`POST ${table}/maintenance`]: { status: 202, body: { query_id: "q-1" } },
    "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model": { status: 204 },
    "POST /api/v1/environments/prod/namespaces": (request) => ({
      status: 201,
      body: { path: (request.body as { path: string }).path, properties: {} },
    }),
    "DELETE /api/v1/environments/prod/namespaces/silver": { status: 204 },
    "POST /api/v1/environments/prod/namespaces/bronze/tables/external": (request) => {
      const sent = request.body as { name: string; location_pattern: string; format: string };
      return {
        status: 201,
        body: {
          name: sent.name,
          namespace: "bronze",
          location_pattern: sent.location_pattern,
          format: sent.format,
          schema: { order_id: "int64", amount: "double" },
        },
      };
    },
  };
}
