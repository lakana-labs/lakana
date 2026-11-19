/**
 * Row filters & masks' filter: the environment, the table in it, and who.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EnvironmentChoice } from "../api/environments";
import type { PolicyFilter } from "../api/policies";
import { splitDotted } from "../catalog/paths";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import "./accessStrings";
import "./policyStrings";
import { AppliedField, type FilterStep, WhoFilter } from "./WhoFilter";

/** Properties of {@link PolicyFilters}. */
export interface PolicyFiltersProps {
  /** The filter the lists show. */
  readonly filter: PolicyFilter;
  /** The environments to choose from. */
  readonly environments: readonly EnvironmentChoice[];
  /** Called with the next filter, and how to keep it in the history. */
  readonly onChange: (next: PolicyFilter, step: FilterStep) => void;
}

/**
 * The table as the Table field shows it.
 *
 * @param filter - The filter.
 * @returns `namespace.table`, or empty.
 */
function dottedOf(filter: PolicyFilter): string {
  return filter.namespace === null || filter.table === null
    ? ""
    : `${filter.namespace}.${filter.table}`;
}

/**
 * Renders the Environment picker (with all environments), the Table field (`namespace.table`,
 * applied on Enter or when it is left, usable once an environment is chosen) and Who. A table
 * without a dot says how to write it and changes nothing.
 *
 * @param props - The filter, the environments and the handler.
 * @param props.filter - The filter the lists show.
 * @param props.environments - The environments to choose from.
 * @param props.onChange - Called with the next filter.
 * @returns The filters.
 */
export function PolicyFilters({ filter, environments, onChange }: PolicyFiltersProps): JSX.Element {
  const { t } = useTranslation();
  const [typed, setTyped] = useState(() => dottedOf(filter));
  const [typedFor, setTypedFor] = useState(() => dottedOf(filter));
  const [refused, setRefused] = useState(false);
  // A table from elsewhere (Clear filters, Back) sets the field again.
  if (typedFor !== dottedOf(filter)) {
    setTypedFor(dottedOf(filter));
    setTyped(dottedOf(filter));
    setRefused(false);
  }
  const applyTable = (): void => {
    const text = typed.trim();
    const split = text === "" ? { namespace: null, name: null } : splitDotted(text);
    setRefused(split === null);
    if (split === null || (split.namespace === filter.namespace && split.name === filter.table)) {
      return;
    }
    setTypedFor(text);
    onChange({ ...filter, namespace: split.namespace, table: split.name }, "replace");
  };
  return (
    <div className="access-filters" role="group" aria-label={t("policies.filters.label")}>
      <EnvironmentPicker
        environments={environments}
        value={filter.environment}
        label={t("policies.filters.environment")}
        all={t("policies.filters.all")}
        onChange={(environment: string | null) => {
          const keep = environment !== null;
          onChange(
            {
              ...filter,
              environment,
              namespace: keep ? filter.namespace : null,
              table: keep ? filter.table : null,
            },
            "push",
          );
        }}
      />
      <AppliedField
        label={t("policies.filters.table")}
        value={typed}
        onChange={(value) => {
          setTyped(value);
          setRefused(false);
        }}
        onApply={applyTable}
        isDisabled={filter.environment === null}
        help={t("policies.filters.tableHelp")}
        error={refused ? t("policies.filters.tableDotted") : null}
      />
      <WhoFilter
        who={filter.who}
        onChange={(who, step) => {
          onChange({ ...filter, who }, step);
        }}
      />
    </div>
  );
}
