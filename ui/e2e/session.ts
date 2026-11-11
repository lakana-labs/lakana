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

/** The paths the stubs answer; any other request to the API is a test failure. */
export const STUBBED = [
  "/api/v1/sign-in/session",
  "/api/v1/sign-in/notices",
  "/api/v1/whoami",
  "/api/v1/principals",
  "/api/v1/settings/sign-in",
  ...Object.keys(HOME),
];

/**
 * Answers the session routes as for a signed-in administrator without notices, Home (with a
 * worker down, an update note and rows in every box), and the first screens of Access (the
 * people) and Settings (the session lengths).
 *
 * @param page - The page.
 */
export async function signInAsAdmin(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/session", (route) => route.fulfill({ json: ADMIN_SESSION }));
  await page.route("**/api/v1/sign-in/notices", (route) => route.fulfill({ json: [] }));
  await page.route(
    (url) => url.pathname === "/api/v1/principals",
    (route) => route.fulfill({ json: [{ name: "admin", type: "USER", is_active: true }] }),
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
        picture: null,
      },
    }),
  );
  for (const [path, json] of Object.entries(HOME)) {
    await page.route(
      (url) => url.pathname === path,
      (route) => route.fulfill({ json }),
    );
  }
}
