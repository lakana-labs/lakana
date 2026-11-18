/**
 * The Jobs list's search field.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import "./jobStrings";

/** Properties of {@link JobsSearch}. */
export interface JobsSearchProps {
  /** The words in the address. */
  readonly query: string;
  /** Called with the words at each keystroke. */
  readonly onSearch: (query: string) => void;
}

/**
 * The field "Search jobs". It keeps the words being typed itself and hands each change on at
 * once, so the list narrows as the person types; words the address brings from elsewhere (Back,
 * Forward) replace them.
 *
 * @param props - The words and what to do with a change.
 * @param props.query - The words in the address.
 * @param props.onSearch - Called with the words.
 * @returns The field.
 */
export function JobsSearch({ query, onSearch }: JobsSearchProps): JSX.Element {
  const { t } = useTranslation();
  const [words, setWords] = useState(query);
  // What the field last handed on, so the address catching up with a keystroke never puts older
  // words back.
  const sent = useRef(query);
  useEffect(() => {
    if (query !== sent.current) {
      sent.current = query;
      setWords(query);
    }
  }, [query]);
  return (
    <SearchField
      className="field jobs-search"
      value={words}
      onChange={(next) => {
        setWords(next);
        sent.current = next;
        onSearch(next);
      }}
    >
      <Label className="visually-hidden">{t("jobs.list.search")}</Label>
      <Input placeholder={t("jobs.list.search")} />
    </SearchField>
  );
}
