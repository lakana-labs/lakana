/**
 * Row filters & masks' filter: the environment, the table in it, and who.
 *
 * @packageDocumentation
 */
import { type JSX, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EnvironmentChoice } from "../api/environments";
import type { PolicyFilter } from "../api/policies";
import { splitDotted } from "../catalog/paths";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { NamePicker } from "../shell/NamePicker";
import "./accessStrings";
import "./policyStrings";
import { dottedTableSearch } from "./suggest";
import { AppliedField, type FilterStep, WhoFilter } from "./WhoFilter";

/** Properties of {@link PolicyFilters}. */
export interface PolicyFiltersProps {
  /** The filter the lists show. */
  readonly filter: PolicyFilter;
  /**
   * The environments to choose from; null until they are read. None read means the person may
   * list none (a policy manager without `READ_ENVIRONMENT`): the environment is typed then.
   */
  readonly environments: readonly EnvironmentChoice[] | null;
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
 * Renders the Environment picker (with all environments; a typed name for a person who may list
 * none), the Table field (`namespace.table`, usable once an environment is chosen, applied on
 * Enter, when it is left or when a suggestion is chosen; in a listed environment it suggests the
 * tables the person may read there) and Who. A table without a dot says how to write it and
 * changes nothing.
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
  const [typedEnvironment, setTypedEnvironment] = useState(filter.environment ?? "");
  const [environmentFor, setEnvironmentFor] = useState(filter.environment);
  if (environmentFor !== filter.environment) {
    setEnvironmentFor(filter.environment);
    setTypedEnvironment(filter.environment ?? "");
  }
  const chooseEnvironment = (environment: string | null, step: FilterStep): void => {
    if (environment === filter.environment) return;
    const keep = environment !== null;
    onChange(
      {
        ...filter,
        environment,
        namespace: keep ? filter.namespace : null,
        table: keep ? filter.table : null,
      },
      step,
    );
  };
  // Tables are suggested in a listed environment; a typed one may not exist, and is not asked.
  const known = environments?.find((choice) => choice.name === filter.environment)?.name ?? null;
  const suggest = useMemo(() => (known === null ? null : dottedTableSearch(known)), [known]);
  const applyTable = (dotted: string = typed): void => {
    const text = dotted.trim();
    const split = text === "" ? { namespace: null, name: null } : splitDotted(text);
    setRefused(split === null);
    if (split === null || (split.namespace === filter.namespace && split.name === filter.table)) {
      return;
    }
    // The field already shows it; the address changes after this, and the table that comes
    // back sets the field to what it shows (the old one meanwhile is left alone, so the field
    // does not flicker back to it, which reopened the list of suggestions).
    onChange({ ...filter, namespace: split.namespace, table: split.name }, "replace");
  };
  return (
    <div className="access-filters" role="group" aria-label={t("policies.filters.label")}>
      {environments !== null && environments.length === 0 ? (
        <AppliedField
          label={t("policies.filters.environment")}
          value={typedEnvironment}
          onChange={setTypedEnvironment}
          help={t("policies.filters.environmentHelp")}
          onApply={() => {
            const typedName = typedEnvironment.trim();
            chooseEnvironment(typedName === "" ? null : typedName, "replace");
          }}
        />
      ) : (
        <EnvironmentPicker
          environments={environments ?? []}
          value={filter.environment}
          label={t("policies.filters.environment")}
          all={t("policies.filters.all")}
          onChange={(environment: string | null) => {
            chooseEnvironment(environment, "push");
          }}
        />
      )}
      <NamePicker
        label={t("policies.filters.table")}
        value={typed}
        onChange={(value) => {
          setTyped(value);
          setRefused(false);
        }}
        search={suggest}
        // Only a search says no match, and there is one only in a listed environment.
        noMatch={(text) => t("picker.noTable", { name: text, environment: known })}
        onPick={(choice) => {
          applyTable(choice.name);
        }}
        onEnter={() => {
          applyTable();
        }}
        onBlur={() => {
          applyTable();
        }}
        isDisabled={filter.environment === null}
        help={t(
          filter.environment === null
            ? "policies.filters.tableHelp"
            : known === null
              ? "policies.filters.tableHelpTyped"
              : "policies.filters.tableHelpSuggest",
        )}
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
