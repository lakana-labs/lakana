/**
 * The Grants list's filters: who, on exactly what, and what (a role or an action).
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Button,
  Header,
  Label,
  ListBox,
  ListBoxItem,
  ListBoxSection,
  Popover,
  Select,
  SelectValue,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { EnvironmentChoice } from "../api/environments";
import { ACTIONS, type GrantFilter, type GrantWhat, type GrantWhere, ROLES } from "../api/grants";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import "./accessStrings";
import { AppliedField, type FilterStep, WhoFilter } from "./WhoFilter";
import { actionWords, roleName } from "./words";

/** Properties of {@link GrantFilters}. */
export interface GrantFiltersProps {
  /** The filter the list shows. */
  readonly filter: GrantFilter;
  /** The environments to choose from. */
  readonly environments: readonly EnvironmentChoice[];
  /** Called with the next filter, and how to keep it in the history. */
  readonly onChange: (next: GrantFilter, step: FilterStep) => void;
}

/** Where the list is filtered by. */
type WhereKind = "anywhere" | GrantWhere["kind"];

/** What the filter fields show, typed or chosen but not yet applied. */
interface Fields {
  /** Where: anywhere, or a level. */
  readonly whereKind: WhereKind;
  /** The environment chosen. */
  readonly environment: string | null;
  /** The dotted namespace typed. */
  readonly namespace: string;
  /** The table typed. */
  readonly table: string;
}

/**
 * The fields that show a filter.
 *
 * @param filter - The filter.
 * @returns The fields.
 */
function fieldsOf(filter: GrantFilter): Fields {
  const { where } = filter;
  return {
    whereKind: where?.kind ?? "anywhere",
    environment: where === null || where.kind === "INSTANCE" ? null : where.environment,
    namespace: where?.kind === "NAMESPACE" || where?.kind === "TABLE" ? where.namespace : "",
    table: where?.kind === "TABLE" ? where.table : "",
  };
}

/** The key of "any role or action" in the What menu: no role or action has it. */
const ANYTHING = "any";

/**
 * The What menu's key of a role or an action.
 *
 * @param what - The role or the action, or null.
 * @returns The key.
 */
function whatKey(what: GrantWhat | null): string {
  if (what === null) return ANYTHING;
  return what.kind === "role" ? `role:${what.role}` : `action:${what.action}`;
}

/**
 * The role or action a What menu key names.
 *
 * @param key - The key.
 * @returns The role or the action, or null for any.
 */
function whatOf(key: string): GrantWhat | null {
  const role = ROLES.find((candidate) => `role:${candidate}` === key);
  if (role !== undefined) return { kind: "role", role };
  const action = ACTIONS.find((candidate) => `action:${candidate}` === key);
  return action === undefined ? null : { kind: "action", action };
}

/**
 * Whether two filters ask for the same grants.
 *
 * @param a - One filter.
 * @param b - The other.
 * @returns True when they are the same.
 */
function same(a: GrantFilter, b: GrantFilter): boolean {
  return JSON.stringify([a.who, a.what, a.where]) === JSON.stringify([b.who, b.what, b.where]);
}

/** Properties of {@link WhatMenu}. */
interface WhatMenuProps {
  /** The role or action chosen, or null for any. */
  readonly value: GrantWhat | null;
  /** Called with the choice. */
  readonly onChange: (next: GrantWhat | null) => void;
}

/**
 * The What menu: any, then the roles and the actions in two sections.
 *
 * @param props - The choice and the handler.
 * @param props.value - The role or action chosen.
 * @param props.onChange - Called with the choice.
 * @returns The menu.
 */
function WhatMenu({ value, onChange }: WhatMenuProps): JSX.Element {
  const { t } = useTranslation();
  const anything = t("access.grants.filters.anything");
  return (
    <Select
      className="env-picker"
      value={whatKey(value)}
      onChange={(key) => {
        onChange(whatOf(String(key)));
      }}
    >
      <Label>{t("access.grants.filters.what")}</Label>
      <Button className="button">
        <SelectValue />
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom start">
        <ListBox className="menu">
          <ListBoxItem id={ANYTHING} className="menu-item" textValue={anything}>
            <span className="menu-check" aria-hidden="true" />
            {anything}
          </ListBoxItem>
          <ListBoxSection className="menu-section">
            <Header className="menu-header">{t("access.grants.filters.roles")}</Header>
            {ROLES.map((role) => (
              <ListBoxItem
                key={role}
                id={`role:${role}`}
                className="menu-item"
                textValue={roleName(t, role)}
              >
                <span className="menu-check" aria-hidden="true" />
                {roleName(t, role)}
              </ListBoxItem>
            ))}
          </ListBoxSection>
          <ListBoxSection className="menu-section">
            <Header className="menu-header">{t("access.grants.filters.actionsSection")}</Header>
            {ACTIONS.map((action) => (
              <ListBoxItem
                key={action}
                id={`action:${action}`}
                className="menu-item"
                textValue={actionWords(t, action)}
              >
                <span className="menu-check" aria-hidden="true" />
                {actionWords(t, action)}
              </ListBoxItem>
            ))}
          </ListBoxSection>
        </ListBox>
      </Popover>
    </Select>
  );
}

/**
 * Renders the three filters. A menu choice applies at once; a typed name, namespace or table on
 * Enter or when its field is left. A filter that is not complete yet (a person without a name,
 * a table without its namespace) keeps the list as it is.
 *
 * @param props - The filter, the environments and the handler.
 * @param props.filter - The filter the list shows.
 * @param props.environments - The environments to choose from.
 * @param props.onChange - Called with the next filter.
 * @returns The filters.
 */
export function GrantFilters({ filter, environments, onChange }: GrantFiltersProps): JSX.Element {
  const { t } = useTranslation();
  const [shown, setShown] = useState(() => fieldsOf(filter));
  const [shownFor, setShownFor] = useState(filter);
  // A filter from elsewhere (Clear filters, Back) sets the fields again.
  if (!same(shownFor, filter)) {
    setShownFor(filter);
    setShown(fieldsOf(filter));
  }
  const { whereKind, environment, namespace, table } = shown;
  const set = (change: Partial<Fields>): void => {
    setShown((previous) => ({ ...previous, ...change }));
  };
  const setNamespace = (value: string): void => {
    set({ namespace: value });
  };
  const setTable = (value: string): void => {
    set({ table: value });
  };

  const apply = (next: GrantFilter, step: FilterStep): void => {
    if (same(next, filter)) return;
    // The fields already show it: the filter that comes back must not set them again.
    setShownFor(next);
    onChange(next, step);
  };
  const applyWhere = (
    kind: WhereKind,
    chosen: { environment: string | null; namespace: string; table: string },
    step: FilterStep,
  ): void => {
    const space = chosen.namespace.trim();
    const object = chosen.table.trim();
    let next: GrantWhere | null | undefined;
    if (kind === "anywhere") next = null;
    else if (kind === "INSTANCE") next = { kind };
    else if (chosen.environment === null) next = undefined;
    else if (kind === "ENVIRONMENT") next = { kind, environment: chosen.environment };
    else if (space === "") next = undefined;
    else if (kind === "NAMESPACE")
      next = { kind, environment: chosen.environment, namespace: space };
    else if (object !== "") {
      next = { kind, environment: chosen.environment, namespace: space, table: object };
    }
    if (next !== undefined) apply({ ...filter, where: next }, step);
  };
  const chosen = { environment, namespace, table };

  return (
    <div className="access-filters" role="group" aria-label={t("access.grants.filters.label")}>
      <WhoFilter
        who={filter.who}
        onChange={(who, step) => {
          apply({ ...filter, who }, step);
        }}
      />
      <ChoiceMenu<WhereKind>
        className="env-picker"
        label={t("access.grants.filters.where")}
        selected={whereKind}
        choices={[
          { id: "anywhere", text: t("access.grants.filters.anywhere") },
          { id: "INSTANCE", text: t("access.grants.filters.instance") },
          { id: "ENVIRONMENT", text: t("access.grants.filters.environment") },
          { id: "NAMESPACE", text: t("access.grants.filters.namespace") },
          { id: "TABLE", text: t("access.grants.filters.table") },
        ]}
        onChoose={(kind) => {
          set({ whereKind: kind });
          applyWhere(kind, chosen, "push");
        }}
      />
      {whereKind !== "anywhere" && whereKind !== "INSTANCE" && (
        <EnvironmentPicker
          environments={environments}
          value={environment}
          label={t("access.grants.filters.environmentPicker")}
          onChange={(next) => {
            set({ environment: next });
            applyWhere(whereKind, { ...chosen, environment: next }, "push");
          }}
        />
      )}
      {(whereKind === "NAMESPACE" || whereKind === "TABLE") && (
        <AppliedField
          label={t("access.grants.filters.namespaceName")}
          value={namespace}
          onChange={setNamespace}
          help={t("access.grants.filters.namespaceHelp")}
          onApply={() => {
            applyWhere(whereKind, chosen, "replace");
          }}
        />
      )}
      {whereKind === "TABLE" && (
        <AppliedField
          label={t("access.grants.filters.tableName")}
          value={table}
          onChange={setTable}
          onApply={() => {
            applyWhere(whereKind, chosen, "replace");
          }}
        />
      )}
      <WhatMenu
        value={filter.what}
        onChange={(next) => {
          apply({ ...filter, what: next }, "push");
        }}
      />
      <p className="page-purpose access-filters-help">{t("access.grants.filters.whereHelp")}</p>
    </div>
  );
}
