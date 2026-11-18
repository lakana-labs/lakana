/**
 * The mark of what a file in Git defines: "⌂ Git".
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import "./accessStrings";

/**
 * Renders the mark; the glyph is decoration, the word says it.
 *
 * @returns The mark.
 */
export function GitMark(): JSX.Element {
  const { t } = useTranslation();
  return (
    <span className="git-mark">
      <span aria-hidden="true">⌂</span> {t("access.git")}
    </span>
  );
}
