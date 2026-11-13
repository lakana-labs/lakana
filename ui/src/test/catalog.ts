/**
 * What the server answers the catalog list with, for tests: the tree of two environments and a
 * page of realistic tables, and a builder for one more row.
 *
 * @packageDocumentation
 */
import type { TableListItemOut, TableListPageOut, TableTreeOut } from "../api/catalog";
import type { FakeRoute } from "./server";

/** Two environments, the medallion namespaces and one nested namespace. */
export const TREE: TableTreeOut = {
  count: 9,
  environments: [
    {
      name: "dev",
      count: 3,
      namespaces: [
        { path: "bronze", count: 1 },
        { path: "gold", count: 2 },
      ],
    },
    {
      name: "prod",
      count: 6,
      namespaces: [
        { path: "bronze", count: 1 },
        { path: "silver", count: 1 },
        { path: "gold", count: 4 },
        { path: "gold.sales", count: 1 },
      ],
    },
  ],
};

/**
 * One row of the list: a published table in `prod.gold`, owned by Kim, healthy.
 *
 * @param overrides - The fields that differ.
 * @returns The row.
 */
export function item(overrides: Partial<TableListItemOut> = {}): TableListItemOut {
  return {
    environment: "prod",
    namespace: "gold",
    name: "station_ranking",
    uuid: "5d0c7a1e-3f51-4a0e-9b0e-2a6f4c1d8e11",
    kind: "TABLE",
    model_kind: null,
    state: "published",
    owner: { name: "kim.schulz", kind: "USER" },
    created_at: "2026-10-01T08:00:00Z",
    row_count: 48210,
    size_bytes: 3_100_000,
    last_write_at: "2026-11-12T06:00:00Z",
    last_read_at: "2026-11-13T07:30:00Z",
    readers_7d: 4,
    health_summary: { state: "healthy", item: "healthy", text: "Healthy", others: 0 },
    match: null,
    ...overrides,
  };
}

/** A page of three tables with no further page. */
export const PAGE: TableListPageOut = {
  items: [
    item(),
    item({
      namespace: "gold.sales",
      name: "fact_sales",
      uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
      model_kind: "fact",
      owner: { name: "ana.nowak", kind: "USER" },
      row_count: 1_204_000,
      size_bytes: 98_304,
      health_summary: {
        state: "warning",
        item: "freshness",
        text: "Last write was 30 hours ago.",
        others: 1,
      },
    }),
    item({
      name: "dim_store",
      uuid: "c7e2b5f0-1d34-4e8a-a6b9-3f5d0c8e7a63",
      model_kind: "dimension",
      owner: { name: "loader", kind: "SERVICE_ACCOUNT" },
      row_count: null,
      size_bytes: null,
      last_write_at: null,
      last_read_at: null,
      readers_7d: 0,
      health_summary: { state: "not_judged", item: "not_judged", text: "Not judged", others: 0 },
    }),
  ],
  next_cursor: null,
  capped: false,
};

/**
 * The two routes the catalog list reads, answering with `TREE` and `PAGE`.
 *
 * @returns The routes.
 */
export function catalogRoutes(): Record<string, FakeRoute> {
  return {
    "GET /api/v1/tables/tree": { status: 200, body: TREE },
    "GET /api/v1/tables": { status: 200, body: PAGE },
  };
}
