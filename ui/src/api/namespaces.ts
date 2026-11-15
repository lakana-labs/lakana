/**
 * The calls that create and delete namespaces, the same routes `lakana ns create` and
 * `lakana ns delete` use.
 *
 * @packageDocumentation
 */
import { api } from "./client";
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
  await api.request<undefined>("DELETE", withQuery(target, breakGlassQuery(breakGlassReason)));
}
