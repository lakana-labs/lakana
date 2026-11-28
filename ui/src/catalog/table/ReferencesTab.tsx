/**
 * The References tab: what the table's header declares it points at, and which tables point at
 * it. A table the person may not read is counted, never named. Its code loads only when the tab
 * is opened.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getReferences } from "../../api/manage";
import type { ModelReferenceOut } from "../../api";
import { VisibleText } from "../../shell/VisibleText";
import { splitDotted, tablePath } from "../paths";
import { TabBody } from "./TabBody";
import { type ListSortSpec, sortRows } from "../../shell/listSort";
import { SortHeader } from "../../shell/SortHeader";
import { useListSort } from "../../shell/useListSort";
import { useTableData } from "./TableContext";

/** Properties of {@link TableName}. */
interface TableNameProps {
  /** The environment the link stays in. */
  readonly environment: string;
  /** The table's dotted name. */
  readonly dotted: string;
  /** Whether the table exists; a table that does not has no page to link to. */
  readonly exists: boolean;
}

/**
 * A table's dotted name, linked to its page when it exists and has a name that can be split.
 *
 * @param props - The table.
 * @param props.environment - The environment the link stays in.
 * @param props.dotted - The table's dotted name.
 * @param props.exists - Whether the table exists.
 * @returns The link, or the name as text.
 */
function TableName({ environment, dotted, exists }: TableNameProps): JSX.Element {
  const parts = splitDotted(dotted);
  const name = <VisibleText text={dotted} breakable name />;
  return exists && parts !== null ? (
    <Link to={tablePath(environment, parts.namespace, parts.name)}>{name}</Link>
  ) : (
    name
  );
}

/** Properties of {@link Panel}. */
interface PanelProps {
  /** The panel's heading. */
  readonly heading: string;
  /** The references to list. */
  readonly rows: readonly ModelReferenceOut[];
  /** How many more the person may not read. */
  readonly hidden: number;
  /** The sentence that counts the hidden ones. */
  readonly hiddenText: string;
  /** Which way the panel looks: what it shows in its table column and its reference cell. */
  readonly direction: "outgoing" | "incoming";
}

/** How the references this table makes sort, in the browser. */
const OUTGOING_SORT: ListSortSpec<"table" | "reference"> = {
  fields: { table: "asc", reference: "asc" },
  natural: null,
  param: "out",
};

/** How the references to this table sort, in the browser. */
const INCOMING_SORT: ListSortSpec<"table" | "reference"> = { ...OUTGOING_SORT, param: "in" };

/**
 * One direction's references as a flat panel.
 *
 * @param props - The references and what is hidden.
 * @param props.heading - The panel's heading.
 * @param props.rows - The references.
 * @param props.hidden - How many more are hidden.
 * @param props.hiddenText - The sentence that counts them.
 * @param props.direction - Which way the panel looks.
 * @returns The panel.
 */
function Panel({ heading, rows, hidden, hiddenText, direction }: PanelProps): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const outgoing = direction === "outgoing";
  const sorting = useListSort(outgoing ? OUTGOING_SORT : INCOMING_SORT);
  const note = (row: ModelReferenceOut): ReactNode => {
    if (outgoing && !row.target_exists) {
      return t("table.referencesTab.missing");
    }
    return row.at === null ? null : (
      <VisibleText text={t("table.referencesTab.validAt", { at: row.at })} />
    );
  };
  return (
    <section className="flat-panel">
      <h2 className="block-heading">{heading}</h2>
      {rows.length === 0 ? (
        <p className="second">{t("table.referencesTab.none")}</p>
      ) : (
        <table className="list list-static">
          <thead>
            <tr>
              {outgoing ? (
                <SortHeader {...sorting.header("reference")}>
                  {t("table.referencesTab.reference")}
                </SortHeader>
              ) : (
                <>
                  <SortHeader {...sorting.header("table")}>
                    {t("table.referencesTab.table")}
                  </SortHeader>
                  <SortHeader {...sorting.header("reference")}>
                    {t("table.referencesTab.reference")}
                  </SortHeader>
                </>
              )}
              {/* A note says what the model declares about one reference: it does not sort. */}
              <th scope="col">{t("table.referencesTab.note")}</th>
            </tr>
          </thead>
          <tbody>
            {sortRows(rows, sorting.sort, {
              table: (row) => row.table,
              reference: (row) =>
                outgoing
                  ? `${row.column} ${row.target}.${row.target_column}`
                  : `${row.column} ${row.target_column}`,
            }).map((row) => (
              <tr key={row.id}>
                {outgoing ? (
                  <td>
                    <VisibleText text={row.column} breakable name /> →{" "}
                    <TableName
                      environment={ref.environment}
                      dotted={row.target}
                      exists={row.target_exists}
                    />
                    <VisibleText text={`.${row.target_column}`} breakable name />
                  </td>
                ) : (
                  <>
                    <td>
                      <TableName environment={ref.environment} dotted={row.table} exists />
                    </td>
                    <td>
                      <VisibleText text={row.column} breakable name /> →{" "}
                      <VisibleText text={row.target_column} breakable name />
                    </td>
                  </>
                )}
                <td>{note(row)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {hidden > 0 && <p className="second">{hiddenText}</p>}
    </section>
  );
}

/**
 * The References tab. A failed read says why with Try again; the focus then goes to the tab,
 * because the button is gone.
 *
 * @returns The tab's content.
 */
export default function ReferencesTab(): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const { environment, namespace, name } = ref;
  const load = useCallback(
    () => getReferences({ environment, namespace, name }),
    [environment, namespace, name],
  );
  return (
    <TabBody className="references-tab" load={load} reading={t("table.referencesTab.reading")}>
      {(read) =>
        read.outgoing.length === 0 &&
        read.incoming.length === 0 &&
        read.hidden_outgoing === 0 &&
        read.hidden_incoming === 0 ? (
          <p className="second">{t("table.referencesTab.empty")}</p>
        ) : (
          <>
            <Panel
              heading={t("table.referencesTab.outgoing")}
              rows={read.outgoing}
              hidden={read.hidden_outgoing}
              hiddenText={t("table.referencesTab.hiddenOutgoing", { count: read.hidden_outgoing })}
              direction="outgoing"
            />
            <Panel
              heading={t("table.referencesTab.incoming")}
              rows={read.incoming}
              hidden={read.hidden_incoming}
              hiddenText={t("table.referencesTab.hiddenIncoming", { count: read.hidden_incoming })}
              direction="incoming"
            />
          </>
        )
      }
    </TabBody>
  );
}
