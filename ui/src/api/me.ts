/**
 * What the interface keeps for the signed-in person on the server (the tour, Get started, the
 * update note, what they opened recently), and the version of the server.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** What the interface remembers for a person. */
export interface InterfaceStateOut {
  /** Whether the person finished or skipped the tour. */
  readonly tour_seen: boolean;
  /** Whether the person hid Get started on Home. */
  readonly get_started_hidden: boolean;
  /** The last release whose update note the person saw (`x.y.z`), or null. */
  readonly update_note_seen: string | null;
}

/** One object the person opened recently. */
export interface RecentOut {
  /** What it is. */
  readonly kind: "table" | "query" | "job" | "run";
  /** Its environment. */
  readonly environment: string;
  /** The object: a dotted table name, a query's or a run's id, or `repository/job`. */
  readonly ref: string;
  /** When the person last opened it. */
  readonly opened_at: string;
}

/** What `GET /api/v1/info` answers, as far as the interface reads it. */
export interface InfoOut {
  /** The version of the running server. */
  readonly version: string;
}

/**
 * The path of a person's own resource.
 *
 * @param name - The principal name.
 * @param what - The resource under the person.
 * @returns The path.
 */
function own(name: string, what: string): string {
  return `/api/v1/principals/${encodeURIComponent(name)}/${what}`;
}

/**
 * Reads what the interface remembers for the person.
 *
 * @param name - The person's own principal name.
 * @returns The state; the defaults for a person who never changed anything.
 */
export async function getInterfaceState(name: string): Promise<InterfaceStateOut> {
  return api.request<InterfaceStateOut>("GET", own(name, "interface-state"));
}

/**
 * Changes the fields sent and keeps the others. The server refuses the whole request (422) when
 * `update_note_seen` is not a release version (`x.y.z`), so send that field on its own.
 *
 * @param name - The person's own principal name.
 * @param change - The fields to change.
 * @returns The state now.
 */
export async function changeInterfaceState(
  name: string,
  change: Partial<InterfaceStateOut>,
): Promise<InterfaceStateOut> {
  return api.request<InterfaceStateOut>("PATCH", own(name, "interface-state"), change);
}

/**
 * Lists what the person opened recently.
 *
 * @param name - The person's own principal name.
 * @returns At most 20 objects, newest first.
 */
export async function listRecent(name: string): Promise<readonly RecentOut[]> {
  return api.request<readonly RecentOut[]>("GET", own(name, "recent"));
}

/**
 * Reads the version of the running server.
 *
 * @returns The version, such as `0.23.0`, or a development build's.
 */
export async function getInfo(): Promise<InfoOut> {
  const info = await api.request<InfoOut>("GET", "/api/v1/info");
  return { version: info.version };
}

/**
 * Records that the person opened an object, so it shows in Recently opened.
 *
 * @param person - The person's own principal name.
 * @param kind - What was opened.
 * @param environment - Its environment.
 * @param ref - The object: a dotted table name.
 * @returns Nothing.
 */
export async function recordOpened(
  person: string,
  kind: "table",
  environment: string,
  ref: string,
): Promise<void> {
  await api.request<undefined>("POST", own(person, "recent"), { kind, environment, ref });
}
