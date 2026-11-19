/**
 * What the server answers the Connections screen with, for tests: the connection `crm` of
 * `prod`, the readable environments, and routes that serve them.
 *
 * @packageDocumentation
 */
import type { ConnectionEnvironmentOut, ConnectionOut } from "../api/connections";

import type { FakeRoute } from "./server";

/** The route that lists the environments whose connections the reader may see. */
export const ENVIRONMENTS_ROUTE = "GET /api/v1/connections/environments";

/** The route that lists the environments the reader may read, each saying whether it is protected. */
export const READABLE_ROUTE = "GET /api/v1/environments";

/**
 * The answer of {@link READABLE_ROUTE} for some environments.
 *
 * @param names - The environments' names.
 * @param protectedNames - Those of them that are protected; none by default.
 * @returns The route.
 */
export function readableRoute(
  names: readonly string[],
  protectedNames: readonly string[] = [],
): FakeRoute {
  return {
    status: 200,
    body: names.map((name) => ({
      name,
      storage_root: `file:///srv/lakana/${name}`,
      storage_secret_ref: null,
      is_protected: protectedNames.includes(name),
      properties: {},
      retention_days: null,
      deleted_at: null,
      purge_after: null,
    })),
  };
}

/**
 * A connection, as the server answers a manager: PostgreSQL in `prod`, with a finished test, and
 * every action open.
 *
 * @param over - The fields that differ.
 * @returns The connection.
 */
export function aConnection(over: Partial<ConnectionOut> = {}): ConnectionOut {
  return {
    name: "crm",
    kind: "postgresql",
    address: { host: "db.example.org", port: 5432, database: "crm" },
    settings: {},
    secret: "crm-password",
    declared_by: [],
    created_by: "ana.nowak",
    created_at: "2026-11-01T08:00:00Z",
    updated_at: "2026-11-02T09:00:00Z",
    environment: "prod",
    last_test: {
      id: "5d0c1e7a-3b2f-4c8d-9e1a-6f7b8c9d0e1f",
      state: "done",
      outcome: "ok",
      message: null,
      created_at: "2026-11-02T09:30:00Z",
      finished_at: "2026-11-02T09:30:02Z",
      requested_by: "ana.nowak",
    },
    actions: ["test", "update", "delete", "name_secret"],
    ...over,
  };
}

/**
 * The routes of a list of connections: the list, with the environment filter applied, and the
 * environments, with those that are protected.
 *
 * @param rows - The connections.
 * @param environments - The readable environments.
 * @param protectedNames - The protected ones among them; none by default.
 * @returns The routes, for `fakeServer`.
 */
export function connectionRoutes(
  rows: readonly ConnectionOut[],
  environments: readonly ConnectionEnvironmentOut[],
  protectedNames: readonly string[] = [],
): Record<string, FakeRoute> {
  return {
    "GET /api/v1/connections": (request) => {
      const environment = new URL(request.path, "http://localhost").searchParams.get("environment");
      const items = rows.filter((row) => environment === null || row.environment === environment);
      return { status: 200, body: { items, next_cursor: null } };
    },
    [ENVIRONMENTS_ROUTE]: { status: 200, body: environments },
    [READABLE_ROUTE]: readableRoute(
      environments.map(({ name }) => name),
      protectedNames,
    ),
  };
}
