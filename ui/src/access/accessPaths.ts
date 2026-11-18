/**
 * The addresses of Access's places: a group's page and the Grants list with its filters.
 *
 * @packageDocumentation
 */
import {
  ACTIONS,
  type GrantFilter,
  type GrantWhat,
  type GrantWhere,
  type GrantWho,
  grantQuery,
  NO_FILTER,
  ROLES,
} from "../api/grants";

/** The address of Groups. */
export const GROUPS_PATH = "/settings/access/groups";

/** The address of Grants. */
const GRANTS_PATH = "/settings/access/grants";

/** The address's names of the server's parameters that it names otherwise. */
const ADDRESS_NAMES: Readonly<Record<string, string>> = {
  principal_name: "principal",
  group_name: "group",
};

/**
 * The address of a group's page.
 *
 * @param name - The group's name.
 * @returns The path.
 */
export function groupPath(name: string): string {
  return `${GROUPS_PATH}/${encodeURIComponent(name)}`;
}

/**
 * The Grants list's address with a filter.
 *
 * @param filter - The filter; a part left out is any.
 * @returns The path, with its query when a filter is set.
 */
export function grantsPath(filter: Partial<GrantFilter>): string {
  const query = new URLSearchParams();
  // The server's query, without the page size, and with the address's shorter names for who.
  for (const [key, value] of grantQuery({ ...NO_FILTER, ...filter })) {
    if (key !== "limit") query.set(ADDRESS_NAMES[key] ?? key, value);
  }
  const text = query.toString();
  return text === "" ? GRANTS_PATH : `${GRANTS_PATH}?${text}`;
}

/**
 * A query value, unless it is missing or empty.
 *
 * @param search - The address's query.
 * @param key - The parameter.
 * @returns The value, or null.
 */
function value(search: URLSearchParams, key: string): string | null {
  const found = search.get(key);
  return found === null || found === "" ? null : found;
}

/**
 * Who the address names: a principal or a group, never both.
 *
 * @param search - The address's query.
 * @returns Who, or null.
 */
function readWho(search: URLSearchParams): GrantWho | null {
  const principal = value(search, "principal");
  const group = value(search, "group");
  if (principal !== null && group === null) return { kind: "principal", name: principal };
  if (group !== null && principal === null) return { kind: "group", name: group };
  return null;
}

/**
 * What the address names: a known role or a known action, never both.
 *
 * @param search - The address's query.
 * @returns What, or null.
 */
function readWhat(search: URLSearchParams): GrantWhat | null {
  const role = value(search, "role");
  const action = value(search, "action");
  const knownRole = ROLES.find((candidate) => candidate === role);
  const knownAction = ACTIONS.find((candidate) => candidate === action);
  if (knownRole !== undefined && action === null) return { kind: "role", role: knownRole };
  if (knownAction !== undefined && role === null) {
    return { kind: "action", action: knownAction };
  }
  return null;
}

/**
 * Where the address names: the instance, or an environment with a namespace and a table in it.
 *
 * @param search - The address's query.
 * @returns Where, or null.
 */
function readWhere(search: URLSearchParams): GrantWhere | null {
  if (value(search, "kind") === "INSTANCE") return { kind: "INSTANCE" };
  const environment = value(search, "environment");
  if (environment === null) return null;
  const namespace = value(search, "namespace");
  if (namespace === null) return { kind: "ENVIRONMENT", environment };
  const table = value(search, "table");
  if (table === null) return { kind: "NAMESPACE", environment, namespace };
  return { kind: "TABLE", environment, namespace, table };
}

/**
 * Reads the Grants list's filter from its address; what it cannot read is any.
 *
 * @param search - The address's query.
 * @returns The filter.
 */
export function readGrantFilter(search: URLSearchParams): GrantFilter {
  return { ...NO_FILTER, who: readWho(search), what: readWhat(search), where: readWhere(search) };
}

/**
 * A key that is the same for the same filter, however it was built: the list is keyed by it.
 *
 * @param filter - The filter.
 * @returns The key.
 */
export function filterKey(filter: GrantFilter): string {
  return grantQuery(filter).toString();
}

/** The address of Tokens. */
const TOKENS_PATH = "/settings/access/tokens";

/**
 * The Tokens page's address with its filters.
 *
 * @param filter - The filters; a part left out is any.
 * @param filter.owner - The owner of the personal tokens.
 * @param filter.environment - The environment of the task tokens.
 * @returns The path, with its query when a filter is set.
 */
export function tokensPath(filter: {
  readonly owner?: string;
  readonly environment?: string;
}): string {
  const query = new URLSearchParams();
  if (filter.owner !== undefined && filter.owner !== "") query.set("owner", filter.owner);
  if (filter.environment !== undefined && filter.environment !== "") {
    query.set("environment", filter.environment);
  }
  const text = query.toString();
  return text === "" ? TOKENS_PATH : `${TOKENS_PATH}?${text}`;
}
