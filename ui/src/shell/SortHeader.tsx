/**
 * The header of a column a list can be sorted by: a button that sorts, `aria-sort` on the
 * sorted column and an arrow beside its name.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useEffect, useRef } from "react";
import { Button } from "react-aria-components";

import type { ListSort } from "./listSort";

/** Properties of {@link SortHeader}. */
export interface SortHeaderProps<F extends string> {
  /** The column's field. */
  readonly field: F;
  /** The column's name. */
  readonly children: ReactNode;
  /** The sort the list is shown in. */
  readonly sort: ListSort<F>;
  /** Called with the field when the header is pressed. */
  readonly onSort: (field: F) => void;
  /** The cell's class, such as `number`. */
  readonly className?: string | undefined;
  /**
   * Whether this header was pressed last: drawn again while the focus is lost (a list that shows
   * its skeleton while the new order is read), it takes the focus back.
   */
  readonly pressed?: boolean;
}

/**
 * Renders the `<th>`. The button is reached by Tab and pressed by Enter or Space, as every
 * React Aria button; the sorted column says its direction with `aria-sort`, and the arrow (▲ from
 * the smallest, ▼ from the largest) repeats it for the eye only.
 *
 * @template F - The list's fields.
 * @param props - The field, its name, the sort and the handler.
 * @param props.field - The column's field.
 * @param props.children - The column's name.
 * @param props.sort - The sort shown.
 * @param props.onSort - Called with the field.
 * @param props.className - The cell's class.
 * @param props.pressed - Whether this header was pressed last.
 * @returns The header cell.
 */
export function SortHeader<F extends string>({
  field,
  children,
  sort,
  onSort,
  className,
  pressed = false,
}: SortHeaderProps<F>): JSX.Element {
  const sorted = sort.field === field;
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (pressed && document.activeElement === document.body) {
      button.current?.focus();
    }
    // Only when the header is drawn: a list that redraws takes the focus back once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <th
      scope="col"
      className={className}
      {...(sorted ? { "aria-sort": sort.order === "asc" ? "ascending" : "descending" } : {})}
    >
      <Button
        ref={button}
        className="sort-head"
        onPress={() => {
          onSort(field);
        }}
      >
        {children}
        {sorted && (
          <span className="sort-arrow" aria-hidden="true">
            {sort.order === "asc" ? "▲" : "▼"}
          </span>
        )}
      </Button>
    </th>
  );
}
