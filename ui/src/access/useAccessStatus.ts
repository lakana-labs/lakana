/**
 * Reads, once, which scopes access files manage.
 *
 * @packageDocumentation
 */
import { type AccessStatusOut, getAccessStatus } from "../api/accessFile";
import { type Resource, useResource } from "../shell/useResource";

/**
 * Reads the access files' status for a place that writes access: until it is read, the place
 * offers no write, and a failed read offers none either.
 *
 * @returns The status.
 */
export function useAccessStatus(): Resource<AccessStatusOut> {
  return useResource(getAccessStatus);
}
