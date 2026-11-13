/**
 * The session routes, stubbed with `page.route`, so the interface runs against `vite preview`
 * without a server.
 *
 * @packageDocumentation
 */
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

/** The session of a signed-in administrator, as `GET /api/v1/sign-in/session` answers it. */
export const ADMIN_SESSION = {
  name: "admin",
  display_name: null,
  type: "USER",
  via: "passkey",
  step_up_until: null,
  needs_passkey: false,
  offer_passkey: false,
  absolute_expires_at: "2099-01-01T00:00:00Z",
};

/** The session lengths as a new instance has them. */
const SIGN_IN_SETTINGS = {
  idle_timeout_minutes: 720,
  absolute_lifetime_minutes: 10080,
  bounds: {
    idle_timeout_minutes_min: 15,
    idle_timeout_minutes_max: 1440,
    absolute_lifetime_minutes_min: 60,
    absolute_lifetime_minutes_max: 43200,
  },
};

const AT = "2026-11-10T08:00:00Z";

/** What Home reads, for an administrator of a team's instance in its first weeks. */
const HOME: Record<string, unknown> = {
  "/api/v1/status": {
    state: "degraded",
    causes: ["worker_lost"],
    workers_alive: 1,
    workers_total: 2,
    detail: {
      slots_busy: 1,
      slots_total: 2,
      memory_used_bytes: 1288490188,
      memory_limit_bytes: 4294967296,
      workers: [],
    },
  },
  "/api/v1/home/get-started": {
    audience: "admin",
    local: false,
    steps: [
      { id: "connect_repository", done: true },
      { id: "invite_team", done: false },
      { id: "add_passkey", done: true },
      { id: "deploy_project", done: false },
      { id: "ci_deploy", done: false },
    ],
    hidden: false,
    complete: false,
  },
  "/api/v1/home/attention": {
    items: [
      { kind: "run_failed", environment: "prod", title: "daily_sales", ref: "run-1", at: AT },
      {
        kind: "table_late",
        environment: "prod",
        title: "gold.station_ranking",
        ref: "gold.station_ranking",
        at: AT,
      },
      { kind: "worker_lost", environment: null, title: "worker-2.internal", ref: "w-2", at: AT },
    ],
    truncated: false,
  },
  "/api/v1/home/work": {
    repositories: [
      {
        name: "analytics",
        environments: [{ environment: "dev", last_deploy_status: "succeeded", last_deploy_at: AT }],
      },
    ],
    jobs: [
      {
        environment: "dev",
        repository: "analytics",
        job: "daily_sales",
        last_run_status: "succeeded",
        last_run_at: AT,
      },
    ],
  },
  "/api/v1/principals/admin/interface-state": {
    tour_seen: true,
    get_started_hidden: false,
    update_note_seen: "0.22.0",
  },
  "/api/v1/principals/admin/recent": [
    { kind: "table", environment: "prod", ref: "gold.station_ranking", opened_at: AT },
  ],
  "/api/v1/info": { version: "0.23.0" },
};

/** What the Catalog reads besides the list: a tree of two environments and an empty namespace. */
const CATALOG: Record<string, unknown> = {
  "/api/v1/tables/tree": {
    count: 4,
    environments: [
      { name: "dev", count: 1, namespaces: [{ path: "gold", count: 1 }] },
      {
        name: "prod",
        count: 3,
        namespaces: [
          { path: "gold", count: 3 },
          { path: "gold.sales", count: 1 },
          { path: "bronze", count: 0 },
        ],
      },
    ],
  },
};

/** The path of the list, which answers by its query (see {@link tablesAnswer}). */
const TABLES = "/api/v1/tables";

/**
 * A row of the list.
 *
 * @param row - The fields that differ from a table in prod.gold nobody has judged.
 * @returns The row as the server sends it.
 */
function tableRow(row: Record<string, unknown>): Record<string, unknown> {
  return {
    environment: "prod",
    namespace: "gold",
    uuid: "5d0c7a1e-3f51-4a0e-9b0e-2a6f4c1d8e11",
    kind: "TABLE",
    model_kind: null,
    state: "published",
    owner: { name: "kim.schulz", kind: "USER" },
    created_at: "2026-10-01T08:00:00Z",
    row_count: 48210,
    size_bytes: 83900000,
    last_write_at: "2026-11-09T06:00:00Z",
    last_read_at: "2026-11-10T07:30:00Z",
    readers_7d: 4,
    health_summary: { state: "healthy", item: "healthy", text: "Healthy", others: 0 },
    match: null,
    ...row,
  };
}

const STATION_RANKING = tableRow({ name: "station_ranking" });
const FACT_SALES = tableRow({
  environment: "prod",
  namespace: "gold.sales",
  name: "fact_sales",
  model_kind: "fact",
  owner: { name: "ana.nowak", kind: "USER" },
  row_count: 1204000,
  size_bytes: 98304,
  last_write_at: "2026-11-10T06:00:00Z",
  last_read_at: "2026-11-10T07:50:00Z",
  readers_7d: 2,
  health_summary: { state: "failing", item: "check_failed", text: "Check failed", others: 2 },
});
const DIM_STORE = tableRow({
  environment: "dev",
  name: "dim_store",
  kind: "VIEW",
  model_kind: "dimension",
  owner: { name: "loader", kind: "SERVICE_ACCOUNT" },
  row_count: null,
  size_bytes: null,
  last_write_at: null,
  last_read_at: null,
  readers_7d: 0,
  health_summary: { state: "not_judged", item: "not_judged", text: "Not judged", others: 0 },
});
const LATE_ARRIVAL = tableRow({
  name: "late_arrival",
  health_summary: { state: "warning", item: "late", text: "Late", others: 0 },
});

/**
 * What `GET /api/v1/tables` answers for a query: a search finds a column and a description, the
 * text "nothing" finds nothing, the namespace "bronze" holds no table, an environment "staging"
 * does not exist, and the first page has a second one behind its cursor.
 *
 * @param url - The request's URL.
 * @returns The status and the body.
 */
function tablesAnswer(url: URL): { status: number; json: unknown } {
  const query = url.searchParams;
  const page = (items: unknown[], next: string | null): { status: number; json: unknown } => ({
    status: 200,
    json: { items, next_cursor: next, capped: false },
  });
  const q = query.get("q");
  if (query.get("environment") === "staging") {
    return { status: 404, json: { detail: "No environment named staging." } };
  }
  if (query.get("namespace") === "bronze" || q === "nothing") {
    return page([], null);
  }
  if (q !== null) {
    return page(
      [
        {
          ...FACT_SALES,
          match: {
            kind: "column",
            matched: "name",
            column: "store_id",
            column_type: "bigint",
            snippet: null,
            language: null,
            translated: false,
          },
        },
        {
          ...STATION_RANKING,
          match: {
            kind: "table",
            matched: "description",
            column: null,
            column_type: null,
            snippet: `Every ${q} ranked by the trips it served`,
            language: "en",
            translated: false,
          },
        },
      ],
      null,
    );
  }
  if (query.get("cursor") !== null) {
    return page([LATE_ARRIVAL], null);
  }
  return page([STATION_RANKING, FACT_SALES, DIM_STORE], "page-two");
}

/** The path of the table page's table, `prod/gold.sales/fact_sales`. */
const TABLE_PATH = "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";

/** The path of what the administrator may do with a table. */
const PERMISSIONS_PATH = "/api/v1/principals/admin/permissions";

/** The table `fact_sales` as its page reads it. */
const TABLE_DETAIL = {
  name: "fact_sales",
  namespace: "gold.sales",
  uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
  kind: "TABLE",
  metadata_location: "s3://warehouse/gold/sales/fact_sales/metadata/00001.metadata.json",
  schema: ["sale_id", "store_id", "sold_at", "amount"].map((name, index) => ({
    name,
    type: index === 2 ? "timestamp" : "long",
    nullable: index !== 0,
    description: null,
    description_source: null,
    label: null,
    label_source: null,
    label_language: null,
    description_language: null,
    terms: [],
  })),
  sql: null,
  references: null,
  hidden_references: 0,
  snapshot_id: 4821,
  state: "published",
  state_message: null,
  description: "Every sale in every store, one row per sale.",
  description_source: "project",
  last_write_at: "2026-11-10T06:00:00Z",
  row_count: 1204000,
  size_bytes: 98304,
  file_count: 3,
  expires_at: null,
  expires_soon: false,
  model: null,
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
  last_read_at: "2026-11-10T07:50:00Z",
  readers_7d: 2,
  health_summary: { state: "failing", item: "check_failed", text: "Check failed", others: 2 },
};

/** What the administrator may do with `fact_sales`: everything the page asks about. */
const PERMISSIONS = {
  principal: "admin",
  active: true,
  scope: { kind: "TABLE", environment: "prod", namespace: "gold.sales", table: "fact_sales" },
  permissions: ["READ_TABLE", "WRITE_TABLE", "DROP_TABLE", "SUBMIT_QUERY"].map((action) => ({
    action,
    allowed: true,
    via: [],
    reason: null,
  })),
  note: null,
};

/** The administrator's profile picture: one pixel, a PNG. */
const PICTURE =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

/** The paths the stubs answer; any other request to the API is a test failure. */
export const STUBBED = [
  "/api/v1/sign-in/session",
  "/api/v1/sign-in/notices",
  "/api/v1/whoami",
  "/api/v1/principals",
  "/api/v1/principals/admin/picture",
  "/api/v1/settings/sign-in",
  ...Object.keys(HOME),
  ...Object.keys(CATALOG),
  TABLES,
  TABLE_PATH,
  PERMISSIONS_PATH,
];

/**
 * Answers the session routes as for a signed-in administrator without notices, Home (with a
 * worker down, an update note and rows in every box), the Catalog (a tree and tables in every
 * health, and the page of `prod/gold.sales/fact_sales`), the administrator's profile picture, and the first screens of Access (the people) and
 * Settings (the session lengths).
 *
 * @param page - The page.
 */
export async function signInAsAdmin(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/session", (route) => route.fulfill({ json: ADMIN_SESSION }));
  await page.route("**/api/v1/sign-in/notices", (route) => route.fulfill({ json: [] }));
  await page.route(
    (url) => url.pathname === "/api/v1/principals",
    (route) =>
      route.fulfill({ json: [{ name: "admin", type: "USER", is_active: true, picture: "e1" }] }),
  );
  await page.route("**/api/v1/principals/admin/picture", (route) =>
    route.fulfill({ contentType: "image/png", body: Buffer.from(PICTURE, "base64") }),
  );
  await page.route("**/api/v1/settings/sign-in", (route) =>
    route.fulfill({ json: SIGN_IN_SETTINGS }),
  );
  await page.route("**/api/v1/whoami", (route) =>
    route.fulfill({
      json: {
        name: "admin",
        type: "USER",
        task_token: null,
        ci_token: null,
        language: null,
        admin: true,
        has_access: true,
        may_deploy: true,
        picture: "e1",
      },
    }),
  );
  for (const [path, json] of Object.entries({ ...HOME, ...CATALOG })) {
    await page.route(
      (url) => url.pathname === path,
      (route) => route.fulfill({ json }),
    );
  }
  await page.route(
    (url) => url.pathname === TABLE_PATH,
    (route) => route.fulfill({ json: TABLE_DETAIL }),
  );
  await page.route(
    (url) => url.pathname === PERMISSIONS_PATH,
    (route) => route.fulfill({ json: PERMISSIONS }),
  );
  await page.route(
    (url) => url.pathname === TABLES,
    (route) => route.fulfill(tablesAnswer(new URL(route.request().url()))),
  );
}

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

// React Aria's live announcer (a pending button speaks through it) keeps its message for 7 s and
// names it by the ids of the button's parts, which may be gone by then; it is the library's
// transient node, not the page.
const LIVE_ANNOUNCER = "[data-live-announcer]";

/**
 * Runs axe over the page with the WCAG 2.2 AA tags.
 *
 * @param page - The page to check.
 * @returns The violations' ids and targets.
 */
export async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(WCAG).exclude(LIVE_ANNOUNCER).analyze();
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
}

/**
 * Collects the console errors and page errors of a page.
 *
 * @param page - The page to listen to.
 * @returns The list, filled as errors arrive.
 */
export function consoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}
