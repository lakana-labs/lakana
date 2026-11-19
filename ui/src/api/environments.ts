/**
 * The environments a person may read, as the lists of every area choose among them; and the
 * reads and writes of Settings › Environments.
 *
 * @remarks
 * A storage option's value can be an inline credential: it is only ever in a request body,
 * never in an address, and no answer carries one.
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";

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

/** A storage kind the server accepts for a new environment. */
export type StorageScheme = "file" | "s3" | "abfs";

/** The body of `POST /api/v1/environments`. */
export interface EnvironmentCreate {
  /** The new environment's name. */
  readonly name: string;
  /** Where its data lives. */
  readonly storage: {
    /** The storage kind. */
    readonly scheme: StorageScheme;
    /** The storage root URI. */
    readonly root: string;
    /** The storage options; their values are written, never read back. */
    readonly options: Readonly<Record<string, string>>;
  };
  /** Whether deleting it and changing its storage are refused. */
  readonly is_protected: boolean;
}

/** The storage one kind of thing uses in an environment. */
interface UsagePartOut {
  /** How many there are. */
  readonly count: number;
  /** The sum of the sizes that are known. */
  readonly bytes: number;
  /** How many have no size yet. */
  readonly unmeasured: number;
}

/** How much storage an environment uses, as `GET …/{name}/usage` answers it. */
export interface EnvironmentUsageOut {
  /** The environment. */
  readonly environment: string;
  /** Its managed tables. */
  readonly tables: UsagePartOut;
  /** Its dropped tables whose files wait for the end of their undo period. */
  readonly dropped_tables: UsagePartOut;
  /** Its artifacts. */
  readonly artifacts: UsagePartOut;
  /** The code archives of its project versions. */
  readonly code_archives: UsagePartOut;
  /** The four sums together. */
  readonly total_bytes: number;
  /** False when a part has unmeasured entries: the true figure is higher. */
  readonly complete: boolean;
}

/** One table directory that no table owns. */
export interface OrphanedLocationOut {
  /** Its path relative to the environment's storage root. */
  readonly location: string;
  /** How many files it held. */
  readonly files: number;
  /** Their total size. */
  readonly bytes: number;
  /** When its newest file was last changed; null when the storage does not say. */
  readonly newest_file_at: string | null;
  /** Whether a file is newer than the age asked for: it is never deleted. */
  readonly recent: boolean;
  /** Whether the deletion removed it. */
  readonly deleted: boolean;
  /** The files the deletion removed from it. */
  readonly files_deleted: number;
  /** Their total size. */
  readonly bytes_deleted: number;
  /** Why the deletion left it; null in a listing and for a deleted directory. */
  readonly kept_because: string | null;
}

/** What a search for, or a deletion of, leftover directories found and did. */
export interface OrphanedLocationsOut {
  /** The environment looked at. */
  readonly environment: string;
  /** The age asked for. */
  readonly older_than_days: number;
  /** The directories. */
  readonly locations: readonly OrphanedLocationOut[];
  /** False when the server's time limit stopped the call; calling again continues. */
  readonly complete: boolean;
}

/** An environment's storage as `GET …/{name}/storage` answers it: keys, never values. */
export interface EnvironmentStorageOut {
  /** The storage root URI. */
  readonly root: string;
  /** The storage secret's name; null for none. */
  readonly secret_ref: string | null;
  /** The keys of the options that are set, sorted. */
  readonly option_keys: readonly string[];
}

/**
 * The path of one environment.
 *
 * @param name - Its name, encoded here.
 * @returns The path.
 */
const environmentApiPath = (name: string): string =>
  `/api/v1/environments/${encodeURIComponent(name)}`;

/**
 * Lists the environments the person may read, whole.
 *
 * @returns The environments, in the server's order.
 */
export async function listEnvironmentsFull(): Promise<readonly EnvironmentOut[]> {
  return api.request<readonly EnvironmentOut[]>("GET", "/api/v1/environments");
}

/**
 * Lists the deleted environments whose files are not deleted yet, oldest deletion first.
 *
 * @returns The deleted environments the person may read.
 */
export async function listDeletedEnvironments(): Promise<readonly EnvironmentOut[]> {
  return api.request<readonly EnvironmentOut[]>("GET", "/api/v1/environments?deleted=true");
}

/**
 * Reads one environment.
 *
 * @param name - Its name.
 * @returns The environment.
 */
export async function getEnvironment(name: string): Promise<EnvironmentOut> {
  return api.request<EnvironmentOut>("GET", environmentApiPath(name));
}

/**
 * Creates an environment, after the passkey touch when the last one is old.
 *
 * @param body - The environment; its options' values go only in this body.
 * @returns The new environment.
 */
export async function createEnvironment(body: EnvironmentCreate): Promise<EnvironmentOut> {
  return guarded<EnvironmentOut>("POST", "/api/v1/environments", body);
}

/**
 * Protects or unprotects an environment, after the passkey touch when the last one is old.
 *
 * @param name - Its name.
 * @param isProtected - The new value.
 * @returns The environment as changed.
 */
export async function setProtected(name: string, isProtected: boolean): Promise<EnvironmentOut> {
  return guarded<EnvironmentOut>("PATCH", environmentApiPath(name), { is_protected: isProtected });
}

/**
 * Changes an environment's storage secret or replaces all its options, after the passkey touch
 * when the last one is old.
 *
 * @param name - Its name.
 * @param change - Only the parts that change; options replace every option.
 * @param change.options - The new options; their values go only in this body.
 * @param change.secret_ref - The new storage secret's name.
 * @returns The environment as changed.
 */
export async function changeStorage(
  name: string,
  change: {
    readonly options?: Readonly<Record<string, string>>;
    readonly secret_ref?: string;
  },
): Promise<EnvironmentOut> {
  return guarded<EnvironmentOut>("PATCH", `${environmentApiPath(name)}/storage`, change);
}

/**
 * Sets the retention of an environment's tables, or turns it off; no touch.
 *
 * @param name - Its name.
 * @param days - 1 to 3,650 days, or null to keep tables.
 * @returns The environment as changed.
 */
export async function setEnvironmentRetention(
  name: string,
  days: number | null,
): Promise<EnvironmentOut> {
  return api.request<EnvironmentOut>("PUT", `${environmentApiPath(name)}/retention`, { days });
}

/**
 * Deletes an environment. With `purge` its files go at the next clean-up and it cannot be
 * restored, so the passkey touch comes first; without, it can be restored and asks none.
 *
 * @param name - Its name.
 * @param purge - Whether its files are deleted at the next clean-up.
 * @returns The deleted environment, with its `purge_after`.
 */
export async function deleteEnvironment(name: string, purge: boolean): Promise<EnvironmentOut> {
  return purge
    ? guarded<EnvironmentOut>("DELETE", `${environmentApiPath(name)}?purge=true`)
    : api.request<EnvironmentOut>("DELETE", environmentApiPath(name));
}

/**
 * Restores a deleted environment within its undo period; no touch.
 *
 * @param name - Its name.
 * @returns The restored environment.
 */
export async function restoreEnvironment(name: string): Promise<EnvironmentOut> {
  return api.request<EnvironmentOut>("POST", `${environmentApiPath(name)}/restore`);
}

/**
 * Removes a deleted environment whose storage Lakana cannot reach from the catalog, after the
 * passkey touch when the last one is old; no file is touched.
 *
 * @param name - Its name.
 * @returns When it is removed.
 */
export async function purgeEnvironmentRows(name: string): Promise<void> {
  await guarded("POST", `${environmentApiPath(name)}/purge-rows`);
}

/**
 * Reads how much storage an environment uses; a background read, since nobody asked for it.
 *
 * @param name - Its name.
 * @returns The usage.
 */
export async function getUsage(name: string): Promise<EnvironmentUsageOut> {
  return api.request<EnvironmentUsageOut>("GET", `${environmentApiPath(name)}/usage`, undefined, {
    background: true,
  });
}

/**
 * Reads an environment's storage: its root, its secret and its options' keys, never a value.
 *
 * @param name - Its name.
 * @returns The storage.
 */
export async function getStorage(name: string): Promise<EnvironmentStorageOut> {
  return api.request<EnvironmentStorageOut>("GET", `${environmentApiPath(name)}/storage`);
}

/**
 * Looks for table directories that no table owns; deletes nothing.
 *
 * @param name - The environment's name.
 * @param olderThanDays - A directory with a file newer than this is listed as recent.
 * @returns What was found within the server's time limit.
 */
export async function findOrphans(
  name: string,
  olderThanDays: number,
): Promise<OrphanedLocationsOut> {
  return api.request<OrphanedLocationsOut>(
    "GET",
    `${environmentApiPath(name)}/orphaned-locations?older_than_days=${String(olderThanDays)}`,
  );
}

/**
 * Deletes the table directories no table owns whose files are older than the days given. They
 * cannot be restored, so the passkey touch comes first when the last one is old.
 *
 * @param name - The environment's name.
 * @param olderThanDays - How old a directory's newest file must be.
 * @returns Each directory, deleted or with why it was kept.
 */
export async function deleteOrphans(
  name: string,
  olderThanDays: number,
): Promise<OrphanedLocationsOut> {
  return guarded<OrphanedLocationsOut>(
    "POST",
    `${environmentApiPath(name)}/orphaned-locations/delete`,
    { older_than_days: olderThanDays },
  );
}
