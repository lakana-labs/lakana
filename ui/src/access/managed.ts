/**
 * Which scope an access file manages for a change, as the server decides it.
 *
 * @packageDocumentation
 */
import type { ManagedScopeOut } from "../api/accessFile";

/** The name of the scope that stands for the whole instance. */
export const INSTANCE_SCOPE = "instance";

/**
 * The managed scope a change falls in, mirroring `lakana_core.access.managed.guard_change`: a
 * change in an environment is managed when that environment is listed, else when the instance
 * is; a change of the whole instance (a group, a membership, a grant on the instance) only when
 * the instance is.
 *
 * @param scopes - The managed scopes, as `GET /api/v1/access/status` lists them.
 * @param environment - The environment the change is in; null for the whole instance.
 * @returns The scope that manages the change, or null when none does.
 */
export function managedScopeOf(
  scopes: readonly ManagedScopeOut[],
  environment: string | null,
): ManagedScopeOut | null {
  const own =
    environment === null ? undefined : scopes.find((scope) => scope.scope === environment);
  return own ?? scopes.find((scope) => scope.scope === INSTANCE_SCOPE) ?? null;
}
