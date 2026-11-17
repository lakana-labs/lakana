/**
 * What the server answers the Queries screen with, for tests: the environments one may query
 * (`prod`, protected, and `dev`), a finished run, a saved query, and the routes that serve them.
 *
 * @packageDocumentation
 */
import type {
  QueryEnvironmentOut,
  QueryListItemOut,
  QueryOrigin,
  QueryRunOut,
  QueryWaitingOut,
} from "../api/queries";
import type { SavedQueryOut } from "../api/savedQueries";
import type { ResultPageOut } from "../api/table";

import type { FakeRoute } from "./server";

/** The id of the run `aRun` answers for. */
export const RUN_ID = "0b1c2d3e-4f50-4a61-b072-8394a5b6c7d8";

/** The environments the person may query: `dev` and the protected `prod`. */
export const QUERY_ENVIRONMENTS: readonly QueryEnvironmentOut[] = [
  { name: "dev", protected: false },
  { name: "prod", protected: true },
];

/** Where the runs of these fixtures came from: a person wrote them. */
const WRITTEN: QueryOrigin = "written";

/** Why a queued run waits, as the server words it for a full queue of the person's own. */
export const WAITING: QueryWaitingOut = {
  code: "user_limit",
  message: "You already run 2 queries; this one starts when one ends.",
};

/**
 * A run of `select 1 as one`, finished and succeeded.
 *
 * @param over - The fields that differ.
 * @returns The run, as the status read answers it.
 */
export function aRun(over: Partial<QueryRunOut> = {}): QueryRunOut {
  return {
    query_id: RUN_ID,
    status: "SUCCEEDED",
    origin: WRITTEN,
    sql: "select 1 as one",
    error_message: null,
    error_cause: null,
    error_line: null,
    error_column: null,
    result_rows: 1,
    result_bytes: 512,
    created_at: "2026-11-16T09:00:00Z",
    started_at: "2026-11-16T09:00:01Z",
    finished_at: "2026-11-16T09:00:02Z",
    waiting_for: null,
    ...over,
  };
}

/**
 * The first page of a run's result: one column `one` and one row, nothing masked or cut, the end.
 *
 * @param over - The fields that differ.
 * @returns The page, as the JSON result read answers it.
 */
export function aPage(over: Partial<ResultPageOut> = {}): ResultPageOut {
  return {
    columns: [{ name: "one", type: "int32" }],
    columns_cut: 0,
    masked_columns: [],
    masked_columns_cut: 0,
    rows: [[1]],
    row_cut: [],
    row_count: 1,
    next_offset: null,
    duration_ms: 1_000,
    preview: null,
    ...over,
  };
}

/**
 * One row of the list of recent queries.
 *
 * @param over - The fields that differ.
 * @returns The row.
 */
export function aListedRun(over: Partial<QueryListItemOut> = {}): QueryListItemOut {
  return {
    id: RUN_ID,
    environment: "prod",
    origin: WRITTEN,
    submitter: "kim.lee",
    kind: "read",
    target: null,
    status: "SUCCEEDED",
    created_at: "2026-11-16T09:00:00Z",
    started_at: "2026-11-16T09:00:01Z",
    finished_at: "2026-11-16T09:00:02Z",
    error_cause: null,
    waiting_for: null,
    ...over,
  };
}

/**
 * A saved query of the person's own, shared with no one.
 *
 * @param over - The fields that differ.
 * @returns The saved query.
 */
export function aSavedQuery(over: Partial<SavedQueryOut> = {}): SavedQueryOut {
  return {
    id: "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d",
    environment: "prod",
    name: "Daily sales",
    sql: "select 1 as one",
    visibility: "private",
    owner: "kim.lee",
    former_owner: false,
    actions: ["update", "delete"],
    created_at: "2026-11-10T08:00:00Z",
    updated_at: "2026-11-12T08:00:00Z",
    ...over,
  };
}

/**
 * The routes of the Queries screen: the environments, the lists, the run and the saved query.
 *
 * @returns The routes, keyed `"METHOD /path"`.
 */
export function queryRoutes(): Record<string, FakeRoute> {
  return {
    "GET /api/v1/queries/environments": { status: 200, body: QUERY_ENVIRONMENTS },
    "GET /api/v1/queries": { status: 200, body: { items: [aListedRun()], next_cursor: null } },
    "GET /api/v1/saved-queries": {
      status: 200,
      body: { items: [aSavedQuery()], next_cursor: null },
    },
    [`GET /api/v1/environments/prod/queries/${RUN_ID}`]: { status: 200, body: aRun() },
    "GET /api/v1/environments/prod/saved-queries/6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d": {
      status: 200,
      body: aSavedQuery(),
    },
  };
}
