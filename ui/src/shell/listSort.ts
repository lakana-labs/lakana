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
  /**
   * The names of the list's sort in the address when a page shows two sorted lists:
   * `<param>Sort` and `<param>Order`; `sort` and `order` without one.
   */
  readonly param?: string;
}

/**
 * The names of a list's sort in the address.
 *
 * @param spec - The list's fields.
 * @param spec.param - The list's own prefix, if it has one.
 * @returns The names of its field and its direction.
 */
function names({ param }: { readonly param?: string }): { sort: string; order: string } {
  return param === undefined
    ? { sort: "sort", order: "order" }
    : { sort: `${param}Sort`, order: `${param}Order` };
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
  const named = names(spec);
  const field = params.get(named.sort);
  if (!isField(spec, field)) {
    return spec.natural ?? { field: null, order: "asc" };
  }
  const order = params.get(named.order);
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
  const named = names(spec);
  params.delete(named.sort);
  params.delete(named.order);
  const natural = spec.natural;
  if (sort.field === null || (natural?.field === sort.field && natural.order === sort.order)) {
    return params;
  }
  params.set(named.sort, sort.field);
  if (sort.order !== spec.fields[sort.field]) {
    params.set(named.order, sort.order);
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

/** What a list sorted in the browser compares in one column: a name, a number or a time. */
export type SortValue = string | number | null;

/**
 * Lowers the letters A to Z only, the letters every database the server runs on lowers alike.
 *
 * @param text - The name.
 * @returns The name with A to Z in lower case and every other character as it was.
 */
function foldAscii(text: string): string {
  return text.replace(/[A-Z]/gu, (letter) => letter.toLowerCase());
}

/**
 * Compares two texts by code point, as the server's names compare (`COLLATE "C"`), not by the
 * UTF-16 units `<` compares: a character beyond the first plane sorts after every one in it.
 *
 * @param a - One text.
 * @param b - The other.
 * @returns Below zero, zero or above zero.
 */
function byCodePoint(a: string, b: string): number {
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    // Inside the text, so a number; `Number` keeps the type without a branch.
    const left = Number(a.codePointAt(i));
    const right = Number(b.codePointAt(j));
    if (left !== right) return left - right;
    i += left > 0xffff ? 2 : 1;
    j += right > 0xffff ? 2 : 1;
  }
  return a.length - i - (b.length - j);
}

/**
 * Compares two values of one column going up: names with A to Z in any letter case and then by
 * code point (as the server sorts names everywhere), numbers by their value.
 *
 * @param left - One value, not null.
 * @param right - The other, of the same kind.
 * @returns Below zero, zero or above zero.
 */
function compareValues(left: string | number, right: string | number): number {
  // One column holds values of one kind: a name with a name, a number with a number.
  if (typeof left === "string") {
    return byCodePoint(foldAscii(left), foldAscii(String(right)));
  }
  return left - Number(right);
}

/**
 * Sorts a list the browser holds whole (one that is not paged by the server) by a column:
 * a row without a value comes last in either direction, and rows of one value keep the list's
 * own order.
 *
 * @template T - A row.
 * @template F - The fields.
 * @param rows - The rows in the list's own order.
 * @param sort - The sort shown.
 * @param keys - For each field, the value of a row it sorts by.
 * @returns The rows in the sort's order (the same rows when no column is sorted).
 */
export function sortRows<T, F extends string>(
  rows: readonly T[],
  sort: ListSort<F>,
  keys: Readonly<Record<F, (row: T) => SortValue>>,
): readonly T[] {
  const { field } = sort;
  if (field === null) return rows;
  const key = keys[field];
  const down = sort.order === "desc" ? -1 : 1;
  return rows
    .map((row, index) => ({ row, index, value: key(row) }))
    .sort((left, right) => {
      if (left.value === null || right.value === null) {
        const nulls = (left.value === null ? 1 : 0) - (right.value === null ? 1 : 0);
        return nulls === 0 ? left.index - right.index : nulls;
      }
      return down * compareValues(left.value, right.value) || left.index - right.index;
    })
    .map(({ row }) => row);
}
