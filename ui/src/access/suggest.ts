/**
 * The searches the name pickers of Access ask: principals, and the tables of an environment.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { suggestGroups, suggestPrincipals, suggestTables } from "../api/suggestions";
import { useSettingsViewer } from "../settings/viewer";
import { type NameSearch, PICKER_LIMIT } from "../shell/NamePicker";
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
    return {
      choices: page.items.map((table) => ({
        key: JSON.stringify([table.namespace, table.name]),
        name: table.name,
        detail: table.namespace,
        data: { namespace: table.namespace, name: table.name },
      })),
      more: page.next_cursor !== null,
    };
  };
}
