/**
 * The searches the name pickers of Access ask: principals, groups, and the tables of an
 * environment.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { TableListPageOut } from "../api/catalog";
import { suggestGroups, suggestPrincipals, suggestTables } from "../api/suggestions";
import { useSettingsViewer } from "../settings/viewer";
import { type NameMatches, type NameSearch, PICKER_LIMIT } from "../shell/NamePicker";
import "./accessStrings";

/** A suggested table: what Add filter or mask fills in when it is chosen. */
export interface TableChoice {
  /** The table's dotted namespace. */
  readonly namespace: string;
  /** The table's name. */
  readonly name: string;
}

/**
 * The principals whose name contains the text, each with its kind and, when it no longer signs
 * in, the word for that. Only for a person who may list principals.
 *
 * @param t - The translator.
 * @returns The search.
 */
function principalSearch(t: TFunction): NameSearch<null> {
  return async (text, signal) => {
    const page = await suggestPrincipals(text, PICKER_LIMIT, signal);
    return {
      choices: page.people.map((person) => {
        const kind = t(person.type === "USER" ? "access.people.person" : "access.people.service");
        return {
          key: person.name,
          name: person.name,
          detail: person.is_active ? kind : `${kind} · ${t("access.people.deactivated")}`,
          data: null,
        };
      }),
      more: page.more,
    };
  };
}

/**
 * The principal search for a picker, for an administrator only: only one may list principals,
 * and anyone else would be refused at every keystroke (an `AUTHORIZATION_DENIED` each time).
 *
 * @returns The search, or null for a plain field.
 */
export function usePrincipalSearch(): NameSearch<null> | null {
  const { t } = useTranslation();
  const admin = useSettingsViewer().admin;
  return useMemo(() => (admin ? principalSearch(t) : null), [admin, t]);
}

/**
 * The group search for a picker, for an administrator only, who alone may list groups.
 *
 * @returns The search, or null for a plain field.
 */
export function useGroupSearch(): NameSearch<null> | null {
  const admin = useSettingsViewer().admin;
  return useMemo(() => (admin ? groupSearch() : null), [admin]);
}

/** The search of a field that names a principal or a group, and its sentence for no match. */
export interface WhoSearch {
  /** The search, or null for a plain field. */
  readonly search: NameSearch<null> | null;
  /** The sentence for a text nothing matches. */
  readonly noMatch: (text: string) => string;
}

/**
 * The search of a field that names a principal or a group, by the choice of who: Add grant's
 * and Add filter or mask's Name, and the lists' Who filter. Each search is the administrator's
 * only, so anyone else gets the plain field. A change of who changes the search, which drops the
 * other kind's suggestions.
 *
 * @param who - A person or service account, or a group.
 * @returns The search and its sentence for no match.
 */
export function useWhoSearch(who: "principal" | "group"): WhoSearch {
  const { t } = useTranslation();
  const principals = usePrincipalSearch();
  const groups = useGroupSearch();
  return who === "group"
    ? { search: groups, noMatch: (text) => t("picker.noGroup", { name: text }) }
    : { search: principals, noMatch: (text) => t("picker.noPrincipal", { name: text }) };
}

/**
 * The groups whose name contains the text. Only for an administrator.
 *
 * @returns The search.
 */
function groupSearch(): NameSearch<null> {
  return async (text, signal) => {
    const page = await suggestGroups(text, PICKER_LIMIT, signal);
    return {
      choices: page.names.map((name) => ({ key: name, name, detail: null, data: null })),
      more: page.more,
    };
  };
}

/**
 * The tables of an environment the person may read whose own name contains the text, each
 * with its namespace.
 *
 * @param environment - The environment.
 * @returns The search.
 */
export function tableSearch(environment: string): NameSearch<TableChoice> {
  return async (text, signal) => {
    const page = await suggestTables(environment, text, PICKER_LIMIT, signal);
    return tableMatches(page, (table) => ({ name: table.name, detail: table.namespace }));
  };
}

/**
 * The tables of an environment the person may read, for a field that takes `namespace.table`
 * (Row filters & masks' Table filter): a text with a dot looks for the part after its last dot
 * in the namespace before it and the ones beneath it, `gold.` for every table there; one
 * without a dot looks in the table's own name. Each suggestion is the dotted name.
 *
 * @param environment - The environment.
 * @returns The search.
 */
export function dottedTableSearch(environment: string): NameSearch<TableChoice> {
  return async (text, signal) => {
    const dot = text.lastIndexOf(".");
    const page =
      dot > 0
        ? await suggestTables(
            environment,
            text.slice(dot + 1),
            PICKER_LIMIT,
            signal,
            text.slice(0, dot),
          )
        : await suggestTables(environment, text, PICKER_LIMIT, signal);
    return tableMatches(page, (table) => ({
      name: `${table.namespace}.${table.name}`,
      detail: null,
    }));
  };
}

/**
 * A page of tables as suggestions.
 *
 * @param page - The page.
 * @param shown - The name the field takes and the quiet word beside it, for a table.
 * @returns The suggestions.
 */
function tableMatches(
  page: TableListPageOut,
  shown: (table: TableChoice) => { readonly name: string; readonly detail: string | null },
): NameMatches<TableChoice> {
  return {
    choices: page.items.map((table) => ({
      key: JSON.stringify([table.namespace, table.name]),
      ...shown(table),
      data: { namespace: table.namespace, name: table.name },
    })),
    more: page.next_cursor !== null,
  };
}
