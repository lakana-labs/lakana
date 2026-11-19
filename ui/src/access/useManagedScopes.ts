/**
 * Reads, once, which scopes access files manage, as a policy manager may know them.
 *
 * @packageDocumentation
 */
import { listManagedScopes, type ManagedScopeBriefOut } from "../api/accessFile";
import { type Resource, useResource } from "../shell/useResource";

/**
 * Reads the managed scopes for Row filters & masks: until they are read the place offers no
 * write, and a failed read offers none either.
 *
 * @returns The scopes.
 */
export function useManagedScopes(): Resource<readonly ManagedScopeBriefOut[]> {
  return useResource(listManagedScopes);
}
