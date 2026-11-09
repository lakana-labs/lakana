/**
 * The head row of a list: its visible column names, then the unnamed columns at the row's end
 * (actions, the chevron), named for screen readers only.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/** Properties of {@link ListHead}. */
export interface ListHeadProps {
  /** The visible column names, in order; the name comes first. */
  readonly columns: readonly string[];
  /** The names of the columns at the row's end that show no heading. */
  readonly hidden: readonly string[];
}

/**
 * Renders the `<thead>`.
 *
 * @param props - The column names.
 * @param props.columns - The visible column names.
 * @param props.hidden - The names of the columns without a visible heading.
 * @returns The head.
 */
export function ListHead({ columns, hidden }: ListHeadProps): JSX.Element {
  return (
    <thead>
      <tr>
        {columns.map((column) => (
          <th scope="col" key={column}>
            {column}
          </th>
        ))}
        {hidden.map((column) => (
          <th scope="col" key={column}>
            <span className="visually-hidden">{column}</span>
          </th>
        ))}
      </tr>
    </thead>
  );
}
