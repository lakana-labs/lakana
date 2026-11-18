/**
 * The access files' status: which scopes a file manages, and the changes made by hand since.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** A scope an access file manages, and its last apply. */
export interface ManagedScopeOut {
  /** `instance`, or an environment's name. */
  readonly scope: string;
  /** The name of the file that last applied it. */
  readonly file_name: string;
  /** That file's hash. */
  readonly file_sha256: string;
  /** The commit the file came from, when given. */
  readonly source_commit: string | null;
  /** Who applied it; null once that principal is deleted. */
  readonly applied_by: string | null;
  /** When it was applied. */
  readonly applied_at: string;
}

/** A change made by hand in a managed scope that no apply has taken into account yet. */
interface AccessOverrideOut {
  /** The managed scope. */
  readonly scope: string;
  /** What was changed. */
  readonly change: string;
  /** Why. */
  readonly reason: string;
  /** Who changed it; null once that principal is deleted. */
  readonly by: string | null;
  /** When. */
  readonly at: string;
}

/** What `GET /api/v1/access/status` answers. */
export interface AccessStatusOut {
  /** Every managed scope, sorted by name. */
  readonly scopes: readonly ManagedScopeOut[];
  /** Every open override, oldest first. */
  readonly overrides: readonly AccessOverrideOut[];
}

/**
 * Reads which scopes access files manage. Only an administrator may.
 *
 * @returns The scopes and the open overrides.
 * @throws {ApiError} for a refusal.
 */
export function getAccessStatus(): Promise<AccessStatusOut> {
  return api.request<AccessStatusOut>("GET", "/api/v1/access/status");
}
