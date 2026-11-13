/**
 * The Catalog's one search field, above the list.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { type CatalogView, Q_MAX, readView } from "./view";

// How long typing must pause before the list is read again; the same as Access → People.
const SEARCH_PAUSE_MS = 250;

/**
 * The text as the address will hold it: cut, trimmed and without NUL characters.
 *
 * @param words - What was typed.
 * @returns The text a view would read back.
 */
function asInAddress(words: string): string {
  return readView(new URLSearchParams({ q: words })).q;
}

/** Properties of {@link CatalogSearch}. */
export interface CatalogSearchProps {
  /** The view whose search text the field shows. */
  readonly view: CatalogView;
  /** Called with the text once typing pauses. */
  readonly onSearch: (q: string) => void;
}

/**
 * Renders the search field. The text is the field's own while typing; once typing pauses it is
 * handed on, and when the address holds other text (Back, a link, widening) the field follows.
 *
 * @param props - The view and the handler.
 * @param props.view - The view whose search text the field shows.
 * @param props.onSearch - Called with the text once typing pauses.
 * @returns The field.
 */
export function CatalogSearch({ view, onSearch }: CatalogSearchProps): JSX.Element {
  const { t } = useTranslation();
  const [words, setWords] = useState(view.q);
  // What the field last handed on, as the address holds it, so the address catching up with a
  // keystroke never puts older text back.
  const sent = useRef(view.q);

  useEffect(() => {
    if (view.q !== sent.current) {
      sent.current = view.q;
      setWords(view.q);
    }
  }, [view.q]);

  useEffect(() => {
    const wanted = asInAddress(words);
    if (wanted === sent.current) {
      return undefined;
    }
    const timer = setTimeout(() => {
      sent.current = wanted;
      onSearch(wanted);
    }, SEARCH_PAUSE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [words, onSearch]);

  return (
    <SearchField className="field catalog-search" value={words} onChange={setWords}>
      <Label className="visually-hidden">{t("catalog.search")}</Label>
      <Input placeholder={t("catalog.search")} maxLength={Q_MAX} />
    </SearchField>
  );
}
