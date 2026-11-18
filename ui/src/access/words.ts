/**
 * Access's facts in words: an action, a role and what a grant is on.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { Action, Role } from "../api/grants";
import type { ScopeOut } from "../api/table";
import "./accessStrings";

/** What a grant or a permission is on, as the server names it. */
export interface WhereFacts {
  /** The level. */
  readonly kind: ScopeOut["kind"];
  /** The environment, if any. */
  readonly environment: string | null;
  /** The dotted namespace, if any. */
  readonly namespace: string | null;
  /** The table, if any. */
  readonly table: string | null;
}

/**
 * An action in plain words, such as "Read tables".
 *
 * @param t - The translation function.
 * @param action - The action.
 * @returns The words.
 */
export function actionWords(t: TFunction, action: Action): string {
  return t(`access.actions.${action}`);
}

/**
 * A role's name as the server writes it, such as "ANALYST".
 *
 * @param t - The translation function.
 * @param role - The role.
 * @returns The name.
 */
export function roleName(t: TFunction, role: Role): string {
  return t(`access.roles.${role}.name`);
}

/**
 * What a role gives on tables and namespaces, such as "Read tables and run queries".
 *
 * @param t - The translation function.
 * @param role - The role.
 * @returns The summary.
 */
export function roleSummary(t: TFunction, role: Role): string {
  return t(`access.roles.${role}.summary`);
}

/**
 * Where a grant is, such as "dev › sales › orders", or "The whole instance"; every name is
 * marked by `t()`.
 *
 * @param t - The translation function.
 * @param on - The level and the object's names.
 * @returns The words.
 */
export function whereText(t: TFunction, on: WhereFacts): string {
  return t(`access.where.${on.kind}`, {
    environment: on.environment ?? "",
    namespace: on.namespace ?? "",
    table: on.table ?? "",
  });
}
