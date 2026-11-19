/**
 * What Access's lists of facts share: the Git mark of a row an access file manages, and Clear
 * filters under a filtered list that came back empty.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { CursorPages } from "../catalog/views/useCursorPages";
import "./accessStrings";
import { GitMark } from "./GitMark";

/** Properties of {@link ManagedMark}. */
export interface ManagedMarkProps {
  /** The name of the file that manages the row's scope. */
  readonly file: string;
}

/**
 * Renders "⌂ Git" with "Managed by FILE" as its second line, in place of a row's change.
 *
 * @param props - The file.
 * @param props.file - The file's name.
 * @returns The mark.
 */
export function ManagedMark({ file }: ManagedMarkProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <GitMark />
      <span className="second">{t("access.managed.markOf", { file })}</span>
    </>
  );
}

/** Properties of {@link ClearFilters}. */
export interface ClearFiltersProps {
  /** The list's pages. */
  readonly pages: CursorPages<unknown>;
  /** Whether a filter is set. */
  readonly filtered: boolean;
  /** The button's words. */
  readonly label: string;
  /** Goes back to the whole list. */
  readonly onClear: () => void;
}

/**
 * Renders Clear filters once a filtered list has been read and holds nothing.
 *
 * @param props - The pages, whether filtered, the words and the handler.
 * @param props.pages - The list's pages.
 * @param props.filtered - Whether a filter is set.
 * @param props.label - The button's words.
 * @param props.onClear - Goes back to the whole list.
 * @returns The button, or nothing.
 */
export function ClearFilters({ pages, filtered, label, onClear }: ClearFiltersProps): JSX.Element {
  const shown = filtered && pages.items?.length === 0 && !pages.busy;
  return (
    <>
      {shown && (
        <p>
          <Button className="button" onPress={onClear}>
            {label}
          </Button>
        </p>
      )}
    </>
  );
}
