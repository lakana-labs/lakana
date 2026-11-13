/**
 * Why a person may or may not do something with a table, in sentences, and the levels and CLI
 * commands of the Access tab's grants.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { AccessOut, PermissionOut, ScopeOut, TableOut, ViaOut } from "../../api";
import "./tableStrings";

/**
 * The object a grant is on, in words.
 *
 * @param scope - The object.
 * @param t - The translation function.
 * @returns The words, such as "namespace gold in prod".
 */
function scopeWords(scope: ScopeOut, t: TFunction): string {
  return t(`table.access.scope.${scope.kind}`, {
    environment: scope.environment,
    namespace: scope.namespace,
    table: scope.table,
  });
}

/**
 * One way a permission reaches the person, as a sentence.
 *
 * @param via - The way.
 * @param action - The action the permission is for.
 * @param t - The translation function.
 * @returns The sentence.
 */
function viaSentence(via: ViaOut, action: string, t: TFunction): string {
  const words = {
    scope: scopeWords(via.scope, t),
    action,
    group: via.group,
    role: via.role === null ? null : t(`table.access.roles.${via.role}`),
  };
  const what = via.role === null ? "Grant" : "Role";
  return via.kind === "group"
    ? t(`table.access.via.group${what}`, words)
    : t(`table.access.via.${what.toLowerCase()}`, words);
}

/**
 * Why a permission is given, each way it reaches the person once; for a refusal the server's
 * reason, else the next step.
 *
 * @param permission - The permission.
 * @param t - The translation function.
 * @returns The sentences.
 */
export function allowedBecause(permission: PermissionOut, t: TFunction): string {
  if (!permission.allowed) {
    return permission.reason ?? t("table.access.askAdmin");
  }
  const sentences = new Set(permission.via.map((via) => viaSentence(via, permission.action, t)));
  return sentences.size === 0 ? t("table.access.allowedPlain") : [...sentences].join(" ");
}

/**
 * Whether the person may edit the table's texts, and why: its owner may, and so may whoever may
 * drop it; anyone else is sent to the owner, or to an administrator for a table without one.
 *
 * @param table - The table.
 * @param me - The person.
 * @param drop - The person's permission to drop the table, if it was read.
 * @param t - The translation function.
 * @returns The answer and its words.
 */
export function editTextsAnswer(
  table: TableOut,
  me: AccessOut,
  drop: PermissionOut | undefined,
  t: TFunction,
): { allowed: boolean; words: string } {
  if (table.owner?.name === me.name) {
    return { allowed: true, words: t("table.access.owner") };
  }
  if (drop?.allowed === true) {
    return { allowed: true, words: `${t("table.access.byDrop")} ${allowedBecause(drop, t)}` };
  }
  return {
    allowed: false,
    words:
      table.owner === null
        ? t("table.access.editRefusedNoOwner")
        : t("table.access.editRefused", { owner: table.owner.name }),
  };
}

/**
 * The table's namespace and each one above it: grants on any of them reach the table.
 *
 * @param namespace - The dotted namespace, such as `gold.sales`.
 * @returns The namespaces, the nearest first: `gold.sales`, then `gold`.
 */
export function namespaceLevels(namespace: string): readonly string[] {
  const parts = namespace.split(".");
  return parts.map((_, at) => parts.slice(0, parts.length - at).join("."));
}

/**
 * The CLI command that lists every grant on one level, for a level with more than one page.
 *
 * @param environment - The environment.
 * @param namespace - The dotted namespace, or null for the environment.
 * @param table - The table, or null for a namespace or the environment.
 * @returns The command.
 */
export function grantListCommand(
  environment: string,
  namespace: string | null,
  table: string | null,
): string {
  const parts = ["lakana grant list", `--env ${environment}`];
  if (namespace !== null) {
    parts.push(`--ns ${namespace}`);
  }
  if (table !== null) {
    parts.push(`--table ${table}`);
  }
  return parts.join(" ");
}
