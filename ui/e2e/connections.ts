/**
 * Stubs for the end-to-end tests of the Connections screen: the environments `dev` (where the
 * administrator may add, test, change and delete) and `prod` (read only and protected), five connections (one
 * that is fine, one the source refused, one never tested, one tested before its last change and
 * one whose test is running and is done at its third read), a page, a create and an update
 * (each asking for a passkey touch first), a delete, a test and Test all. What a write changes
 * shows in the reads after it.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

import { stubStepUpCeremony } from "./manage";
import { signInAsAdmin, STUBBED, freshTouch } from "./session";

/** The name of the connection the Add flow creates in these stubs. */
export const CREATED = "events";

/** The connection that is fine, in the read only environment `prod`. */
export const WAREHOUSE = "warehouse";

/** The connection whose secret the source refused; two assets declare it. */
export const EXPORTS = "exports";

/** The connection that was never tested; no asset declares it, so it may be deleted. */
export const SCRATCH = "scratch";

/** The connection tested before its last change. */
export const LEGACY = "legacy";

/** The connection whose test is running and is done at its third read. */
export const NIGHTLY = "nightly";

/** The environment where the administrator manages connections. */
export const DEV = "dev";

/** The environment the administrator may only read. */
export const PROD = "prod";

/** The environments the administrator may read, and what they may do in each. */
const ENVIRONMENTS = [
  { name: DEV, actions: ["create", "test"] },
  { name: PROD, actions: [] },
];

/** What the administrator may do with a connection of `dev`. */
const MANAGER = ["test", "update", "delete"];

/** The stubbed connections' names by environment, the one the Add flow creates included. */
const NAMES: Readonly<Record<string, readonly string[]>> = {
  [DEV]: [NIGHTLY, EXPORTS, SCRATCH, LEGACY, CREATED],
  [PROD]: [WAREHOUSE],
};

/** The routes of the connections' lists. */
const LIST = "/api/v1/connections";

/** The route of the environments. */
const LIST_ENVIRONMENTS = "/api/v1/connections/environments";

/** The route of the environments the administrator may read, which says which are protected. */
const READABLE = "/api/v1/environments";

/**
 * The path of an environment's part.
 *
 * @param environment - The environment.
 * @param suffix - What follows its name.
 * @returns The path.
 */
function at(environment: string, suffix: string): string {
  return `/api/v1/environments/${environment}${suffix}`;
}

/** The paths these stubs answer, beside those of `session.ts`. */
export const CONNECTIONS_STUBBED = [
  ...STUBBED,
  LIST,
  LIST_ENVIRONMENTS,
  READABLE,
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
  ...Object.entries(NAMES).flatMap(([environment, names]) => [
    at(environment, "/connections"),
    at(environment, "/connection-tests"),
    ...names.flatMap((name) => [
      at(environment, `/connections/${name}`),
      at(environment, `/connections/${name}/tests`),
    ]),
  ]),
];

/** A connection as the stubs keep it. */
type Stored = Record<string, unknown> & {
  name: string;
  environment: string;
  last_test: Record<string, unknown> | null;
  declared_by: readonly unknown[];
};

/** A mutating request that reached the stubs: its method, path and parsed body. */
interface Sent {
  /** The method. */
  readonly method: string;
  /** The path. */
  readonly path: string;
  /** The parsed body; undefined without one. */
  readonly body: unknown;
}

/** What `stubConnections` hands back to the test. */
export interface ConnectionsStub {
  /** The mutating requests to the connections and the step-up pair, as `METHOD path`. */
  readonly sent: string[];
  /** The same requests with their bodies, for the ones that have one. */
  readonly requests: Sent[];
  /** The number of reads of one connection (the list's or its own) so far. */
  readonly reads: (environment: string, name: string) => number;
}

/**
 * A time some way back from now, in the form the server writes.
 *
 * @param ms - How far back.
 * @returns The time.
 */
function ago(ms: number): string {
  return new Date(Date.now() - ms).toISOString();
}

/** An hour in milliseconds. */
const HOUR = 3_600_000;

/** A day in milliseconds. */
const DAY = 24 * HOUR;

/**
 * A finished test as a manager reads it.
 *
 * @param outcome - What it found.
 * @param finished - When it was done.
 * @param message - What it found, in words.
 * @returns The test.
 */
function done(outcome: string, finished: string, message: string): Record<string, unknown> {
  return {
    state: "done",
    outcome,
    finished_at: finished,
    id: `7a1b2c3d-0000-4000-8000-${outcome.padEnd(12, "0").slice(0, 12)}`,
    created_at: finished,
    message,
    requested_by: "admin",
  };
}

/**
 * The connections the stubs start with.
 *
 * @returns The five connections, `dev`'s first.
 */
function startingConnections(): Stored[] {
  const base = { created_by: "admin", created_at: ago(30 * DAY), updated_at: ago(30 * DAY) };
  return [
    {
      ...base,
      name: EXPORTS,
      environment: DEV,
      kind: "s3",
      address: { bucket: "acme-exports", region: "eu-central-1", endpoint: "" },
      settings: { access_key_id: "AKIAEXAMPLE", prefix: "daily/" },
      secret: EXPORTS,
      declared_by: [
        { repository: "analytics", asset: "bronze.exports" },
        { repository: "analytics", asset: "silver.exports_clean" },
      ],
      last_test: done("credentials", ago(DAY), "The source refused the secret."),
      actions: MANAGER,
    },
    {
      ...base,
      name: SCRATCH,
      environment: DEV,
      kind: "directory",
      address: { root: "landing", path: "scratch" },
      settings: {},
      secret: null,
      declared_by: [],
      last_test: null,
      actions: MANAGER,
    },
    {
      ...base,
      name: LEGACY,
      environment: DEV,
      kind: "sqlite-file",
      address: { root: "legacy", path: "orders.sqlite" },
      settings: {},
      secret: null,
      declared_by: [],
      updated_at: ago(2 * DAY),
      last_test: done("ok", ago(3 * DAY), "Reached the file."),
      actions: MANAGER,
    },
    {
      ...base,
      name: NIGHTLY,
      environment: DEV,
      kind: "http",
      address: { base_url: "https://api.example.com/v2" },
      settings: { auth: "bearer" },
      secret: NIGHTLY,
      declared_by: [{ repository: "analytics", asset: "bronze.nightly" }],
      last_test: {
        state: "running",
        outcome: null,
        finished_at: null,
        id: "5e6f7a8b-0000-4000-8000-000000000001",
        created_at: ago(5_000),
        message: null,
        requested_by: "admin",
      },
      actions: MANAGER,
    },
    {
      ...base,
      name: WAREHOUSE,
      environment: PROD,
      kind: "postgresql",
      address: { host: "db.example.com", port: 5432, database: "sales" },
      settings: { user: "reader", tls: "verify-full" },
      secret: WAREHOUSE,
      declared_by: [{ repository: "analytics", asset: "gold.sales" }],
      // A reader's last test: the three fields everyone gets.
      last_test: { state: "done", outcome: "ok", finished_at: ago(2 * HOUR) },
      actions: [],
    },
  ];
}

/**
 * The id the stubs give the tests they queue.
 *
 * @param count - The number of tests queued so far.
 * @returns The id.
 */
function testId(count: number): string {
  return `9c8b7a6d-0000-4000-8000-${String(count).padStart(12, "0")}`;
}

/**
 * Signs in as an administrator and answers the Connections screen's routes like a server would:
 * the two environments, the lists and pages, a Test that queues (and is running at its second
 * read and done at its third), Test all, and a create and an update that ask for a passkey touch
 * once each. The connection `nightly` starts running. `exports` is declared by two assets, so
 * its delete is refused; the only one of `dev` that may be deleted is `scratch`.
 *
 * @param page - The page.
 * @returns What reached the stubs.
 */
export async function stubConnections(page: Page): Promise<ConnectionsStub> {
  await signInAsAdmin(page);
  await freshTouch(page);
  const sent: string[] = [];
  const requests: Sent[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() !== "GET" &&
      (path.includes("/connection") || path.startsWith("/api/v1/sign-in/step-up"))
    ) {
      sent.push(`${request.method()} ${path}`);
      requests.push({
        method: request.method(),
        path,
        body: request.postData() === null ? undefined : (request.postDataJSON() as unknown),
      });
    }
  });
  const stored = new Map<string, Stored>(
    startingConnections().map((one) => [`${one.environment}/${one.name}`, one]),
  );
  const readCount = new Map<string, number>();
  const touched = await stubStepUpCeremony(page);
  let touches = 0;
  let queuedTests = 0;

  // Whether this write has the touch it needs; asks once for each.
  const touch = (route: Route): boolean => {
    if (touches > 0 && touched()) {
      touches -= 1;
      return true;
    }
    touches = 1;
    void route.fulfill({
      status: 403,
      json: { type: "StepUpRequired", detail: "Confirm with your passkey." },
    });
    return false;
  };

  // What a read answers: an open test moves on, queued, then running, then done.
  const read = (one: Stored): Stored => {
    const key = `${one.environment}/${one.name}`;
    const reads = (readCount.get(key) ?? 0) + 1;
    readCount.set(key, reads);
    const last = one.last_test;
    if (last !== null && last["state"] !== "done") {
      if (reads >= 3) {
        one.last_test = {
          ...last,
          state: "done",
          outcome: "ok",
          finished_at: new Date().toISOString(),
          message: "Reached the source.",
        };
      } else if (reads >= 2) {
        one.last_test = { ...last, state: "running" };
      }
    }
    return one;
  };

  // Queues a test of a connection, as the server does when none is open.
  const queue = (one: Stored): Record<string, unknown> => {
    queuedTests += 1;
    const key = `${one.environment}/${one.name}`;
    readCount.set(key, 0);
    one.last_test = {
      state: "queued",
      outcome: null,
      finished_at: null,
      id: testId(queuedTests),
      created_at: new Date().toISOString(),
      message: null,
      requested_by: "admin",
    };
    return one.last_test;
  };

  // The connections of an environment, or of all, by name.
  const listed = (environment: string | null): Stored[] =>
    [...stored.values()]
      .filter((one) => environment === null || one.environment === environment)
      .sort((a, b) => a.name.localeCompare(b.name));

  await page.route(
    (url) => url.pathname === LIST_ENVIRONMENTS,
    (route) => route.fulfill({ json: ENVIRONMENTS }),
  );
  await page.route(
    (url) => url.pathname === READABLE,
    (route) =>
      route.fulfill({
        json: ENVIRONMENTS.map(({ name }) => ({
          name,
          storage_root: `file:///srv/lakana/${name}`,
          storage_secret_ref: null,
          is_protected: name === PROD,
          properties: {},
          retention_days: null,
          deleted_at: null,
          purge_after: null,
        })),
      }),
  );
  await page.route(
    (url) => url.pathname === LIST,
    (route) => {
      const environment = new URL(route.request().url()).searchParams.get("environment");
      return route.fulfill({
        json: { items: listed(environment).map(read), next_cursor: null },
      });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/[^/]+\/connection-tests$/u.test(url.pathname),
    (route) => {
      const [, environment = ""] =
        /^\/api\/v1\/environments\/([^/]+)\//u.exec(new URL(route.request().url()).pathname) ?? [];
      const items = listed(environment).map((one) => {
        const open = one.last_test !== null && one.last_test["state"] !== "done";
        const test = open && one.last_test !== null ? one.last_test : queue(one);
        return { connection: one.name, test_id: String(test["id"]), queued: !open };
      });
      return route.fulfill({ status: 202, json: { items, next_cursor: null } });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/environments\/[^/]+\/connections(\/|$)/u.test(url.pathname),
    async (route) => {
      const request = route.request();
      const [, environment = "", rest = ""] =
        /^\/api\/v1\/environments\/([^/]+)\/connections\/?(.*)$/u.exec(
          new URL(request.url()).pathname,
        ) ?? [];
      if (rest === "") {
        if (request.method() === "GET") {
          await route.fulfill({ json: { items: listed(environment), next_cursor: null } });
          return;
        }
        const body = request.postDataJSON() as Record<string, unknown>;
        if (!touch(route)) {
          return;
        }
        if (stored.has(`${environment}/${String(body["name"])}`)) {
          await route.fulfill({
            status: 409,
            json: {
              type: "ConnectionNameTaken",
              detail: `There is already a connection named ${String(body["name"])} in ${environment}.`,
            },
          });
          return;
        }
        const name = String(body["name"]);
        const created: Stored = {
          name,
          environment,
          kind: body["kind"],
          address: body["address"],
          settings: body["settings"],
          secret:
            typeof body["secret"] === "string"
              ? body["secret"]
              : body["secret_values"] === undefined
                ? null
                : name,
          declared_by: [],
          created_by: "admin",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          last_test: null,
          actions: MANAGER,
        };
        stored.set(`${environment}/${name}`, created);
        await route.fulfill({ status: 201, json: created });
        return;
      }
      const [name = "", part] = rest.split("/");
      const one = stored.get(`${environment}/${name}`);
      if (one === undefined) {
        await route.fulfill({ status: 404, json: { detail: "No such connection." } });
        return;
      }
      if (part === "tests") {
        const test =
          one.last_test !== null && one.last_test["state"] !== "done" ? null : queue(one);
        if (test === null) {
          await route.fulfill({
            status: 409,
            json: { type: "ConnectionTestOpen", detail: "A test of this connection is open." },
          });
          return;
        }
        await route.fulfill({ status: 202, json: test });
      } else if (request.method() === "PATCH") {
        if (!touch(route)) {
          return;
        }
        const body = request.postDataJSON() as Record<string, unknown>;
        const changed: Stored = {
          ...one,
          address: { ...(one["address"] as object), ...(body["address"] as object | undefined) },
          settings: { ...(one["settings"] as object), ...(body["settings"] as object | undefined) },
          updated_at: new Date().toISOString(),
        };
        stored.set(`${environment}/${name}`, changed);
        await route.fulfill({ json: changed });
      } else if (request.method() === "DELETE") {
        if (one.declared_by.length > 0) {
          await route.fulfill({
            status: 409,
            json: { type: "ConnectionInUse", detail: `${name} is still declared by an asset.` },
          });
          return;
        }
        stored.delete(`${environment}/${name}`);
        await route.fulfill({ status: 204 });
      } else {
        await route.fulfill({ json: read(one) });
      }
    },
  );
  return {
    sent,
    requests,
    reads: (environment, name) => readCount.get(`${environment}/${name}`) ?? 0,
  };
}
