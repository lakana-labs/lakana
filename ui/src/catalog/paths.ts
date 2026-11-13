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

/** The tabs of a table's page. */
export type TableTab = "overview" | "columns" | "preview" | "lineage" | "health" | "access";

/** Every tab, in the order they are shown. */
export const TABLE_TABS: readonly TableTab[] = [
  "overview",
  "columns",
  "preview",
  "lineage",
  "health",
  "access",
];

/**
 * The address of one table, or of one of its tabs.
 *
 * @param environment - The environment's name.
 * @param namespace - The dotted namespace.
 * @param name - The table's name.
 * @param tab - The tab; Overview, which has no suffix, by default.
 * @returns The path, each part encoded.
 */
export function tablePath(
  environment: string,
  namespace: string,
  name: string,
  tab: TableTab = "overview",
): string {
  const table = `/catalog/${[environment, namespace, name].map(encodeURIComponent).join("/")}`;
  return tab === "overview" ? table : `${table}/${tab}`;
}

/**
 * Splits a dotted table reference at its last dot.
 *
 * @param dotted - A reference such as `gold.sales.fact_sales`.
 * @returns The namespace and the name; null when there is no dot or either part is empty.
 */
export function splitDotted(dotted: string): { namespace: string; name: string } | null {
  const dot = dotted.lastIndexOf(".");
  if (dot <= 0 || dot === dotted.length - 1) {
    return null;
  }
  return { namespace: dotted.slice(0, dot), name: dotted.slice(dot + 1) };
}
