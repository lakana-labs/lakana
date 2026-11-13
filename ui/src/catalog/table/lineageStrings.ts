/**
 * The Lineage tab's strings, registered with i18next when the module runs, and the words for a
 * connection's kind.
 *
 * @remarks
 * They load with the Lineage tab's chunk, not with the table's page or the first visit. Each
 * module of the tab that writes one of them imports this module, so they are in place before
 * the tab renders and no string shows untranslated.
 * @packageDocumentation
 */
import i18next, { type TFunction } from "i18next";

import strings from "./lineage.en.json";

i18next.addResourceBundle("en", "translation", { table: { lineage: strings } }, true, false);

/** Every kind of connection the server can send (`connections/kinds.py`), each with its word. */
const KINDS: ReadonlySet<string> = new Set([
  "postgresql",
  "mariadb",
  "sqlite-file",
  "http",
  "s3",
  "gcs",
  "azure",
  "directory",
  "tcp",
]);

/**
 * How to name a connection's kind: a word for a kind the interface knows and for a connection
 * the environment does not have (no kind), else the server's value as it came, which the caller
 * shows through the visible-marks display.
 *
 * @param t - The translation function.
 * @param kind - The connection's kind, or null or undefined when the environment has none.
 * @returns The word, or the raw value.
 */
export function kindName(
  t: TFunction,
  kind: string | null | undefined,
): { readonly word: string } | { readonly raw: string } {
  const value = kind ?? null;
  if (value === null) {
    return { word: t("table.lineage.kinds.none") };
  }
  return KINDS.has(value) ? { word: t(`table.lineage.kinds.${value}`) } : { raw: value };
}
