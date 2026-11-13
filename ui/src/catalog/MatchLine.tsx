/**
 * The quiet second line of a search hit: which part of the table matched, with the match marked.
 *
 * @packageDocumentation
 */
import { Fragment, type JSX } from "react";
import { useTranslation } from "react-i18next";

import type { TableMatchOut } from "../api";

/** Properties of {@link MatchLine}. */
export interface MatchLineProps {
  /** Why the row matches. */
  readonly match: TableMatchOut;
  /** The search text. */
  readonly q: string;
}

/**
 * Text with the first occurrence of the search wrapped in `<mark>`, whatever its case.
 *
 * @param props - The text and the search text.
 * @param props.text - The text.
 * @param props.q - The search text.
 * @returns The text, with the match marked.
 */
function Marked({ text, q }: { readonly text: string; readonly q: string }): JSX.Element {
  const at = q === "" ? -1 : text.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) {
    return <>{text}</>;
  }
  return (
    <>
      {text.slice(0, at)}
      <mark className="match">{text.slice(at, at + q.length)}</mark>
      {text.slice(at + q.length)}
    </>
  );
}

/**
 * Whether a hit says more than the table's name, which the normal second line already shows.
 *
 * @param match - The hit.
 * @returns True for a column, a label, a description, a grain or a measure.
 */
export function showsMatch(match: TableMatchOut): boolean {
  return match.kind !== "table" || match.matched !== "name";
}

/**
 * Renders the line of a hit.
 *
 * @param props - The hit and the search text.
 * @param props.match - Why the row matches.
 * @param props.q - The search text.
 * @returns The line.
 */
export function MatchLine({ match, q }: MatchLineProps): JSX.Element {
  const { t } = useTranslation();
  const field = t(`catalog.match.${match.matched}`, { defaultValue: match.matched });
  const parts: JSX.Element[] = [];
  if (match.kind === "column") {
    parts.push(
      <span key="column">
        {t("catalog.match.column")} <Marked text={match.column ?? ""} q={q} />
      </span>,
    );
    if (match.column_type !== null) {
      parts.push(<span key="type">{match.column_type}</span>);
    }
    if (match.matched !== "name" && match.snippet !== null) {
      parts.push(<Marked key="snippet" text={match.snippet} q={q} />);
    }
  } else {
    parts.push(
      <span key="field">{field}</span>,
      <Marked key="snippet" text={match.snippet ?? ""} q={q} />,
    );
  }
  if (match.translated && match.language !== null) {
    parts.push(<span key="language">{match.language}</span>);
  }
  return (
    <span className="match-line">
      {parts.map((part, index) => (
        <Fragment key={part.key}>
          {index > 0 && " · "}
          {part}
        </Fragment>
      ))}
    </span>
  );
}
