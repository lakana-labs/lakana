/**
 * Stubs for the end-to-end tests of the Queries screen: the environments a person may query
 * (`prod`, protected, and `dev`), a run's life (queued, running, succeeded), a run that fails with
 * a position, a run that goes on until it is cancelled, a page of a result, the two files, the
 * saved and recent lists, and the saved queries' writes (create, update and delete). What a write
 * changes shows in the reads after it.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

import { signInAsAdmin, STUBBED } from "./session";

/** The environments where the administrator may run a query; `prod` is protected. */
const ENVIRONMENTS = [
  { name: "prod", protected: true },
  { name: "dev", protected: false },
];

/** The run that succeeds: queued at the first poll, running at the second, done after. */
export const RUN_ID = "0f8e2a6c-4b1d-4e9a-8c3f-2d7b6a5e9c10";

/** The run that fails with a position in its SQL. */
const FAILED_ID = "7c3d9e1f-5a2b-4c8d-9e0f-1a2b3c4d5e6f";

/** The run that goes on until it is cancelled. */
const LONG_ID = "4b9a1c7e-2d3f-4a5b-8c6d-7e8f9a0b1c2d";

/** A run of last week, in Recent from the start. */
const EARLIER_ID = "9d2e4f6a-8b0c-4d1e-a3f5-6b7c8d9e0f1a";

/** SQL the stubbed server answers with the succeeding run. */
export const SALES_SQL = "SELECT store, amount FROM gold.sales.fact_sales";

/** SQL that fails: nothing follows FROM, so the parser points at line 2, column 1. */
export const BROKEN_SQL = "SELECT 1\nFROM";

/** SQL that runs until it is cancelled. */
export const LONG_SQL = "SELECT count(*) FROM range(1000000000000)";

/** The server's sentence for the broken SQL. */
export const PARSER_ERROR = "Parser Error: syntax error at end of input";

/** Why the succeeding run waits at its first poll. */
const WAITING = { code: "user_limit", message: "Your other queries run first; this one waits." };

/** The CSV of the succeeding run. */
export const CSV = "store,amount\r\nBerlin,1200.50\r\nHamburg,980.00\r\nMünchen,1430.25\r\n";

/** The spreadsheet of the succeeding run: a zip, as every xlsx file is. */
const XLSX = Buffer.concat([Buffer.from("PK\u0003\u0004"), Buffer.alloc(60, 1)]);

/** The media type of a spreadsheet. */
const XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** The first page of the succeeding run. */
const PAGE = {
  columns: [
    { name: "store", type: "string" },
    { name: "amount", type: "decimal(10,2)" },
  ],
  columns_cut: 0,
  masked_columns: [],
  masked_columns_cut: 0,
  rows: [
    ["Berlin", "1200.50"],
    ["Hamburg", "980.00"],
    ["München", "1430.25"],
  ],
  row_cut: [],
  row_count: 3,
  next_offset: null,
  duration_ms: 2400,
  preview: null,
};

/** The id of the saved query that Save as new creates. */
const CREATED_ID = "e6f7a8b9-0c1d-4e2f-9a3b-4c5d6e7f8a9b";

/** The administrator's own saved query in `prod`. */
export const WEEKLY = {
  id: "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  environment: "prod",
  name: "Weekly sales",
  sql: "SELECT store, sum(amount) AS total\nFROM gold.sales.fact_sales\nGROUP BY store",
  visibility: "private",
  owner: "admin",
  former_owner: false,
  actions: ["update", "delete"],
  created_at: "2026-11-10T08:00:00Z",
  updated_at: "2026-11-12T08:00:00Z",
};

/** Kim's query, shared with everyone who may query `dev`. */
const STORES = {
  id: "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
  environment: "dev",
  name: "Stores by region",
  sql: "SELECT region, count(*) FROM gold.stores GROUP BY region",
  visibility: "everyone",
  owner: "kim.schulz",
  former_owner: false,
  actions: [],
  created_at: "2026-11-08T08:00:00Z",
  updated_at: "2026-11-08T08:00:00Z",
};

/** Kim's private query: the administrator sees it, without its SQL, and may delete it. */
export const DRAFT = {
  id: "3c4d5e6f-7a8b-4c9d-8e0f-2a3b4c5d6e7f",
  environment: "prod",
  name: "Kim's draft",
  visibility: "private",
  owner: "kim.schulz",
  former_owner: false,
  actions: ["delete"],
  created_at: "2026-11-09T08:00:00Z",
  updated_at: "2026-11-09T08:00:00Z",
};

/** The list of queries (Recent). */
const QUERIES = "/api/v1/queries";

/** The environments where the person may query. */
const QUERY_ENVIRONMENTS = "/api/v1/queries/environments";

/** The saved queries across environments. */
const SAVED = "/api/v1/saved-queries";

/** Every run's id, for the paths below. */
const RUN_IDS = [RUN_ID, FAILED_ID, LONG_ID, EARLIER_ID];

/** Every saved query's id, for the paths below. */
const SAVED_IDS = [WEEKLY.id, STORES.id, DRAFT.id, CREATED_ID];

/**
 * The path of a part of an environment.
 *
 * @param environment - The environment.
 * @param suffix - What follows its name.
 * @returns The path.
 */
function at(environment: string, suffix: string): string {
  return `/api/v1/environments/${environment}${suffix}`;
}

/** The paths these stubs answer, beside those of `session.ts`. */
export const QUERIES_STUBBED = [
  ...STUBBED,
  QUERIES,
  SAVED,
  ...ENVIRONMENTS.flatMap(({ name }) => [
    at(name, "/queries"),
    at(name, "/saved-queries"),
    ...RUN_IDS.flatMap((id) =>
      ["", "/result", "/cancel"].map((s) => at(name, `/queries/${id}${s}`)),
    ),
    ...SAVED_IDS.map((id) => at(name, `/saved-queries/${id}`)),
  ]),
];

/** A run the stubbed server knows: its SQL, environment, polls so far and whether cancelled. */
interface StubRun {
  readonly id: string;
  sql: string;
  environment: string;
  polls: number;
  cancelled: boolean;
  created: string;
}

/** A saved query as the stubs keep it. */
type Saved = Record<string, unknown> & { id: string; environment: string; name: string };

/**
 * The id the stubbed server gives a submitted SQL text.
 *
 * @param sql - The text.
 * @returns The run's id.
 */
function runFor(sql: string): string {
  if (sql.includes("range(")) {
    return LONG_ID;
  }
  return /FROM\s*$/u.test(sql) ? FAILED_ID : RUN_ID;
}

/**
 * A run's state at this poll, as `GET …/queries/{id}` answers it.
 *
 * @param run - The run.
 * @returns The status body.
 */
function status(run: StubRun): Record<string, unknown> {
  const base = {
    query_id: run.id,
    origin: "written",
    sql: run.sql,
    error_message: null,
    error_cause: null,
    error_line: null,
    error_column: null,
    result_rows: null,
    result_bytes: null,
    created_at: run.created,
    started_at: null,
    finished_at: null,
    waiting_for: null,
  };
  const started = { started_at: "2026-11-17T09:00:01Z" };
  const ended = { ...started, finished_at: "2026-11-17T09:00:03.400Z" };
  if (run.cancelled) {
    return {
      ...base,
      ...ended,
      status: "CANCELLED",
      error_message: "Cancelled by admin.",
      error_cause: "cancelled",
    };
  }
  if (run.id === FAILED_ID) {
    return run.polls < 1
      ? { ...base, ...started, status: "RUNNING" }
      : {
          ...base,
          ...ended,
          status: "FAILED",
          error_message: PARSER_ERROR,
          error_cause: "invalid_query",
          error_line: 2,
          error_column: 1,
        };
  }
  if (run.polls < 1) {
    return { ...base, status: "QUEUED", waiting_for: WAITING };
  }
  if (run.id === LONG_ID || run.polls < 2) {
    return { ...base, ...started, status: "RUNNING" };
  }
  return { ...base, ...ended, status: "SUCCEEDED", result_rows: 3, result_bytes: 1_234 };
}

/**
 * A run as the list of queries (Recent) answers it: never its SQL.
 *
 * @param run - The run.
 * @returns The row.
 */
function listed(run: StubRun): Record<string, unknown> {
  const now = status(run);
  return {
    id: run.id,
    environment: run.environment,
    origin: "written",
    submitter: "admin",
    kind: "read",
    target: null,
    status: now["status"],
    created_at: run.created,
    started_at: now["started_at"],
    finished_at: now["finished_at"],
    error_cause: now["error_cause"],
    waiting_for: now["waiting_for"],
  };
}

/**
 * Answers a run's file in the format it asks for.
 *
 * @param route - The route of `GET …/result`.
 * @returns Resolves once answered.
 */
function resultAnswer(route: Route): Promise<void> {
  const format = new URL(route.request().url()).searchParams.get("format");
  if (format === "csv") {
    return route.fulfill({ contentType: "text/csv; charset=utf-8", body: CSV });
  }
  if (format === "xlsx") {
    return route.fulfill({ contentType: XLSX_TYPE, body: XLSX });
  }
  return route.fulfill({ json: PAGE });
}

/**
 * Signs in as an administrator and answers the Queries screen's routes like a server would: the
 * two environments, a submit (the SQL decides which run it starts: {@link SALES_SQL} and any other
 * succeeds, {@link BROKEN_SQL} fails, {@link LONG_SQL} goes on until cancelled), each run's polls,
 * its page and its two files, the cancel, Recent (the runs submitted here, newest first, and one
 * of last week) and the saved queries with their create, update and delete.
 *
 * @param page - The page.
 * @returns The mutating requests that reached the stubs, in order, as `METHOD path`.
 */
export async function stubQueries(page: Page): Promise<string[]> {
  await signInAsAdmin(page);
  const sent: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && path.includes("queries")) {
      sent.push(`${request.method()} ${path}`);
    }
  });
  const runs = new Map<string, StubRun>([
    [
      EARLIER_ID,
      {
        id: EARLIER_ID,
        sql: SALES_SQL,
        environment: "prod",
        polls: 5,
        cancelled: false,
        created: "2026-11-12T09:00:00Z",
      },
    ],
  ]);
  let saved: Saved[] = [WEEKLY, STORES, DRAFT];

  await page.route(
    (url) => url.pathname === QUERY_ENVIRONMENTS,
    (route) => route.fulfill({ json: ENVIRONMENTS }),
  );
  await page.route(
    (url) => url.pathname === QUERIES,
    (route) => {
      const environment = new URL(route.request().url()).searchParams.get("environment");
      const items = [...runs.values()]
        .filter((run) => environment === null || run.environment === environment)
        .sort((a, b) => b.created.localeCompare(a.created))
        .map(listed);
      return route.fulfill({ json: { items, next_cursor: null } });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/[^/]+\/queries(\/|$)/u.test(url.pathname),
    async (route) => {
      const request = route.request();
      const [, environment = "", rest = ""] =
        /^\/api\/v1\/environments\/([^/]+)\/queries\/?(.*)$/u.exec(
          new URL(request.url()).pathname,
        ) ?? [];
      if (rest === "") {
        const { sql } = request.postDataJSON() as { sql: string };
        const id = runFor(sql);
        runs.set(id, {
          id,
          sql,
          environment,
          polls: 0,
          cancelled: false,
          created: `2026-11-17T09:00:0${String(runs.size)}Z`,
        });
        await route.fulfill({ status: 202, json: { query_id: id } });
        return;
      }
      const [id = "", part] = rest.split("/");
      const run = runs.get(id);
      if (run === undefined) {
        await route.fulfill({ status: 404, json: { detail: "No such query." } });
        return;
      }
      if (part === "cancel") {
        run.cancelled = true;
        await route.fulfill({ status: 202, json: { query_id: id, status: "CANCELLED" } });
      } else if (part === "result") {
        await resultAnswer(route);
      } else {
        const answer = status(run);
        run.polls += 1;
        await route.fulfill({ json: answer });
      }
    },
  );
  await page.route(
    (url) => url.pathname === SAVED,
    (route) => {
      const environment = new URL(route.request().url()).searchParams.get("environment");
      const items = saved
        .filter((one) => environment === null || one.environment === environment)
        .sort((a, b) => a.name.localeCompare(b.name));
      return route.fulfill({ json: { items, next_cursor: null } });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/[^/]+\/saved-queries(\/|$)/u.test(url.pathname),
    async (route) => {
      const request = route.request();
      const [, environment = "", id = ""] =
        /^\/api\/v1\/environments\/([^/]+)\/saved-queries\/?(.*)$/u.exec(
          new URL(request.url()).pathname,
        ) ?? [];
      const found = saved.find((one) => one.id === id && one.environment === environment);
      if (request.method() === "POST") {
        const body = request.postDataJSON() as Record<string, unknown>;
        if (saved.some((one) => one.owner === "admin" && one.name === body["name"])) {
          await route.fulfill({
            status: 409,
            json: {
              type: "SavedQueryNameTaken",
              detail: `You already have a saved query named ${String(body["name"])} in ${environment}.`,
            },
          });
          return;
        }
        const created = {
          ...WEEKLY,
          ...body,
          id: CREATED_ID,
          environment,
          created_at: "2026-11-17T09:10:00Z",
          updated_at: "2026-11-17T09:10:00Z",
        } as Saved;
        saved = [...saved, created];
        await route.fulfill({ status: 201, json: created });
        return;
      }
      if (found === undefined) {
        await route.fulfill({ status: 404, json: { detail: "No such saved query." } });
        return;
      }
      if (request.method() === "PUT") {
        const changed = { ...found, ...(request.postDataJSON() as object) } as Saved;
        saved = saved.map((one) => (one.id === id ? changed : one));
        await route.fulfill({ json: changed });
      } else if (request.method() === "DELETE") {
        saved = saved.filter((one) => one.id !== id);
        await route.fulfill({ status: 204 });
      } else {
        await route.fulfill({ json: found });
      }
    },
  );
  return sent;
}
