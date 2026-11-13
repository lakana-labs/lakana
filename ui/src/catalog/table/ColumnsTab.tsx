/**
 * The columns of a table as a table: on the Columns tab all of them with every fact, on Overview
 * the first few.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ColumnOut, ModelReferenceOut, TableOut } from "../../api";
import { Unknown } from "../cells";
import { splitDotted, tablePath } from "../paths";
import { useTableData } from "./TableContext";

/**
 * What a column is in the table's model: part of its key, or a measure.
 *
 * @param table - The table.
 * @param column - The column's name.
 * @returns `key` for a key column (also when it is a measure), `measure`, or null.
 */
export function columnRole(table: TableOut, column: string): "key" | "measure" | null {
  if (table.model?.key.includes(column) === true) {
    return "key";
  }
  return table.model?.measures.some((measure) => measure.column === column) === true
    ? "measure"
    : null;
}

/**
 * The references the table's header declares for a column.
 *
 * @param table - The table.
 * @param column - The column's name.
 * @returns The references whose referencing column it is; none for a table without a model.
 */
export function columnJoins(table: TableOut, column: string): readonly ModelReferenceOut[] {
  return table.model?.references.filter((reference) => reference.column === column) ?? [];
}

/** Properties of {@link ColumnsTable}. */
export interface ColumnsTableProps {
  /** The columns to show, in order. */
  readonly columns: readonly ColumnOut[];
  /** Whether to show nullability, joins and terms too; Overview shows name, type and text. */
  readonly full: boolean;
}

/**
 * One reference as a link to the table it points at, or as plain text when that table does not
 * exist or its name cannot be split into a namespace and a name.
 *
 * @param props - The reference.
 * @param props.join - The reference.
 * @returns The link or the text.
 */
function Join({ join }: { readonly join: ModelReferenceOut }): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const place = join.target_exists ? splitDotted(join.target) : null;
  const text = `${join.target}.${join.target_column}`;
  return (
    <span className="column-join">
      <span className="visually-hidden">{t("table.columns.joinsTo")} </span>
      <span aria-hidden="true">→</span>{" "}
      {place === null ? (
        text
      ) : (
        <Link to={tablePath(ref.environment, place.namespace, place.name)}>{text}</Link>
      )}
    </span>
  );
}

/**
 * Renders the columns as a table. The facts a reader needs to join and read the table are
 * there; a glossary term is plain text until the glossary has its page.
 *
 * @param props - The columns and whether to show every fact.
 * @param props.columns - The columns.
 * @param props.full - Whether to show nullability, joins and terms.
 * @returns The table.
 */
export function ColumnsTable({ columns, full }: ColumnsTableProps): JSX.Element {
  const { t } = useTranslation();
  const { table } = useTableData();
  return (
    <table className="list columns-table">
      <thead>
        <tr>
          <th scope="col">{t("table.columns.name")}</th>
          <th scope="col">{t("table.columns.type")}</th>
          {full && <th scope="col">{t("table.columns.nulls")}</th>}
          {full && <th scope="col">{t("table.columns.role")}</th>}
          {full && <th scope="col">{t("table.columns.joins")}</th>}
          <th scope="col">{t("table.columns.description")}</th>
          {full && <th scope="col">{t("table.columns.terms")}</th>}
        </tr>
      </thead>
      <tbody>
        {columns.map((column) => {
          const role = columnRole(table, column.name);
          const roleWord = role === null ? null : t(`table.columns.roles.${role}`);
          return (
            <tr key={column.name}>
              <td className="column-name">{column.name}</td>
              <td>
                <code className="column-type">{column.type}</code>
                {!full && roleWord !== null && <span className="second">{roleWord}</span>}
              </td>
              {full && (
                <td>
                  {column.nullable ? t("table.columns.nullable") : t("table.columns.required")}
                </td>
              )}
              {full && <td>{roleWord}</td>}
              {full && (
                <td>
                  {columnJoins(table, column.name).map((join) => (
                    <Join key={join.id} join={join} />
                  ))}
                </td>
              )}
              <td className="column-description">
                {column.description ?? <Unknown words={t("table.columns.noDescription")} />}
              </td>
              {full && <td>{column.terms.map((term) => term.name).join(", ")}</td>}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * The Columns tab: every column in a region that scrolls on its own, so a wide schema never
 * makes the page scroll sideways.
 *
 * @returns The tab's content.
 */
export function ColumnsTab(): JSX.Element {
  const { t } = useTranslation();
  const { table } = useTableData();
  if (table.schema.length === 0) {
    return <p className="empty">{t("table.columns.none")}</p>;
  }
  return (
    <div
      className="list-scroll"
      role="region"
      aria-label={t("table.columns.region")}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <ColumnsTable columns={table.schema} full />
    </div>
  );
}
