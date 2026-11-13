/**
 * The small pieces the catalog list and a table's page write the same way.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";

import type { OwnerOut } from "../api";
import { Avatar } from "../profile/Avatar";
import { useWhen } from "../shell/useWhen";
import { formatRows, relativeTime } from "./format";

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

/**
 * What a cell shows where nothing is known: a dash for the eye and the words for a screen reader.
 *
 * @param props - The words.
 * @param props.words - What is not known, in words.
 * @returns The dash and the words.
 */
export function Unknown({ words }: { readonly words: string }): JSX.Element {
  return (
    <>
      <span aria-hidden="true">–</span>
      <span className="visually-hidden">{words}</span>
    </>
  );
}

/** Properties of {@link When}. */
export interface WhenProps {
  /** The time, ISO 8601; null when it never happened. */
  readonly iso: string | null;
  /** What to say when it never happened, such as "Never read". */
  readonly never: string;
}

/**
 * A time as how long ago it was, with the absolute time in its `title` and in text a screen
 * reader hears; the words in `never` without a time.
 *
 * @param props - The time and the words.
 * @param props.iso - The time, or null.
 * @param props.never - What to say without one.
 * @returns The time.
 */
export function When({ iso, never }: WhenProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const when = useWhen();
  // The present is read once per mount, so a render never depends on the clock.
  const [now] = useState(Date.now);
  if (iso === null) {
    return <Unknown words={never} />;
  }
  const absolute = when(iso);
  return (
    <>
      <time dateTime={iso} title={absolute}>
        {relativeTime(iso, now, i18n.language) ?? t("catalog.justNow")}
      </time>
      <span className="visually-hidden"> {absolute}</span>
    </>
  );
}

/** Properties of {@link RowCount}. */
export interface RowCountProps {
  /** The rows, or null when not known. */
  readonly value: number | null;
}

/**
 * A row count: short from a million with the exact count in a `title` and for a screen reader.
 *
 * @param props - The count.
 * @param props.value - The rows, or null.
 * @returns The count.
 */
export function RowCount({ value }: RowCountProps): JSX.Element {
  const { t, i18n } = useTranslation();
  if (value === null) {
    return <Unknown words={t("catalog.notKnown")} />;
  }
  const short = formatRows(value, i18n.language);
  const exact = new Intl.NumberFormat(i18n.language).format(value);
  return short === exact ? (
    <>{short}</>
  ) : (
    <>
      <span aria-hidden="true" title={exact}>
        {short}
      </span>
      <span className="visually-hidden">{exact}</span>
    </>
  );
}

/** Properties of {@link OwnerName}. */
export interface OwnerNameProps {
  /** The owner, or null when the table has none. */
  readonly owner: OwnerOut | null;
}

/**
 * An owner as a circle and a name, with "Service account" beside a service account; "No owner"
 * in words without one.
 *
 * @param props - The owner.
 * @param props.owner - The owner, or null.
 * @returns The owner.
 */
export function OwnerName({ owner }: OwnerNameProps): JSX.Element {
  const { t } = useTranslation();
  if (owner === null) {
    return <Unknown words={t("catalog.noOwner")} />;
  }
  return (
    <div className="person-cell">
      <Avatar name={owner.name} displayName={null} etag={null} size={24} />
      <span>{owner.name}</span>
      {owner.kind === "SERVICE_ACCOUNT" && (
        <span className="second">{t("catalog.serviceAccount")}</span>
      )}
    </div>
  );
}

/** Properties of {@link ReadFailed}. */
export interface ReadFailedProps {
  /** The cause and the next step, as the server or the interface words them. */
  readonly message: string;
  /** Reads again. */
  readonly onRetry: () => void;
}

/**
 * The sentence that says why something could not be read, with Try again beneath it.
 *
 * @param props - The sentence and the way to read again.
 * @param props.message - The cause and next step.
 * @param props.onRetry - Called when Try again is pressed.
 * @returns The sentence and the button.
 */
export function ReadFailed({ message, onRetry }: ReadFailedProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="read-failed">
      <p role="alert">{message}</p>
      <button type="button" className="button" onClick={onRetry}>
        {t("table.retry")}
      </button>
    </div>
  );
}
