/**
 * The small pieces the catalog list and a table's page write the same way.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

/**
 * The word for what a table is: its modelling kind (`Fact`) when it has one, else its entry kind
 * (`Table`, `View`).
 *
 * @param t - The translation function.
 * @param kind - The entry kind, such as `TABLE`.
 * @param modelKind - The modelling kind, such as `fact`, or null.
 * @returns The word.
 */
export function kindWord(t: TFunction, kind: string, modelKind: string | null): string {
  return modelKind === null
    ? t(`catalog.kind.${kind}`)
    : t(`catalog.modelKind.${modelKind}`, { defaultValue: t(`catalog.kind.${kind}`) });
}
