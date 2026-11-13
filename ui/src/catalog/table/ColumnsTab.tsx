/**
 * The columns of a table as a table: on the Columns tab all of them with every fact, on Overview
 * the first few.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ColumnOut, ModelReferenceOut, TableOut } from "../../api";
import { Unknown } from "../cells";
import { splitDotted, tablePath } from "../paths";
import { editLanguage, languageName } from "./language";
import { useTableData } from "./TableContext";
import { BackToGit, TextEditor, useDescribe, useReturnFocus } from "./TextEditor";

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

/** The number of cells in a row of the full table that can be edited. */
const COLUMNS_WITH_ACTIONS = 8;

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

/** Properties of {@link ColumnRow}. */
interface ColumnRowProps {
  /** The column. */
  readonly column: ColumnOut;
  /** Whether to show nullability, joins, terms and the way to edit. */
  readonly full: boolean;
  /** Whether this column's editor is open. */
  readonly editing: boolean;
  /** Whether another column's editor is open: Edit waits until it is saved or cancelled. */
  readonly blocked: boolean;
  /** Opens or closes this column's editor. */
  readonly onEditing: (editing: boolean) => void;
}

/**
 * One column: its facts, and under it, while it is being edited, the editor of its label and
 * description. Focus returns to the column's Edit, or to the row when Edit is gone.
 *
 * @param props - The column and whether it is being edited.
 * @param props.column - The column.
 * @param props.full - Whether to show every fact and the way to edit.
 * @param props.editing - Whether the editor is open.
 * @param props.blocked - Whether another column's editor is open.
 * @param props.onEditing - Opens or closes the editor.
 * @returns The row, and the editor's row while it is open.
 */
function ColumnRow({ column, full, editing, blocked, onEditing }: ColumnRowProps): JSX.Element {
  const { t } = useTranslation();
  const { table, mayEdit } = useTableData();
  const { lang, named, saveColumn } = useDescribe(column.description_language);
  const { edit, fallback: row, returned } = useReturnFocus<HTMLTableRowElement>();
  const role = columnRole(table, column.name);
  const roleWord = role === null ? null : t(`table.columns.roles.${role}`);
  const editable = full && mayEdit;
  const edited = column.description_source === "edited" || column.label_source === "edited";
  const close = (): void => {
    onEditing(false);
    returned();
  };
  return (
    <>
      <tr tabIndex={-1} ref={row}>
        <td className="column-name">
          {column.name}
          {column.label !== null && <span className="second column-label">{column.label}</span>}
        </td>
        <td>
          <code className="column-type">{column.type}</code>
          {!full && roleWord !== null && <span className="second">{roleWord}</span>}
        </td>
        {full && (
          <td>{column.nullable ? t("table.columns.nullable") : t("table.columns.required")}</td>
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
        {editable && (
          <td className="column-actions">
            {!editing && (
              <Button
                className="button"
                ref={edit}
                isDisabled={blocked}
                aria-label={t(blocked ? "table.edit.buttonBlocked" : "table.edit.buttonFor", {
                  name: column.name,
                })}
                onPress={() => {
                  onEditing(true);
                }}
              >
                {t("table.edit.button")}
              </Button>
            )}
            {!editing && edited && (
              <BackToGit
                name={column.name}
                reset={() =>
                  saveColumn(column.name, {
                    ...(column.description_source === "edited" ? { description: null } : {}),
                    ...(column.label_source === "edited" ? { label: null } : {}),
                  })
                }
                onDone={returned}
              />
            )}
          </td>
        )}
      </tr>
      {full && editing && (
        <tr className="text-editor-row">
          <td colSpan={COLUMNS_WITH_ACTIONS}>
            <TextEditor
              texts={{ description: column.description, label: column.label }}
              lang={lang}
              named={named}
              onSave={(change) => saveColumn(column.name, change)}
              onDone={close}
            />
          </td>
        </tr>
      )}
    </>
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
  const { mayEdit } = useTableData();
  const [editing, setEditing] = useState<string | null>(null);
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
          {full && mayEdit && (
            <th scope="col">
              <span className="visually-hidden">{t("table.columns.actions")}</span>
            </th>
          )}
        </tr>
      </thead>
      <tbody>
        {columns.map((column) => (
          <ColumnRow
            key={column.name}
            column={column}
            full={full}
            editing={editing === column.name}
            blocked={editing !== null && editing !== column.name}
            onEditing={(open) => {
              setEditing(open ? column.name : null);
            }}
          />
        ))}
      </tbody>
    </table>
  );
}

/**
 * What a person who may edit should know before they do: that a deploy keeps an edited text, and
 * that a page with no text in their language shows and edits the main language.
 *
 * @returns The notes, or nothing for someone who may not edit.
 */
function ColumnNotes(): JSX.Element | null {
  const { t, i18n } = useTranslation();
  const { table, mayEdit } = useTableData();
  const { lang, fallback } = editLanguage(table, { description_language: null });
  if (!mayEdit) {
    return null;
  }
  const edited = table.schema.some(
    (column) => column.description_source === "edited" || column.label_source === "edited",
  );
  return (
    <>
      {fallback && (
        <p className="second">
          {t("table.edit.fallback", { language: languageName(lang, i18n.language) })}
        </p>
      )}
      {edited && <p className="second">{t("table.edit.deployKeeps")}</p>}
    </>
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
    <>
      <ColumnNotes />
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
    </>
  );
}
