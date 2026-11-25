/**
 * The calls of the Connections screen: the connections a person may read, their environments,
 * creating, changing and deleting one, and testing one or all, the same routes
 * `lakana connection …` uses.
 *
 * @remarks
 * A secret value travels only in the body of {@link createConnection} and
 * {@link updateConnection}; it is never put in a path, a query or an error.
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";
import { environmentPath, withQuery, type CursorPage } from "./manage";

/** The kinds of connection the server knows. */
export type ConnectionKind =
  "postgresql" | "mariadb" | "sqlite-file" | "http" | "s3" | "gcs" | "azure" | "directory" | "tcp";

/** Where a connection test is. */
type ConnectionTestState = "queued" | "running" | "done";

/** What a finished test found. */
export type ConnectionTestOutcome =
  | "ok"
  | "dns"
  | "refused"
  | "timeout"
  | "tls"
  | "credentials"
  | "not_found"
  | "not_allowed"
  | "root_unavailable"
  | "failed";

/** What the caller's rights allow on one connection. */
type ConnectionAction = "test" | "update" | "delete" | "name_secret";

/** What the caller's rights allow on the connections of one environment. */
type EnvironmentConnectionAction = "create" | "test" | "name_secret";

/**
 * A connection's latest test. Everyone who reads the connection gets the first three fields;
 * the others are present only for those who manage connections, and absent (not null) otherwise.
 */
export interface ConnectionLastTestOut {
  /** Where the test is. */
  readonly state: ConnectionTestState;
  /** What it found once done; null before. */
  readonly outcome: ConnectionTestOutcome | null;
  /** When it was done; null before. */
  readonly finished_at: string | null;
  /** The test's id; managers only. */
  readonly id?: string;
  /** When it was requested; managers only. */
  readonly created_at?: string;
  /** What it found, redacted; null before it is done; managers only. */
  readonly message?: string | null;
  /** Who requested it; managers only. */
  readonly requested_by?: string | null;
}

/**
 * The newest run that failed on a connection: its code ended with an error of `lakana.ingest`
 * that names the connection. Only failures after the connection's last change count, and a
 * later run of the same asset hides it.
 */
export interface ConnectionRunFailureOut {
  /** The run's id. */
  readonly run_id: string;
  /** The task that failed. */
  readonly asset: string;
  /** When it failed. */
  readonly finished_at: string;
}

/** A connection, as the server answers it. It carries its secret's name, never a value. */
export interface ConnectionOut {
  /** The connection's name. */
  readonly name: string;
  /** Its environment. */
  readonly environment: string;
  /** Its kind. */
  readonly kind: ConnectionKind;
  /** Where it points. */
  readonly address: Readonly<Record<string, unknown>>;
  /** Its other settings. */
  readonly settings: Readonly<Record<string, unknown>>;
  /** The name of its secret; null when it has none. */
  readonly secret: string | null;
  /** The project assets that declare it. */
  readonly declared_by: readonly { repository: string; asset: string }[];
  /** Who created it. */
  readonly created_by: string;
  /** When it was created. */
  readonly created_at: string;
  /** When it was last changed. */
  readonly updated_at: string;
  /** Its latest test; null when it was never tested. */
  readonly last_test: ConnectionLastTestOut | null;
  /** What the caller's rights allow on it. */
  readonly actions: readonly ConnectionAction[];
  /**
   * The newest run that failed on it, for a reader of the environment's runs; null when there
   * is none, absent from a server older than the field.
   */
  readonly last_run_failure?: ConnectionRunFailureOut | null;
}

/** An environment whose connections the caller may read, and what they may do there. */
export interface ConnectionEnvironmentOut {
  /** The environment's name. */
  readonly name: string;
  /** What the caller's rights allow there. */
  readonly actions: readonly EnvironmentConnectionAction[];
}

/** The answer to requesting one test. */
export interface ConnectionTestOut {
  /** The test's id. */
  readonly id: string;
  /** Where the test is. */
  readonly state: ConnectionTestState;
  /** What it found once done; null before. */
  readonly outcome: ConnectionTestOutcome | null;
  /** What it found, redacted; null before it is done. */
  readonly message: string | null;
  /** When it was requested. */
  readonly created_at: string;
  /** When it was done; null before. */
  readonly finished_at: string | null;
}

/** What Test all did for one connection. */
export interface ConnectionQueuedTestOut {
  /** The connection's name. */
  readonly connection: string;
  /** The test's id. */
  readonly test_id: string;
  /** True when this request queued the test; false when an open recent one was returned. */
  readonly queued: boolean;
}

/** The body that creates a connection. */
export interface ConnectionCreateIn {
  /** The new connection's name. */
  readonly name: string;
  /** Its kind. */
  readonly kind: ConnectionKind;
  /** Where it points. */
  readonly address: Readonly<Record<string, unknown>>;
  /** Its other settings. */
  readonly settings: Readonly<Record<string, unknown>>;
  /** The name of an existing secret to bind. */
  readonly secret?: string;
  /** New values for the connection's own secret. */
  readonly secret_values?: Readonly<Record<string, string>>;
}

/** The body that changes a connection; what it leaves out stays. */
export interface ConnectionUpdateIn {
  /** The new address. */
  readonly address?: Readonly<Record<string, unknown>>;
  /** Settings to set; a null value removes the setting. */
  readonly settings?: Readonly<Record<string, unknown>>;
  /** The name of another secret to bind. */
  readonly secret?: string;
  /** New values for the connection's own secret. */
  readonly secret_values?: Readonly<Record<string, string>>;
  /** Settings to clear. */
  readonly clear?: readonly string[];
}

/** How a read is sent. */
export interface ReadOptions {
  /** Whether the request is a background poll, which never asks for a passkey touch. */
  readonly background?: boolean;
}

/** One page of the server's answer, with its cursor under the server's name. */
interface WirePage<T> {
  readonly items: readonly T[];
  readonly next_cursor: string | null;
}

/** How many connections Test all asks for per page, the server's largest. */
const TEST_ALL_PAGE = 200;

/**
 * The path of a connection, or of one of its parts; the environment and the name are encoded.
 *
 * @param environment - The environment.
 * @param name - The connection, or null for the collection.
 * @param suffix - What follows it, such as `/tests`.
 * @returns The path.
 */
function connectionPath(environment: string, name: string | null, suffix = ""): string {
  const part = name === null ? "" : `/${encodeURIComponent(name)}`;
  return environmentPath(environment, `/connections${part}${suffix}`);
}

/**
 * Lists one page of connections, of one environment or of every one the caller may read.
 *
 * @param environment - The environment, or null for all.
 * @param cursor - The cursor of the page, or null for the first.
 * @param limit - The page's size.
 * @param options - `background` for a poll, which never asks for a passkey touch.
 * @returns The page.
 * @throws {ApiError} for a refusal.
 */
export async function listConnections(
  environment: string | null,
  cursor: string | null,
  limit = 100,
  options: ReadOptions = {},
): Promise<CursorPage<ConnectionOut>> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (environment !== null) {
    query.set("environment", environment);
  }
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const body = await api.request<WirePage<ConnectionOut>>(
    "GET",
    withQuery("/api/v1/connections", query),
    undefined,
    options,
  );
  return { items: body.items, nextCursor: body.next_cursor };
}

/**
 * Lists the environments whose connections the caller may read, and what they may do in each.
 *
 * @returns The environments.
 * @throws {ApiError} for a refusal.
 */
export function listConnectionEnvironments(): Promise<readonly ConnectionEnvironmentOut[]> {
  return api.request<readonly ConnectionEnvironmentOut[]>(
    "GET",
    "/api/v1/connections/environments",
  );
}

/**
 * Reads one connection.
 *
 * @param environment - The environment.
 * @param name - The connection.
 * @param options - `background` for a poll.
 * @returns The connection.
 * @throws {ApiError} for a refusal; 404 for one that does not exist.
 */
export function getConnection(
  environment: string,
  name: string,
  options: ReadOptions = {},
): Promise<ConnectionOut> {
  return api.request<ConnectionOut>("GET", connectionPath(environment, name), undefined, options);
}

/**
 * Creates a connection. From the browser the server asks for a passkey touch first, which
 * `api.request` runs.
 *
 * @param environment - The environment.
 * @param body - The connection, with its secret's name or values.
 * @returns The connection.
 * @throws {ApiError} for a refusal; 409 for a name already taken.
 */
export function createConnection(
  environment: string,
  body: ConnectionCreateIn,
): Promise<ConnectionOut> {
  return guarded<ConnectionOut>("POST", connectionPath(environment, null), body);
}

/**
 * Changes a connection. From the browser the server asks for a passkey touch first.
 *
 * @param environment - The environment.
 * @param name - The connection.
 * @param body - What changes.
 * @returns The connection.
 * @throws {ApiError} for a refusal.
 */
export function updateConnection(
  environment: string,
  name: string,
  body: ConnectionUpdateIn,
): Promise<ConnectionOut> {
  return guarded<ConnectionOut>("PATCH", connectionPath(environment, name), body);
}

/**
 * Deletes a connection.
 *
 * @param environment - The environment.
 * @param name - The connection.
 * @returns Nothing.
 * @throws {ApiError} for a refusal; 409 for one that is still declared by a project.
 */
export async function deleteConnection(environment: string, name: string): Promise<void> {
  await api.request<undefined>("DELETE", connectionPath(environment, name));
}

/**
 * Queues a test of one connection; a worker runs it.
 *
 * @param environment - The environment.
 * @param name - The connection.
 * @returns The queued test.
 * @throws {ApiError} for a refusal.
 */
export function requestConnectionTest(
  environment: string,
  name: string,
): Promise<ConnectionTestOut> {
  return api.request<ConnectionTestOut>("POST", connectionPath(environment, name, "/tests"));
}

/**
 * Queues a test of every connection of an environment, walking every page by `next_cursor`.
 *
 * @param environment - The environment.
 * @returns What was queued for each connection.
 * @throws {ApiError} for a refusal.
 */
export async function testAllConnections(
  environment: string,
): Promise<readonly ConnectionQueuedTestOut[]> {
  const all: ConnectionQueuedTestOut[] = [];
  let cursor: string | null = null;
  do {
    const query = new URLSearchParams({ limit: String(TEST_ALL_PAGE) });
    if (cursor !== null) {
      query.set("cursor", cursor);
    }
    const body: WirePage<ConnectionQueuedTestOut> = await api.request(
      "POST",
      withQuery(environmentPath(environment, "/connection-tests"), query),
    );
    all.push(...body.items);
    cursor = body.next_cursor;
  } while (cursor !== null);
  return all;
}
