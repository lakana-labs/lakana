/**
 * Home's rows that open their object: the name as a link in the accent colour with the
 * environment beside it, one more column, and the quiet chevron; the whole row is clickable.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ListHead } from "../shell/ListHead";
import { EnvironmentLabel } from "./EnvironmentLabel";

/** Properties of {@link ObjectTable}. */
export interface ObjectTableProps {
  /** The heading of the second column, such as "State" or "Opened". */
  readonly column: string;
  /** The rows. */
  readonly children: ReactNode;
}

/**
 * A table of rows: the name, one more column, and the column of chevrons without a heading.
 *
 * @param props - The column and the rows.
 * @param props.column - The heading of the second column.
 * @param props.children - The rows.
 * @returns The table.
 */
export function ObjectTable({ column, children }: ObjectTableProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <table className="list">
      <ListHead columns={[t("list.name"), column]} hidden={[t("list.open")]} />
      <tbody>{children}</tbody>
    </table>
  );
}

/** Properties of {@link ObjectRow}. */
export interface ObjectRowProps {
  /** Where the row goes. */
  readonly to: string;
  /** The object's name. */
  readonly name: string;
  /** The object's environment; null for something of the whole instance. */
  readonly environment: string | null;
  /** The quiet line under the name, such as a time or the kind of object. */
  readonly second: string;
  /** The second column. */
  readonly aside: ReactNode;
}

/**
 * One row that opens its object.
 *
 * @param props - The object.
 * @param props.to - Where the row goes.
 * @param props.name - The object's name.
 * @param props.environment - The object's environment, or null.
 * @param props.second - The line under the name.
 * @param props.aside - The second column.
 * @returns The row.
 */
export function ObjectRow({ to, name, environment, second, aside }: ObjectRowProps): JSX.Element {
  return (
    <tr>
      <td>
        <Link className="row-name" to={to}>
          {name}
        </Link>{" "}
        <EnvironmentLabel environment={environment} />
        <span className="second">{second}</span>
      </td>
      <td>{aside}</td>
      <td>
        <span className="chevron" aria-hidden="true">
          ›
        </span>
      </td>
    </tr>
  );
}
