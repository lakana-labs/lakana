/**
 * Which language an edit of a table's or column's texts is written in, and how to name it.
 *
 * @packageDocumentation
 */
import type { TableOut } from "../../api";

/** The language of an edit and what to tell the person about it. */
export interface EditLanguage {
  /** The language tag the edit is written in. */
  readonly lang: string;
  /** Whether the table has more than its main language, so the person must be told which one. */
  readonly named: boolean;
  /** Whether the page shows the main language because it has no text in the reader's. */
  readonly fallback: boolean;
}

/**
 * Chooses the language an edit is written in: the language of the text the page shows, so that
 * an edit replaces what the person reads. Without a text, the reader's language when the table
 * has texts in it, else the table's main language.
 *
 * @param table - The table.
 * @param texts - The text being edited.
 * @param texts.description_language - The language of the description shown; null without one.
 * @returns The language, whether to name it, and whether the page fell back to the main language.
 */
export function editLanguage(
  table: TableOut,
  texts: { readonly description_language: string | null },
): EditLanguage {
  const translated =
    table.language === table.main_language ||
    table.translations.some((one) => one.language === table.language);
  const lang = texts.description_language ?? (translated ? table.language : table.main_language);
  return { lang, named: table.translations.length > 0, fallback: lang !== table.language };
}

/**
 * Names a language in the interface's language.
 *
 * @param tag - The language tag, such as `de`.
 * @param uiLanguage - The interface's language.
 * @returns The name, such as "German", or the tag itself when the browser does not know it.
 */
export function languageName(tag: string, uiLanguage: string): string {
  try {
    // With the default fallback, `of` answers the tag itself for a language it has no name for.
    return String(new Intl.DisplayNames([uiLanguage], { type: "language" }).of(tag));
  } catch {
    return tag;
  }
}
