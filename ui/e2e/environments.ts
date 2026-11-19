/**
 * Stubs for the end-to-end tests of Settings › Environments: the environments dev and prod
 * (protected) with the deleted dev-old and hdfs-old, which change as the tests create, protect,
 * delete and restore; usage, the storage's option keys, leftover directories; and mara, who
 * manages dev and dev-old and nothing on the instance.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

const AT = "2026-11-18T08:00:00Z";

/** The environments as the stubs keep them, by name. */
interface Environment {
  readonly name: string;
  readonly storage_root: string;
  readonly storage_secret_ref: string | null;
  readonly is_protected: boolean;
  readonly properties: Readonly<Record<string, string>>;
  readonly retention_days: number | null;
  readonly deleted_at: string | null;
  readonly purge_after: string | null;
}

/**
 * An environment.
 *
 * @param name - Its name.
 * @param over - What differs from a live one on S3 that keeps its tables.
 * @returns The environment.
 */
function environment(name: string, over: Partial<Environment> = {}): Environment {
  return {
    name,
    storage_root: `s3://corp-lake/${name}`,
    storage_secret_ref: "corp-s3",
    is_protected: false,
    properties: { "cost-centre": "data-platform" },
    retention_days: null,
    deleted_at: null,
    purge_after: null,
    ...over,
  };
}

/** How much an environment uses: some sizes not measured yet. */
const USAGE = {
  tables: { count: 12, bytes: 1_200_000_000, unmeasured: 1 },
  dropped_tables: { count: 2, bytes: 40_000_000, unmeasured: 0 },
  artifacts: { count: 3, bytes: 8_000_000, unmeasured: 0 },
  code_archives: { count: 4, bytes: 200_000, unmeasured: 0 },
  total_bytes: 1_248_200_000,
  complete: false,
};

/** The leftover directories of an environment: two old, one recent. */
const LEFTOVER = [
  {
    location: "managed/gold/station_ranking-7f3a",
    files: 14,
    bytes: 52_000_000,
    newest_file_at: "2026-08-02T10:00:00Z",
    recent: false,
    deleted: false,
    files_deleted: 0,
    bytes_deleted: 0,
    kept_because: null,
  },
  {
    location: "managed/silver/readings-2b9c",
    files: 3,
    bytes: 900_000,
    newest_file_at: "2026-11-17T22:00:00Z",
    recent: true,
    deleted: false,
    files_deleted: 0,
    bytes_deleted: 0,
    kept_because: null,
  },
];

/** The environments and parts of an environment the stubs answer. */
const NAMES = ["dev", "prod", "staging", "dev-old", "hdfs-old"];
const PARTS = [
  "",
  "/usage",
  "/storage",
  "/retention",
  "/restore",
  "/purge-rows",
  "/secrets",
  "/orphaned-locations",
  "/orphaned-locations/delete",
];

/** The paths {@link stubEnvironments} answers, beside those of `session.ts`. */
export const ENVIRONMENTS_STUBBED = [
  "/api/v1/environments",
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
  ...NAMES.flatMap((name) => PARTS.map((part) => `/api/v1/environments/${name}${part}`)),
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
 * Answers the environments: the lists (live, and deleted for `?deleted=true`), each one's page,
 * create, protect, retention, delete (with or without purge), restore, purge rows, usage, the
 * storage's option keys, an empty list of secrets and the leftover directories. Register it after
 * `signInAsAdmin`.
 *
 * @param page - The page.
 */
export async function stubEnvironments(page: Page): Promise<void> {
  const live = new Map<string, Environment>([
    [
      "dev",
      environment("dev", { storage_root: "file:///srv/lakana/dev", storage_secret_ref: null }),
    ],
    ["prod", environment("prod", { is_protected: true, retention_days: 90 })],
  ]);
  const deleted = new Map<string, Environment>([
    ["dev-old", environment("dev-old", { deleted_at: AT, purge_after: "2026-11-25T08:00:00Z" })],
    [
      "hdfs-old",
      environment("hdfs-old", {
        storage_root: "hdfs://old-cluster/lake",
        deleted_at: AT,
        purge_after: "2026-11-25T08:00:00Z",
      }),
    ],
  ]);
  await page.route(
    (url) => url.pathname === "/api/v1/environments",
    async (route) => {
      const request = route.request();
      if (request.method() === "POST") {
        const body = request.postDataJSON() as {
          name: string;
          storage: { root: string };
          is_protected: boolean;
        };
        const made = environment(body.name, {
          storage_root: body.storage.root,
          storage_secret_ref: null,
          is_protected: body.is_protected,
          properties: {},
        });
        live.set(made.name, made);
        return answer(route, made, 201);
      }
      const url = new URL(request.url());
      return answer(
        route,
        url.searchParams.get("deleted") === "true" ? [...deleted.values()] : [...live.values()],
      );
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/[^/]+(\/.*)?$/.test(url.pathname),
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const [, name = "", part = ""] =
        /^\/api\/v1\/environments\/([^/]+)(?:\/(.*))?$/.exec(url.pathname) ?? [];
      const found = live.get(decodeURIComponent(name));
      const method = request.method();
      if (part === "restore") {
        const was = deleted.get(name) as Environment;
        deleted.delete(name);
        const back = { ...was, deleted_at: null, purge_after: null };
        live.set(name, back);
        return answer(route, back);
      }
      if (part === "purge-rows") {
        deleted.delete(name);
        return route.fulfill({ status: 204 });
      }
      if (part === "") {
        if (method === "DELETE") {
          const purge = url.searchParams.get("purge") === "true";
          const was = (found ?? deleted.get(name)) as Environment;
          live.delete(name);
          const gone = {
            ...was,
            deleted_at: AT,
            purge_after: purge ? AT : "2026-11-25T08:00:00Z",
          };
          deleted.set(name, gone);
          return answer(route, gone);
        }
        if (found === undefined) {
          return answer(route, { detail: `No environment named '${name}'.` }, 404);
        }
        if (method === "PATCH") {
          const next = {
            ...found,
            is_protected: (request.postDataJSON() as { is_protected: boolean }).is_protected,
          };
          live.set(name, next);
          return answer(route, next);
        }
        return answer(route, found);
      }
      const known = found as Environment;
      if (part === "retention") {
        const next = {
          ...known,
          retention_days: (request.postDataJSON() as { days: number | null }).days,
        };
        live.set(name, next);
        return answer(route, next);
      }
      if (part === "usage") return answer(route, { environment: name, ...USAGE });
      if (part === "storage" && method === "GET") {
        return answer(route, {
          root: known.storage_root,
          secret_ref: known.storage_secret_ref,
          option_keys: ["endpoint", "region"],
        });
      }
      if (part === "storage") return answer(route, known);
      if (part === "secrets") return answer(route, []);
      if (part.startsWith("orphaned-locations")) {
        return answer(route, {
          environment: name,
          older_than_days: 30,
          locations: LEFTOVER,
          complete: true,
        });
      }
      // The Catalog's reads of an environment stay with the stubs of `session.ts`.
      return route.fallback();
    },
  );
}

/**
 * Makes the person mara, who reads every environment and manages dev and dev-old, and nothing on
 * the instance. Register it after {@link stubEnvironments}.
 *
 * @param page - The page.
 */
export async function stubMara(page: Page): Promise<void> {
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
      managed_environments: ["dev", "dev-old"],
      picture: "e1",
    }),
  );
}
