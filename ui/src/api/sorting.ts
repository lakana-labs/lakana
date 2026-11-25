/**
 * The sort a long list is read in: a field of the list and a direction, which the server
 * applies across every page (#787).
 *
 * @packageDocumentation
 */
import type { SortOrder } from "./catalog";

/** A list's chosen sort, as its route takes it in `sort` and `order`. */
export interface ListSortChoice {
  /** The field, one the list sorts by. */
  readonly sort: string;
  /** The direction. */
  readonly order: SortOrder;
}

/**
 * Adds a sort to a list's query; nothing for the list's own order.
 *
 * @param query - The query of the list's request.
 * @param choice - The sort, or null for the list's own order.
 */
export function setSort(query: URLSearchParams, choice: ListSortChoice | null): void {
  if (choice !== null) {
    query.set("sort", choice.sort);
    query.set("order", choice.order);
  }
}
