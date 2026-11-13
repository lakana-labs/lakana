/**
 * The session routes, stubbed with `page.route`, so the interface runs against `vite preview`
 * without a server.
 *
 * @packageDocumentation
 */
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

/** What the Catalog reads: a tree of two environments and a page of tables in every health. */
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
        ],
      },
    ],
  },
  "/api/v1/tables": {
    items: [
      {
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
        size_bytes: 3100000,
        last_write_at: "2026-11-09T06:00:00Z",
        last_read_at: "2026-11-10T07:30:00Z",
        readers_7d: 4,
        health_summary: { state: "healthy", item: "healthy", text: "Healthy", others: 0 },
        match: null,
      },
      {
        environment: "prod",
        namespace: "gold.sales",
        name: "fact_sales",
        uuid: "9a41c0de-7b62-4f5d-8c3a-1e0f9b7d2a42",
        kind: "TABLE",
        model_kind: "fact",
        state: "published",
        owner: { name: "ana.nowak", kind: "USER" },
        created_at: "2026-10-02T08:00:00Z",
        row_count: 1204000,
        size_bytes: 98304,
        last_write_at: "2026-11-10T06:00:00Z",
        last_read_at: "2026-11-10T07:50:00Z",
        readers_7d: 2,
        health_summary: {
          state: "failing",
          item: "check_failed",
          text: "Check failed",
          others: 2,
        },
        match: null,
      },
      {
        environment: "dev",
        namespace: "gold",
        name: "dim_store",
        uuid: "c7e2b5f0-1d34-4e8a-a6b9-3f5d0c8e7a63",
        kind: "VIEW",
        model_kind: "dimension",
        state: "published",
        owner: { name: "loader", kind: "SERVICE_ACCOUNT" },
        created_at: "2026-10-03T08:00:00Z",
        row_count: null,
        size_bytes: null,
        last_write_at: null,
        last_read_at: null,
        readers_7d: 0,
        health_summary: { state: "not_judged", item: "not_judged", text: "Not judged", others: 0 },
        match: null,
      },
    ],
    next_cursor: null,
    capped: false,
  },
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
];

/**
 * Answers the session routes as for a signed-in administrator without notices, Home (with a
 * worker down, an update note and rows in every box), the Catalog (a tree and tables in every
 * health), the administrator's profile picture, and the first screens of Access (the people) and
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
}
