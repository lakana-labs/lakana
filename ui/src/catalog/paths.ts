/**
 * The addresses of the catalog's screens.
 *
 * @packageDocumentation
 */
import { viewSearch } from "./view";
import type { CatalogView } from "./view";

/**
 * The address of the list in a view.
 *
 * @param view - The view.
 * @returns The path with its query.
 */
export function catalogPath(view: CatalogView): string {
  return `/catalog${viewSearch(view)}`;
}

/**
 * The address of one table.
 *
 * @param environment - The environment's name.
 * @param namespace - The dotted namespace.
 * @param name - The table's name.
 * @returns The path, each part encoded.
 */
export function tablePath(environment: string, namespace: string, name: string): string {
  return `/catalog/${[environment, namespace, name].map(encodeURIComponent).join("/")}`;
}
