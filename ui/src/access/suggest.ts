/**
 * The searches the name pickers of Access ask: principals, and the tables of an environment.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import { suggestPrincipals, suggestTables } from "../api/suggestions";
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
export function principalSearch(t: TFunction): NameSearch<null> {
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
