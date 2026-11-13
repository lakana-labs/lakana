/**
 * What the catalog list shows, kept in the address so a view can be linked and survives a reload.
 *
 * @packageDocumentation
 */
import { TABLE_SORTS } from "../api/catalog";
import type { ListTablesQuery, SortOrder, TableSort } from "../api/catalog";

/** The place, the search and the sort of the list. */
export interface CatalogView {
  /** The environment shown; null for all. */
  readonly environment: string | null;
  /** The dotted namespace shown; null for all, and always null without an environment. */
  readonly namespace: string | null;
  /** The search text; empty for none. */
  readonly q: string;
  /** The column the list is sorted by. */
  readonly sort: TableSort;
  /** The direction. */
  readonly order: SortOrder;
}

/** All tables, by name, A to Z. */
export const DEFAULT_VIEW: CatalogView = {
  environment: null,
  namespace: null,
  q: "",
  sort: "name",
  order: "asc",
};

/** The longest search text; the server refuses more. */
export const Q_MAX = 200;

/**
 * The direction a column starts in: text from A to Z, health worst first, numbers and times
 * largest and newest first.
 *
 * @param sort - The column.
 * @returns The direction.
 */
export function defaultOrder(sort: TableSort): SortOrder {
  return sort === "name" || sort === "owner" || sort === "health" ? "asc" : "desc";
}

/**
 * Reads a view from the address; anything it does not understand becomes the default.
 *
 * @param params - The address's query.
 * @returns The view.
 */
export function readView(params: URLSearchParams): CatalogView {
  const environment = params.get("env") ?? "";
  const namespace = params.get("ns") ?? "";
  const requested = params.get("sort");
  const sort = TABLE_SORTS.find((key) => key === requested) ?? DEFAULT_VIEW.sort;
  const order = params.get("order");
  return {
    environment: environment === "" ? null : environment,
    namespace: environment === "" || namespace === "" ? null : namespace,
    q: (params.get("q") ?? "").replaceAll("\u0000", "").trim().slice(0, Q_MAX),
    sort,
    order: order === "asc" || order === "desc" ? order : defaultOrder(sort),
  };
}

/**
 * Writes a view as an address query, leaving out what is the default.
 *
 * @param view - The view.
 * @returns The query with its `?`, or an empty string for the default view.
 */
export function viewSearch(view: CatalogView): string {
  const params = new URLSearchParams();
  if (view.environment !== null) {
    params.set("env", view.environment);
    if (view.namespace !== null) {
      params.set("ns", view.namespace);
    }
  }
  if (view.q !== "") {
    params.set("q", view.q);
  }
  if (view.sort !== DEFAULT_VIEW.sort) {
    params.set("sort", view.sort);
  }
  if (view.order !== defaultOrder(view.sort)) {
    params.set("order", view.order);
  }
  const text = params.toString();
  return text === "" ? "" : `?${text}`;
}

/**
 * The request for a view's page.
 *
 * @param view - The view.
 * @param cursor - Where the page starts; null for the first.
 * @returns The query.
 */
export function viewQuery(view: CatalogView, cursor: string | null): ListTablesQuery {
  return { ...view, cursor };
}

/**
 * The view after a click on a column header: the sorted column flips its direction, another
 * column starts in its own.
 *
 * @param view - The view.
 * @param sort - The column clicked.
 * @returns The new view.
 */
export function sortedBy(view: CatalogView, sort: TableSort): CatalogView {
  if (view.sort === sort) {
    return { ...view, order: view.order === "asc" ? "desc" : "asc" };
  }
  return { ...view, sort, order: defaultOrder(sort) };
}

/**
 * The view in another place, keeping the search and the sort.
 *
 * @param view - The view.
 * @param environment - The environment; null for all.
 * @param namespace - The dotted namespace; ignored without an environment.
 * @returns The new view.
 */
export function placed(
  view: CatalogView,
  environment: string | null,
  namespace: string | null,
): CatalogView {
  return { ...view, environment, namespace: environment === null ? null : namespace };
}
