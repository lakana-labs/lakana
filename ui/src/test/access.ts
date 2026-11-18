/**
 * What the server answers Access's places with, for tests: grants, groups, members, managed
 * scopes and permissions.
 *
 * @packageDocumentation
 */
import type { PrincipalOut } from "../api/access";
import type { ManagedScopeOut } from "../api/accessFile";
import type { HeldGrantOut } from "../api/grants";
import type { EnvironmentOut } from "../api/environments";
import type { GrantOut, PermissionsOut } from "../api/table";
import type { FakeRoute } from "./server";

/**
 * A grant a person holds.
 *
 * @param over - What differs from a direct ANALYST grant on `dev`.
 * @returns The grant.
 */
export function aHeldGrant(over: Partial<HeldGrantOut> = {}): HeldGrantOut {
  return {
    id: "h-1",
    role: "ANALYST",
    action: null,
    kind: "ENVIRONMENT",
    environment: "dev",
    namespace: null,
    table: null,
    granted_at: "2026-11-01T09:00:00Z",
    note: null,
    group: null,
    ...over,
  };
}

/**
 * A grant as the Grants list answers it.
 *
 * @param over - What differs from an ANALYST grant on `dev` to the group `analysts`.
 * @returns The grant.
 */
export function aGrant(over: Partial<GrantOut> = {}): GrantOut {
  return {
    id: "g-1",
    principal_id: null,
    group_id: "group-1",
    principal: null,
    group: "analysts",
    role: "ANALYST",
    action: null,
    kind: "ENVIRONMENT",
    environment: "dev",
    namespace: null,
    table: null,
    granted_by: "admin-id",
    granted_at: "2026-11-01T09:00:00Z",
    note: null,
    ...over,
  };
}

/**
 * A grant to a person.
 *
 * @param name - The person's name.
 * @param over - What else differs.
 * @returns The grant.
 */
export function aPersonGrant(name: string, over: Partial<GrantOut> = {}): GrantOut {
  return aGrant({
    principal_id: `${name}-id`,
    group_id: null,
    group: null,
    principal: { id: `${name}-id`, name, principal_type: "user" },
    ...over,
  });
}

/**
 * A member of a group.
 *
 * @param name - The name.
 * @param type - `USER` or `SERVICE`.
 * @returns The principal.
 */
export function aMember(name: string, type = "USER"): PrincipalOut {
  return { name, type, is_active: true, picture: null };
}

/**
 * A scope an access file manages.
 *
 * @param scope - `instance` or an environment's name.
 * @returns The scope.
 */
export function aManagedScope(scope: string): ManagedScopeOut {
  return {
    scope,
    file_name: "access.yaml",
    file_sha256: "ab12cd",
    source_commit: null,
    applied_by: "admin",
    applied_at: "2026-11-17T09:00:00Z",
  };
}

/**
 * The access files' status route.
 *
 * @param scopes - The managed scopes' names.
 * @returns The route.
 */
export function accessStatusRoute(scopes: readonly string[] = []): Record<string, FakeRoute> {
  return {
    "GET /api/v1/access/status": {
      status: 200,
      body: { scopes: scopes.map(aManagedScope), overrides: [] },
    },
  };
}

/**
 * An environment as the environments list answers it.
 *
 * @param name - Its name.
 * @returns The environment.
 */
export function anEnvironment(name: string): EnvironmentOut {
  return {
    name,
    storage_root: `file:///srv/lakana/${name}`,
    storage_secret_ref: null,
    is_protected: false,
    properties: {},
    retention_days: null,
    deleted_at: null,
    purge_after: null,
  };
}

/**
 * What the permissions route answers.
 *
 * @param principal - Who is asked about.
 * @param allowed - The actions allowed.
 * @param refused - The actions refused.
 * @returns The answer.
 */
export function permissionsOf(
  principal: string,
  allowed: readonly string[],
  refused: readonly string[] = [],
): PermissionsOut {
  return {
    principal,
    active: true,
    scope: { kind: "INSTANCE", environment: null, namespace: null, table: null },
    permissions: [
      ...allowed.map((action) => ({ action, allowed: true, via: [], reason: null })),
      ...refused.map((action) => ({ action, allowed: false, via: [], reason: "No grant." })),
    ],
    note: null,
  };
}

/**
 * A session whose passkey touch is fresh for ten more minutes.
 *
 * @returns The route's answer.
 */
export function freshSession(): FakeRoute {
  return {
    status: 200,
    body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
  };
}

/** The step-up routes, for a ceremony a test stubs. */
export const STEP_UP_ROUTES: Record<string, FakeRoute> = {
  "POST /api/v1/sign-in/step-up/options": {
    status: 200,
    body: { ceremony_id: "c-1", public_key: {} },
  },
  "POST /api/v1/sign-in/step-up": { status: 204 },
};

/** A session with no passkey touch: a guarded action asks for one first. */
export const STALE_SESSION: FakeRoute = { status: 200, body: { step_up_until: null } };
