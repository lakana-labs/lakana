/**
 * The sort of a long list, kept in the address.
 *
 * @packageDocumentation
 */
import { useMemo, useState } from "react";

import type { ListSortChoice } from "../api/sorting";
import {
  type ListSort,
  type ListSortSpec,
  nextListSort,
  readListSort,
  sortChoice,
  writeListSort,
} from "./listSort";
import { useAddressParams } from "./useAddressParams";

/** What {@link useListSort} hands back. */
export interface ListSortState<F extends string> {
  /** The sort shown. */
  readonly sort: ListSort<F>;
  /** The sort as the list's request sends it; null for the list's own order. */
  readonly choice: ListSortChoice | null;
  /** Sorts by a column: pressed again, the other way. One step of the history; filters stay. */
  readonly sortBy: (field: F) => void;
  /** The column whose header was pressed last, which takes the focus back if the list redraws. */
  readonly pressed: F | null;
  /**
   * The properties of a column's `SortHeader`: its field, the sort, the handler and whether it
   * was pressed last.
   */
  readonly header: (field: F) => {
    readonly field: F;
    readonly sort: ListSort<F>;
    readonly onSort: (field: F) => void;
    readonly pressed: boolean;
  };
}

/**
 * Reads a list's sort from the address and changes it there. A list whose pages are keyed by the
 * sort starts again from its first page when it changes.
 *
 * @template F - The fields.
 * @param spec - The list's fields; a constant, so the sort is not read again on every render.
 * @returns The sort and how to change it.
 */
export function useListSort<F extends string>(spec: ListSortSpec<F>): ListSortState<F> {
  const [params, update] = useAddressParams();
  const sort = readListSort(params, spec);
  const { field, order } = sort;
  const shown = useMemo(() => ({ field, order }), [field, order]);
  // The spec is a module constant; the choice changes with the sort alone.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const choice = useMemo(() => sortChoice(shown, spec), [shown]);
  const [last, setLast] = useState<F | null>(null);
  const sortBy = (pressed: F): void => {
    setLast(pressed);
    update((current) =>
      writeListSort(current, nextListSort(readListSort(current, spec), pressed, spec), spec),
    );
  };
  return {
    sort: shown,
    choice,
    pressed: last,
    sortBy,
    header: (field: F) => ({ field, sort: shown, onSort: sortBy, pressed: last === field }),
  };
}
