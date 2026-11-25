/**
 * The reads of the name pickers: principals, and the tables of an environment, a page at a time
 * and cancelled when a newer text is typed. They live apart from the lists' calls so that the
 * screens without a picker load none of them.
 *
 * @packageDocumentation
 */
import type { PrincipalOut } from "./access";
import type { TableListPageOut } from "./catalog";
import { api } from "./client";

/** The first principals whose name matches, and whether the server has more. */
export interface PrincipalPage {
  /** The principals, newest first. */
  readonly people: readonly PrincipalOut[];
  /** Whether more match than this page holds (the server sent a next cursor). */
  readonly more: boolean;
}

/**
 * Suggests principals for a name picker: the first `limit` whose name contains the text, newest
 * first. It needs the right to list principals (`MANAGE_PRINCIPAL`), as the People list does.
 *
 * @param text - What the name must contain; blank for the newest.
 * @param limit - How many to ask for.
 * @param signal - Cancels the read once a newer text is typed.
 * @returns The principals and whether more match.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
export async function suggestPrincipals(
  text: string,
  limit: number,
  signal: AbortSignal,
): Promise<PrincipalPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (text !== "") {
    query.set("q", text);
  }
  const page = await api.page(`/api/v1/principals?${query.toString()}`, { signal });
  return { people: page.body as readonly PrincipalOut[], more: page.nextCursor !== null };
}

/**
 * Suggests groups for a name picker: the first `limit` whose name contains the text, newest
 * first. It needs the `ADMIN` role, as the Groups list does.
 *
 * @param text - What the name must contain; blank for the newest.
 * @param limit - How many to ask for.
 * @param signal - Cancels the read once a newer text is typed.
 * @returns The groups' names and whether more match.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
export async function suggestGroups(
  text: string,
  limit: number,
  signal: AbortSignal,
): Promise<{ readonly names: readonly string[]; readonly more: boolean }> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (text !== "") {
    query.set("q", text);
  }
  const page = await api.page(`/api/v1/groups?${query.toString()}`, { signal });
  return {
    names: (page.body as readonly { readonly name: string }[]).map((group) => group.name),
    more: page.nextCursor !== null,
  };
}

/**
 * Suggests tables for a name picker: the first `limit` of an environment whose own name contains
 * the text (`name`, never a namespace, a column or a description), by name, in a namespace and
 * the ones beneath it when one is given. Only tables the person may read are listed; one they
 * may not read is left out, never refused.
 *
 * @param environment - The environment.
 * @param text - What the table's name must contain; blank for the first by name.
 * @param limit - How many to ask for.
 * @param signal - Cancels the read once a newer text is typed.
 * @param namespace - Only this dotted namespace and the ones beneath it; null for any.
 * @returns The page.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
export function suggestTables(
  environment: string,
  text: string,
  limit: number,
  signal: AbortSignal,
  namespace: string | null = null,
): Promise<TableListPageOut> {
  const params = new URLSearchParams({ environment, sort: "name", limit: String(limit) });
  if (namespace !== null) {
    params.set("namespace", namespace);
  }
  if (text !== "") {
    params.set("name", text);
  }
  return api.request<TableListPageOut>("GET", `/api/v1/tables?${params.toString()}`, undefined, {
    signal,
  });
}
