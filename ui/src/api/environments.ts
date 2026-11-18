/**
 * The environments a person may read, as the lists of every area choose among them.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** An environment as `GET /api/v1/environments` answers it. */
export interface EnvironmentOut {
  /** The environment's name. */
  readonly name: string;
  /** Where its tables are stored. */
  readonly storage_root: string;
  /** The secret that holds the storage's credentials; null for none. */
  readonly storage_secret_ref: string | null;
  /** Whether deleting it and changing its storage are refused. */
  readonly is_protected: boolean;
  /** Free-form metadata. */
  readonly properties: Readonly<Record<string, string>>;
  /** The retention of its tables that set none of their own, in days; null when off. */
  readonly retention_days: number | null;
  /** When it was deleted; null unless it is. */
  readonly deleted_at: string | null;
  /** Until when a deleted environment can be restored; null unless it is deleted. */
  readonly purge_after: string | null;
}

/** An environment as a picker offers it: its name and whether it is protected. */
export interface EnvironmentChoice {
  /** The environment's name. */
  readonly name: string;
  /** Whether it is protected, which a picker marks in the colour of danger and in words. */
  readonly protected: boolean;
}

/**
 * Lists the environments the person may read; a background read, never the person's activity.
 *
 * @returns Each environment's name and whether it is protected, in the server's order.
 * @throws {ApiError} for a refusal.
 */
export async function listEnvironments(): Promise<readonly EnvironmentChoice[]> {
  const environments = await api.request<readonly EnvironmentOut[]>(
    "GET",
    "/api/v1/environments",
    undefined,
    { background: true },
  );
  return environments.map((environment) => ({
    name: environment.name,
    protected: environment.is_protected,
  }));
}
