/**
 * Stubs for the end-to-end tests of Settings › Status and Settings › Repositories: a status with
 * its detail and the workers, the repositories `analytics` (SSH, keys in dev) and `dbt-models`
 * (HTTPS), an access test the git host refuses, and kim, who reads dev and manages nothing.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

const AT = "2026-11-18T08:00:00Z";

/**
 * A worker as the status's detail lists it.
 *
 * @param id - Its id.
 * @param host - Its host.
 * @param over - What differs from an alive, idle worker on the server's version.
 * @returns The worker.
 */
function worker(id: string, host: string, over: Record<string, unknown> = {}): object {
  return {
    id,
    host,
    version: "0.23.0",
    started_at: AT,
    heartbeat_at: AT,
    status: "alive",
    takes: "all",
    query_run_id: null,
    deploy_run_id: null,
    task_run_id: null,
    memory: { used_bytes: 1288490188, limit_bytes: 4294967296, measured_at: AT },
    replaced: false,
    ...over,
  };
}

/** The status an administrator sees: one worker lost, one on an older version. */
export const STATUS_DETAIL = {
  state: "degraded",
  causes: ["worker_lost"],
  workers_alive: 2,
  workers_total: 3,
  detail: {
    slots_busy: 1,
    slots_total: 4,
    memory_used_bytes: 2576980377,
    memory_limit_bytes: 8589934592,
    workers: [
      worker("w-1", "worker-1.internal", { query_run_id: "q-1" }),
      worker("w-2", "worker-2.internal", { status: "lost", memory: null }),
      worker("w-3", "worker-3.internal", { version: "0.22.0", takes: "pipeline" }),
    ],
  },
};

/** The same instance as a person who may not manage it sees it. */
export const STATUS_PLAIN = { ...STATUS_DETAIL, detail: null };

/** An SSH repository with the deploy key of dev and two trusted host keys. */
export const ANALYTICS = {
  id: "r-1",
  name: "analytics",
  kind: "ssh",
  url: "git@github.com:acme/analytics.git",
  credential_secret: "analytics-deploy-key",
  credential_username: null,
  ssh_known_hosts:
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n",
  default_ref: "main",
  created_by: null,
  created_at: AT,
  updated_at: AT,
  deploy_keys: [
    {
      environment: "dev",
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILdevQ0r8VHx2mPz7Ykq lakana analytics dev",
      fingerprint: "SHA256:dev7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe",
    },
  ],
  host_keys: [
    { type: "ssh-ed25519", fingerprint: "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" },
  ],
};

/** An HTTPS repository read with a token. */
const DBT = {
  ...ANALYTICS,
  id: "r-2",
  name: "dbt-models",
  kind: "https",
  url: `https://github.com/acme/${"dbt-models-".repeat(26)}x.git`,
  credential_secret: "gh-token",
  ssh_known_hosts: null,
  default_ref: null,
  deploy_keys: [],
  host_keys: [],
};

/** The replaced key of dev. */
const REPLACED = {
  secret: "analytics-deploy-key",
  keys: [{ ...ANALYTICS.deploy_keys[0], public_key: "ssh-ed25519 AAAANEW dev", made: true }],
  skipped: [],
  replaced: ["dev"],
  notes: ["The old key for dev stops working: replace it at your git host now."],
};

/** The access test, which the git host refused. */
const REFUSED = {
  id: "p-9",
  kind: "access",
  state: "done",
  outcome: "auth_refused",
  message:
    "The git host refused the key of dev: add it as a read-only deploy key of the repository, then test again.",
  host_keys: null,
  created_at: AT,
  finished_at: AT,
};

/** The paths {@link stubRepositories} answers, beside those of `session.ts`. */
export const REPOSITORIES_STUBBED = [
  "/api/v1/repositories",
  "/api/v1/repositories/analytics",
  "/api/v1/repositories/dbt-models",
  "/api/v1/repositories/analytics/deploy-key",
  "/api/v1/repository-probes",
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/**
 * Fulfils a route with JSON.
 *
 * @param route - The route.
 * @param json - The body.
 * @param status - The status.
 * @returns When fulfilled.
 */
function answer(route: Route, json: unknown, status = 200): Promise<void> {
  return route.fulfill({ status, json });
}

/**
 * Answers Status with the detail, the repositories, their pages, Delete (which empties the
 * list), Replace key and an access test the git host refuses, and the environments dev and
 * prod. Register it after `signInAsAdmin`.
 *
 * @param page - The page.
 */
export async function stubRepositories(page: Page): Promise<void> {
  let repositories: readonly object[] = [ANALYTICS, DBT];
  await page.route("**/api/v1/status", (route) => answer(route, STATUS_DETAIL));
  await page.route(
    (url) => url.pathname === "/api/v1/environments",
    (route) =>
      answer(
        route,
        ["dev", "prod"].map((name) => ({
          name,
          storage_root: `s3://lakana/${name}`,
          storage_secret_ref: null,
          is_protected: false,
          properties: {},
          retention_days: null,
          deleted_at: null,
          purge_after: null,
        })),
      ),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/repositories",
    (route) => answer(route, repositories),
  );
  await page.route("**/api/v1/repositories/analytics", (route) => {
    if (route.request().method() === "DELETE") {
      repositories = repositories.filter((repo) => repo !== ANALYTICS);
      return route.fulfill({ status: 204 });
    }
    return answer(route, ANALYTICS);
  });
  await page.route("**/api/v1/repositories/dbt-models", (route) => answer(route, DBT));
  await page.route("**/api/v1/repositories/analytics/deploy-key", (route) =>
    answer(route, REPLACED, 201),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/repository-probes",
    (route) => answer(route, REFUSED, 202),
  );
}

/**
 * Makes the person kim, who reads dev and manages nothing: no detail in the status. Register it
 * after {@link stubRepositories}.
 *
 * @param page - The page.
 */
export async function stubReader(page: Page): Promise<void> {
  await page.route("**/api/v1/status", (route) => answer(route, STATUS_PLAIN));
  await page.route("**/api/v1/whoami", (route) =>
    answer(route, {
      name: "admin",
      type: "USER",
      task_token: null,
      ci_token: null,
      language: null,
      admin: false,
      has_access: true,
      may_deploy: false,
      secret_environments: [],
      manage_policy: false,
      reads_environments: true,
      instance_actions: [],
      picture: "e1",
    }),
  );
}
