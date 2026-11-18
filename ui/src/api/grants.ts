/**
 * Grants: the list an administrator filters, adding and removing one, and the grants a person
 * holds.
 *
 * @remarks
 * Creating and revoking a grant need a fresh passkey touch; each asks for it before the request
 * (`guarded`). Nothing here ever sends `override_managed`: the interface never overrides a
 * scope an access file manages.
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";
import type { CursorPage } from "./manage";
import type { GrantOut, ScopeOut } from "./table";

/** The five roles, in the server's order. */
export const ROLES = ["VIEWER", "ANALYST", "DATA_SCIENTIST", "DATA_ENGINEER", "ADMIN"] as const;

/** A role. */
export type Role = (typeof ROLES)[number];

/** Every action, in the server's order. */
export const ACTIONS = [
  "MANAGE_ENVIRONMENT",
  "READ_ENVIRONMENT",
  "CREATE_NAMESPACE",
  "READ_NAMESPACE",
  "DROP_NAMESPACE",
  "MANAGE_NAMESPACE",
  "CREATE_TABLE",
  "READ_TABLE",
  "WRITE_TABLE",
  "DROP_TABLE",
  "MAINTAIN_TABLE",
  "SUBMIT_QUERY",
  "MANAGE_PRINCIPAL",
  "MANAGE_GROUP",
  "MANAGE_TOKEN",
  "MINT_TASK_TOKEN",
  "MANAGE_GRANT",
  "MANAGE_POLICY",
  "MANAGE_SECRET",
  "MANAGE_CONNECTION",
  "READ_AUDIT_LOG",
  "READ_QUERY_TEXT",
  "DEPLOY",
  "MANAGE_REPOSITORY",
  "MANAGE_PACKAGE_INDEX",
  "RUN",
] as const;

/** An action. */
export type Action = (typeof ACTIONS)[number];

/** Who a grant is for: a person or service account, or a group, by name. */
export interface GrantWho {
  /** A principal or a group. */
  readonly kind: "principal" | "group";
  /** The name. */
  readonly name: string;
}

/** A grant that gives a role. */
interface WhatRole {
  /** A role. */
  readonly kind: "role";
  /** The role. */
  readonly role: Role;
}

/** A grant that gives one action. */
interface WhatAction {
  /** A single action. */
  readonly kind: "action";
  /** The action. */
  readonly action: Action;
}

/** What a grant gives: a role or one action. */
export type GrantWhat = WhatRole | WhatAction;

/** A grant on the whole instance. */
interface WhereInstance {
  /** The instance. */
  readonly kind: "INSTANCE";
}

/** A grant on an environment. */
interface WhereEnvironment {
  /** An environment. */
  readonly kind: "ENVIRONMENT";
  /** The environment. */
  readonly environment: string;
}

/** A grant on a namespace. */
interface WhereNamespace {
  /** A namespace. */
  readonly kind: "NAMESPACE";
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace. */
  readonly namespace: string;
}

/** A grant on a table. */
interface WhereTable {
  /** A table. */
  readonly kind: "TABLE";
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table. */
  readonly table: string;
}

/** What a grant is on. */
export type GrantWhere = WhereInstance | WhereEnvironment | WhereNamespace | WhereTable;

/** The filters of the grants list; each null for any. */
export interface GrantFilter {
  /** Only the grants made to this principal or group. */
  readonly who: GrantWho | null;
  /** Only the grants of this role, or those that give this action. */
  readonly what: GrantWhat | null;
  /** Only the grants on exactly this object. */
  readonly where: GrantWhere | null;
}

/** The filter that lists every grant. */
export const NO_FILTER: GrantFilter = { who: null, what: null, where: null };

/** How many grants a page of the list holds. */
export const GRANTS_PAGE = 50;

/**
 * Writes a filter as the server's query.
 *
 * @param filter - The filter.
 * @returns The query, with the page size.
 */
export function grantQuery(filter: GrantFilter): URLSearchParams {
  const query = new URLSearchParams();
  const { who, what, where } = filter;
  if (who !== null) {
    query.set(who.kind === "principal" ? "principal_name" : "group_name", who.name);
  }
  if (what !== null) {
    if (what.kind === "role") {
      query.set("role", what.role);
    } else {
      query.set("action", what.action);
    }
  }
  if (where !== null) {
    if (where.kind === "INSTANCE") {
      query.set("kind", "INSTANCE");
    } else {
      query.set("environment", where.environment);
      if (where.kind !== "ENVIRONMENT") {
        query.set("namespace", where.namespace);
        if (where.kind === "TABLE") {
          query.set("table", where.table);
        }
      }
    }
  }
  query.set("limit", String(GRANTS_PAGE));
  return query;
}

/**
 * Lists one page of grants, newest first.
 *
 * @param filter - The filter.
 * @param cursor - The cursor of the page; null for the first.
 * @returns The page and the cursor of the next one.
 * @throws {ApiError} for a refusal, such as 404 for an unknown principal or group.
 */
export async function listGrants(
  filter: GrantFilter,
  cursor: string | null,
): Promise<CursorPage<GrantOut>> {
  const query = grantQuery(filter);
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const page = await api.page(`/api/v1/grants?${query.toString()}`);
  return { items: page.body as readonly GrantOut[], nextCursor: page.nextCursor };
}

/** A new grant, as `POST /api/v1/grants` takes it; a field left out is none. */
export interface GrantCreateIn {
  /** The principal it is for, unless a group. */
  readonly principal?: string;
  /** The group it is for, unless a principal. */
  readonly group?: string;
  /** The role it gives, unless one action. */
  readonly role?: Role;
  /** The action it gives, unless a role. */
  readonly action?: Action;
  /** The level it is on. */
  readonly kind: ScopeOut["kind"];
  /** The environment, unless on the instance. */
  readonly environment?: string;
  /** The dotted namespace, on a namespace or a table. */
  readonly namespace?: string;
  /** The table, on a table. */
  readonly table?: string;
}

/**
 * Grants a role or an action, after the passkey touch.
 *
 * @param body - Who, what and where.
 * @returns The grant.
 * @throws {ApiError} for a refusal: 404 for an unknown name, 409 for a grant in force or a
 *   managed scope, 422 for an incomplete object.
 */
export function createGrant(body: GrantCreateIn): Promise<GrantOut> {
  return guarded<GrantOut>("POST", "/api/v1/grants", body);
}

/**
 * Revokes a grant, after the passkey touch.
 *
 * @param id - The grant's id.
 * @throws {ApiError} for a refusal, such as 409 for the last administrator's grant.
 */
export async function revokeGrant(id: string): Promise<void> {
  await guarded("DELETE", `/api/v1/grants/${encodeURIComponent(id)}`);
}

/** A grant a person holds, directly or through a group, as `GET …/grants` answers it. */
export interface HeldGrantOut {
  /** The grant's id. */
  readonly id: string;
  /** The role, or null for one action. */
  readonly role: Role | null;
  /** The action, or null for a role. */
  readonly action: Action | null;
  /** The level it is on. */
  readonly kind: ScopeOut["kind"];
  /** The environment, if any. */
  readonly environment: string | null;
  /** The dotted namespace, if any. */
  readonly namespace: string | null;
  /** The table, if any. */
  readonly table: string | null;
  /** When it was granted. */
  readonly granted_at: string;
  /** Why a single action has no effect, or null. */
  readonly note: string | null;
  /** The group it comes through, or null for a direct grant. */
  readonly group: string | null;
}

/**
 * Lists the grants a person holds, directly and through their groups. A person may ask about
 * themselves; about anyone else only an administrator may.
 *
 * @param name - The principal's name.
 * @returns The grants, newest first.
 * @throws {ApiError} for a refusal.
 */
export function listHeldGrants(name: string): Promise<readonly HeldGrantOut[]> {
  return api.request<readonly HeldGrantOut[]>(
    "GET",
    `/api/v1/principals/${encodeURIComponent(name)}/grants`,
  );
}
