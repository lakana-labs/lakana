/**
 * Under the heading of a table's page: the table's label, what it is and where, and the tabs.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { EnvironmentLabel } from "../../shell/EnvironmentLabel";
import { SectionTabs } from "../../shell/SectionTabs";
import { kindWord } from "../cells";
import { TABLE_TABS, tablePath } from "../paths";
import { useTableData } from "./TableContext";

/**
 * Renders the label when the table has one, the line "Fact · gold.sales" with the environment,
 * and the tabs, each with its own address. Every person sees the same tabs.
 *
 * @returns The header.
 */
export function TableHeader(): JSX.Element {
  const { t } = useTranslation();
  const { ref, table } = useTableData();
  return (
    <>
      {table.label !== null && <p className="table-label">{table.label}</p>}
      <p className="table-line">
        {t("catalog.secondLine", {
          kind: kindWord(t, table.kind, table.model?.kind ?? null),
          namespace: table.namespace,
        })}{" "}
        <EnvironmentLabel environment={ref.environment} />
      </p>
      <SectionTabs
        label={t("table.tabsLabel")}
        tabs={TABLE_TABS.map((tab) => ({
          to: tablePath(ref.environment, ref.namespace, ref.name, tab),
          label: t(`table.tabs.${tab}`),
        }))}
      />
    </>
  );
}
