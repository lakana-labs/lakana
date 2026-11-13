/**
 * What the server answers the table page with, for tests: `prod/gold.sales/fact_sales`, a fact
 * table owned by Ana, and a builder for variations of it.
 *
 * @packageDocumentation
 */
import type {
  ColumnOut,
  GraphNodeOut,
  GraphOut,
  PermissionsOut,
  QueryRunStatus,
  QueryStatusOut,
  ResultPageOut,
  TableDefinitionFileOut,
  TableDefinitionOut,
  TableHealthOut,
  TableOut,
} from "../api";
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

/** The newest result of one check, as the server answers it; the type is not exported. */
type CheckResult = TableHealthOut["checks"][number];

/** One health signal, as the server answers it; the type is not exported. */
type SignalResult = TableHealthOut["signals"][number];

/**
 * A check that passed, with some fields changed.
 *
 * @param overrides - The fields that differ.
 * @returns The check's newest result.
 */
export function checkResult(overrides: Partial<CheckResult> = {}): CheckResult {
  return {
    name: "unique_sale_id",
    check: "unique",
    run_id: "5b1f0c52-0e0d-4b0a-9b53-6a53b8d2f001",
    outcome: "passed",
    on_failure: "fail",
    failing_rows: null,
    error: null,
    rollback_outcome: null,
    kept: false,
    not_published: false,
    finished_at: "2026-11-12T06:05:00Z",
    ...overrides,
  };
}

/**
 * A signal that did not fire, with some fields changed.
 *
 * @param overrides - The fields that differ.
 * @returns The signal's current state.
 */
export function signalResult(overrides: Partial<SignalResult> = {}): SignalResult {
  return {
    id: "0c7d7f0e-5c10-4b64-a3f0-5a0b9f6b1001",
    signal: "volume",
    column: null,
    state: "ok",
    effect: "alert",
    snapshot_id: 4821,
    observed_at: "2026-11-12T06:06:00Z",
    observed: null,
    band: null,
    alert: null,
    values_hidden: false,
    ...overrides,
  };
}

/** A healthy table's health: one check that passed and one signal that is within its band. */
export const HEALTH: TableHealthOut = {
  days: 7,
  last_maintenance: null,
  checks: [checkResult()],
  readers: null,
  hidden_checks: 0,
  signals: [signalResult()],
};

/**
 * The health with some fields changed.
 *
 * @param overrides - The fields that differ.
 * @returns The health.
 */
export function health(overrides: Partial<TableHealthOut> = {}): TableHealthOut {
  return { ...HEALTH, ...overrides };
}

/**
 * A node of the lineage.
 *
 * @param kind - The node kind: table, job or connection.
 * @param name - Its name; a dotted name for a table.
 * @param distance - Steps from the table.
 * @returns The node.
 */
function node(kind: string, name: string, distance: number): GraphNodeOut {
  return { id: `${kind}:${name}`, kind, name, distance };
}

/** The table's own node: its id is built from the table's uuid. */
export const ROOT: GraphNodeOut = {
  id: `table:${TABLE.uuid}`,
  kind: "table",
  name: "gold.sales.fact_sales",
  distance: 0,
};

/**
 * A lineage answer: the table's own node, then the others.
 *
 * @param others - The other nodes.
 * @returns The answer.
 */
export function lineage(others: readonly GraphNodeOut[]): GraphOut {
  return { nodes: [ROOT, ...others], edges: [], next_cursor: null };
}

/** What feeds the fact table: a staging table, the job that builds it and a connection. */
export const UP: GraphOut = lineage([
  node("table", "staging.stg_sales", 1),
  node("job", "shop/load_sales", 1),
  node("connection", "shop_db", 2),
]);

/** What the fact table feeds: one table. */
export const DOWN: GraphOut = lineage([node("table", "gold.sales.daily_sales", 1)]);

/** The path of the table's lineage on the server. */
export const LINEAGE_PATH = "/api/v1/environments/prod/graph/lineage";

/** Where the fact table is defined: line 1 of a file in `analytics`, on GitHub. */
export const DEFINITION: TableDefinitionOut = {
  defined: true,
  repository: "analytics",
  path: "models/gold/fact_sales.sql",
  commit: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
  line: 1,
  project_version_id: "7f3e2d1c-0b9a-4876-9543-210fedcba987",
  deployed_at: "2026-11-12T05:50:00Z",
  browse_url:
    "https://github.com/acme/analytics/blob/a1b2c3d4e5f60718293a4b5c6d7e8f9012345678/models/gold/fact_sales.sql#L1",
};

/** The text of the file that defines the fact table. */
const SOURCE = "/* lakana\ngrain: one row per sale\n*/\nSELECT * FROM silver.sales\n";

/**
 * The definition with the file's text, with some fields changed.
 *
 * @param overrides - The fields that differ.
 * @returns The answer of `GET .../definition/file`.
 */
export function definitionFile(
  overrides: Partial<TableDefinitionFileOut> = {},
): TableDefinitionFileOut {
  return {
    ...DEFINITION,
    content: SOURCE,
    bytes: SOURCE.length,
    reason: null,
    message: null,
    ...overrides,
  };
}

/** A view in the place of the fact table: it reads `gold.orders`. */
export const VIEW: TableOut = table({
  kind: "VIEW",
  metadata_location: null,
  sql: "SELECT * FROM gold.orders",
  references: [{ id: "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f", name: "gold.orders" }],
});

/** The path of the queries of `prod` on the server. */
export const QUERIES_PATH = "/api/v1/environments/prod/queries";

/** The id of the preview's query run. */
export const QUERY_ID = "5b0e7c1a-3d2f-4e6a-9b8c-7d6e5f4a3b2c";

/**
 * A query run's status, as the server answers it.
 *
 * @param status - The state, in the server's upper case.
 * @param overrides - The fields that differ.
 * @returns The answer of `GET .../queries/{id}`.
 */
export function queryStatus(
  status: QueryRunStatus,
  overrides: Partial<QueryStatusOut> = {},
): QueryStatusOut {
  return { query_id: QUERY_ID, status, error_message: null, error_cause: null, ...overrides };
}

/**
 * The fact table's preview page: two rows, `customer_email` masked for the reader, a bidi
 * character in a note, a binary column and a struct whose fields are named `binary` and `cut`.
 */
export const RESULT: ResultPageOut = {
  columns: [
    { name: "sale_id", type: "int64" },
    { name: "customer_email", type: "string" },
    { name: "note", type: "string" },
    { name: "receipt", type: "binary" },
    { name: "extra", type: "struct<binary: int32, cut: bool>" },
  ],
  columns_cut: 0,
  masked_columns: [{ table: "gold.sales.fact_sales", column: "customer_email" }],
  masked_columns_cut: 0,
  rows: [
    ["1", "****", "x\u202Ey", { binary: 12 }, { binary: 3, cut: true }],
    ["2", "****", null, null, null],
  ],
  row_cut: [],
  row_count: 2,
  next_offset: null,
  duration_ms: 800,
  preview: { table: "gold.sales.fact_sales" },
};

/**
 * The preview page with some fields changed.
 *
 * @param overrides - The fields that differ.
 * @returns The page.
 */
export function result(overrides: Partial<ResultPageOut> = {}): ResultPageOut {
  return { ...RESULT, ...overrides };
}

/** The view's preview page: the worker names the masked column of the base table it read. */
export const VIEW_RESULT: ResultPageOut = result({
  masked_columns: [{ table: "gold.orders", column: "email" }],
});

/**
 * The routes the table page reads and writes for a person: the table, what they may do, the
 * record that they opened it, its health, lineage, where it is defined and its preview, which
 * has finished at the first poll. The person may do everything.
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
    [`GET ${TABLE_PATH}/health`]: { status: 200, body: HEALTH },
    [`GET ${TABLE_PATH}/definition`]: { status: 200, body: DEFINITION },
    [`GET ${TABLE_PATH}/definition/file`]: { status: 200, body: definitionFile() },
    [`GET ${LINEAGE_PATH}`]: (request) => ({
      status: 200,
      body: request.path.includes("direction=up") ? UP : DOWN,
    }),
    [`POST ${TABLE_PATH}/preview`]: { status: 202, body: { query_id: QUERY_ID } },
    [`GET ${QUERIES_PATH}/${QUERY_ID}`]: { status: 200, body: queryStatus("SUCCEEDED") },
    [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: { status: 200, body: RESULT },
    [`POST ${QUERIES_PATH}/${QUERY_ID}/cancel`]: { status: 200, body: queryStatus("CANCELLED") },
  };
}
