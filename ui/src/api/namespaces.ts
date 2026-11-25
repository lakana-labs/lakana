/**
 * The calls that create and delete namespaces and register an external table in one, the same
 * routes `lakana ns create`, `lakana ns delete` and `lakana table create-external` use.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";
import { breakGlassQuery, environmentPath, withQuery } from "./manage";

/** A namespace, as the server answers it. */
export interface NamespaceOut {
  /** The dotted path. */
  readonly path: string;
  /** Free-form metadata; set from the CLI. */
  readonly properties: Readonly<Record<string, string>>;
}

/**
 * The path of a namespace, or of an environment's namespaces.
 *
 * @param environment - The environment.
 * @param path - The dotted namespace; omitted for the collection.
 * @returns The path.
 */
function namespacesPath(environment: string, path?: string): string {
  const suffix = path === undefined ? "" : `/${encodeURIComponent(path)}`;
  return environmentPath(environment, `/namespaces${suffix}`);
}

/**
 * Creates a namespace with no properties.
 *
 * @param environment - The environment.
 * @param path - The dotted path, such as `gold.sales`.
 * @param breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns The namespace.
 * @throws {ApiError} for a refusal; 409 when it exists.
 */
export function createNamespace(
  environment: string,
  path: string,
  breakGlassReason: string | null,
): Promise<NamespaceOut> {
  return api.request<NamespaceOut>("POST", namespacesPath(environment), {
    path,
    properties: {},
    break_glass_reason: breakGlassReason,
  });
}

/**
 * Deletes an empty namespace. From the browser the server asks for a passkey touch first, which
 * `api.request` runs.
 *
 * @param environment - The environment.
 * @param path - The dotted path.
 * @param breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns Nothing.
 * @throws {ApiError} for a refusal; 409 while it holds anything.
 */
export async function deleteNamespace(
  environment: string,
  path: string,
  breakGlassReason: string | null,
): Promise<void> {
  const target = namespacesPath(environment, path);
  await guarded<undefined>("DELETE", withQuery(target, breakGlassQuery(breakGlassReason)));
}

/** The file formats an external table can read. */
export type ExternalFormat = "csv" | "json" | "parquet";

/** An external table, as the server answers its registration. */
export interface ExternalTableOut {
  /** The table's name. */
  readonly name: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The pattern of its files under `raw/`. */
  readonly location_pattern: string;
  /** The format of its files. */
  readonly format: ExternalFormat;
  /** The columns read from one matching file: each name and its type. */
  readonly schema: Readonly<Record<string, string>>;
}

/**
 * Registers a table over files already under `raw/`.
 *
 * @param environment - The environment.
 * @param namespace - The dotted namespace the table goes in.
 * @param o - The table.
 * @param o.name - The table's name, without its namespace.
 * @param o.locationPattern - A directory and a file pattern under `raw/`, such as
 *   `raw/orders/*.csv`.
 * @param o.format - The format of the files.
 * @param o.breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns The table and its columns.
 * @throws {ApiError} for a refusal; 422 for a location the server refuses, 404 when no file
 *   matches, 409 when the name is taken or a deployed project declares it.
 */
export function createExternalTable(
  environment: string,
  namespace: string,
  o: {
    name: string;
    locationPattern: string;
    format: ExternalFormat;
    breakGlassReason: string | null;
  },
): Promise<ExternalTableOut> {
  return api.request<ExternalTableOut>(
    "POST",
    `${namespacesPath(environment, namespace)}/tables/external`,
    {
      name: o.name,
      location_pattern: o.locationPattern,
      format: o.format,
      break_glass_reason: o.breakGlassReason,
    },
  );
}
