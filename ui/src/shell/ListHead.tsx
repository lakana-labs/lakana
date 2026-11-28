/**
 * The head row of a list: its visible column names, then the unnamed columns at the row's end
 * (actions, the chevron), named for screen readers only.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

import { SortHeader } from "./SortHeader";
import type { ListSortState } from "./useListSort";

/** How the columns of a {@link ListHead} sort. */
interface ListHeadSorts<F extends string> {
  /** Each visible column's field, in the order of `columns`; null for one that does not sort. */
  readonly by: readonly (F | null)[];
  /** The list's sort. */
  readonly sorting: Pick<ListSortState<F>, "header">;
}

/** Properties of {@link ListHead}. */
export interface ListHeadProps<F extends string> {
  /** The visible column names, in order; the name comes first. */
  readonly columns: readonly string[];
  /** The names of the columns at the row's end that show no heading. */
  readonly hidden: readonly string[];
  /** How the visible columns sort; none sorts without it. */
  readonly sorts?: ListHeadSorts<F>;
}

/**
 * Renders the `<thead>`.
 *
 * @template F - The fields the list sorts by.
 * @param props - The column names.
 * @param props.columns - The visible column names.
 * @param props.hidden - The names of the columns without a visible heading.
 * @param props.sorts - How the visible columns sort.
 * @returns The head.
 */
export function ListHead<F extends string>({
  columns,
  hidden,
  sorts,
}: ListHeadProps<F>): JSX.Element {
  return (
    <thead>
      <tr>
        {columns.map((column, index) => {
          const field = sorts?.by[index] ?? null;
          return field === null || sorts === undefined ? (
            <th scope="col" key={column}>
              {column}
            </th>
          ) : (
            <SortHeader key={column} {...sorts.sorting.header(field)}>
              {column}
            </SortHeader>
          );
        })}
        {hidden.map((column) => (
          <th scope="col" key={column}>
            <span className="visually-hidden">{column}</span>
          </th>
        ))}
      </tr>
    </thead>
  );
}
