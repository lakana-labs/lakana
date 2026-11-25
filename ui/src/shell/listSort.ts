/**
 * The sort of a long list as the address keeps it: `?sort=FIELD&order=asc|desc`, so a shared link
 * opens the list sorted the same way (#787).
 *
 * @packageDocumentation
 */
import type { SortOrder } from "../api/catalog";
import type { ListSortChoice } from "../api/sorting";

/** The fields a list sorts by and the order it has when the address names none. */
export interface ListSortSpec<F extends string> {
  /** Each field, with the direction it starts in when its header is pressed. */
  readonly fields: Readonly<Record<F, SortOrder>>;
  /** The column the list's own order is, shown as sorted; null when it is no column. */
  readonly natural: { readonly field: F; readonly order: SortOrder } | null;
}

/** The sort a list is shown in. */
export interface ListSort<F extends string> {
  /** The field; null for the list's own order, which is no column. */
  readonly field: F | null;
  /** The direction. */
  readonly order: SortOrder;
}

/**
 * Tells whether a text is one of a list's fields.
 *
 * @template F - The fields.
 * @param spec - The list's fields.
 * @param value - The text.
 * @returns Whether it is a field.
 */
function isField<F extends string>(spec: ListSortSpec<F>, value: string | null): value is F {
  return value !== null && Object.hasOwn(spec.fields, value);
}

/**
 * Reads a list's sort from the address; a field the list does not have, or a direction that is
 * neither `asc` nor `desc`, gives the list's own order or the field's own direction.
 *
 * @template F - The fields.
 * @param params - The address's query.
 * @param spec - The list's fields.
 * @returns The sort.
 */
export function readListSort<F extends string>(
  params: URLSearchParams,
  spec: ListSortSpec<F>,
): ListSort<F> {
  const field = params.get("sort");
  if (!isField(spec, field)) {
    return spec.natural ?? { field: null, order: "asc" };
  }
  const order = params.get("order");
  return { field, order: order === "asc" || order === "desc" ? order : spec.fields[field] };
}

/**
 * The sort after a header was pressed: the sorted column flips its direction, another column
 * starts in its own.
 *
 * @template F - The fields.
 * @param current - The sort shown.
 * @param field - The column pressed.
 * @param spec - The list's fields.
 * @returns The new sort.
 */
export function nextListSort<F extends string>(
  current: ListSort<F>,
  field: F,
  spec: ListSortSpec<F>,
): ListSort<F> {
  if (current.field === field) {
    return { field, order: current.order === "asc" ? "desc" : "asc" };
  }
  return { field, order: spec.fields[field] };
}

/**
 * Writes a sort into the address's query, leaving out what is the list's own order and a field's
 * own direction; every other parameter (the filters) stays.
 *
 * @template F - The fields.
 * @param params - The query, changed in place.
 * @param sort - The sort.
 * @param spec - The list's fields.
 * @returns The same query.
 */
export function writeListSort<F extends string>(
  params: URLSearchParams,
  sort: ListSort<F>,
  spec: ListSortSpec<F>,
): URLSearchParams {
  params.delete("sort");
  params.delete("order");
  const natural = spec.natural;
  if (sort.field === null || (natural?.field === sort.field && natural.order === sort.order)) {
    return params;
  }
  params.set("sort", sort.field);
  if (sort.order !== spec.fields[sort.field]) {
    params.set("order", sort.order);
  }
  return params;
}

/**
 * The sort as a list's request sends it; null for the list's own order, which the server gives
 * without one.
 *
 * @template F - The fields.
 * @param sort - The sort.
 * @param spec - The list's fields.
 * @returns The choice.
 */
export function sortChoice<F extends string>(
  sort: ListSort<F>,
  spec: ListSortSpec<F>,
): ListSortChoice | null {
  const natural = spec.natural;
  if (sort.field === null || (natural?.field === sort.field && natural.order === sort.order)) {
    return null;
  }
  return { sort: sort.field, order: sort.order };
}
