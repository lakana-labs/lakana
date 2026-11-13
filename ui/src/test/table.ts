/**
 * What the server answers the table page with, for tests: `prod/gold.sales/fact_sales`, a fact
 * table owned by Ana, and a builder for variations of it.
 *
 * @packageDocumentation
 */
import type { ColumnOut, PermissionsOut, TableOut } from "../api";
import type { FakeRoute } from "./server";

/**
 * One column.
 *
 * @param name - The column's name.
 * @param type - Its Iceberg type.
 * @param overrides - The fields that differ.
 * @returns The column.
 */
function column(name: string, type: string, overrides: Partial<ColumnOut> = {}): ColumnOut {
  return {
    name,
    type,
    nullable: true,
    description: null,
    description_source: null,
    label: null,
    label_source: null,
    label_language: null,
    description_language: null,
    terms: [],
    ...overrides,
  };
}

/** A fact table in prod.gold.sales: a key, a reference, a measure and a column with personal data. */
export const TABLE: TableOut = {
  name: "fact_sales",
  namespace: "gold.sales",
  uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
  kind: "TABLE",
  metadata_location: "s3://warehouse/gold/sales/fact_sales/metadata/00001.metadata.json",
  schema: [
    column("sale_id", "long", { nullable: false }),
    column("store_id", "long"),
    column("sold_at", "timestamp"),
    column("amount", "decimal(10,2)"),
    column("customer_email", "string"),
    column("channel", "string"),
    column("note", "string"),
  ],
  sql: null,
  references: null,
  hidden_references: 0,
  snapshot_id: 4821,
  state: "published",
  state_message: null,
  description: "Every sale in every store, one row per sale.",
  description_source: "project",
  last_write_at: "2026-11-12T06:00:00Z",
  row_count: 1_204_000,
  size_bytes: 3_100_000,
  file_count: 12,
  expires_at: null,
  expires_soon: false,
  model: {
    kind: "fact",
    grain: "one row per sale",
    key: ["sale_id"],
    key_none: false,
    references: [
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
    hidden_references: 0,
    measures: [
      {
        id: "gold.sales.fact_sales/measure_amount",
        column: "amount",
        aggregation: "sum",
        additivity: "additive",
        not_over: [],
        unit: null,
        currency: "EUR",
        label: null,
        description: null,
      },
    ],
    checks: [],
  },
  label: null,
  label_source: null,
  label_language: null,
  description_language: "en",
  language: "en",
  main_language: "en",
  translations: [],
  terms: [],
  health: {},
  health_missing: [],
  health_never_judged: {},
  owner: { name: "ana.nowak", kind: "USER" },
  created_at: "2026-10-01T08:00:00Z",
  last_read_at: "2026-11-13T07:30:00Z",
  readers_7d: 3,
  health_summary: { state: "healthy", item: "healthy", text: "Healthy", others: 0 },
};

/**
 * The fact table with some fields changed.
 *
 * @param overrides - The fields that differ.
 * @returns The table.
 */
export function table(overrides: Partial<TableOut> = {}): TableOut {
  return { ...TABLE, ...overrides };
}

/**
 * What a person may do on a table.
 *
 * @param person - The person's principal name.
 * @param allowed - The actions the person may do; every other one is refused.
 * @returns The answer.
 */
export function permissions(person: string, allowed: readonly string[]): PermissionsOut {
  const actions = ["READ_TABLE", "WRITE_TABLE", "DROP_TABLE", "SUBMIT_QUERY"];
  return {
    principal: person,
    active: true,
    scope: { kind: "TABLE", environment: "prod", namespace: "gold.sales", table: "fact_sales" },
    permissions: actions.map((action) => ({
      action,
      allowed: allowed.includes(action),
      via: [],
      reason: allowed.includes(action) ? null : "No grant gives it.",
    })),
    note: null,
  };
}

/** The path of the fact table on the server. */
export const TABLE_PATH = "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";

/**
 * The routes the table page reads and writes for a person: the table, what they may do, and
 * the record that they opened it. The person may do everything.
 *
 * @param person - The person's principal name.
 * @returns The routes.
 */
export function tableRoutes(person = "admin"): Record<string, FakeRoute> {
  return {
    [`GET ${TABLE_PATH}`]: { status: 200, body: TABLE },
    [`GET /api/v1/principals/${person}/permissions`]: {
      status: 200,
      body: permissions(person, ["READ_TABLE", "WRITE_TABLE", "DROP_TABLE", "SUBMIT_QUERY"]),
    },
    [`POST /api/v1/principals/${person}/recent`]: { status: 204 },
  };
}
