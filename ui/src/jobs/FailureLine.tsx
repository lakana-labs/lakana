/**
 * Why a task failed, was skipped or was cancelled, in the server's two sentences.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { FailureOut } from "../api/jobs";
import { DOCS_URL } from "../shell/docs";
import { VisibleText } from "../shell/VisibleText";
import "./jobStrings";
import { failureCode } from "./status";

/** The reference's table of the codes, on the documentation site. */
const CODES_URL = `${DOCS_URL}reference/why-did-it-fail/#the-codes`;

/** Properties of {@link FailureLine}. */
export interface FailureLineProps {
  /** The failure the server sent for the task. */
  readonly failure: FailureOut;
  /** The short form of a row of a list, "code: cause — next step"; no by default. */
  readonly inline?: boolean;
}

/**
 * The failure as one line: the code, which links to the reference of the codes, the cause and the
 * next step; in a row of a list, the short form "code: cause — next step". A code the reference does not document (`unknown` on an old row, or one a newer
 * server sends) shows as `internal`, as the reference says. The two sentences are the same as
 * Home's and the command line's, with hidden characters shown as marks.
 *
 * @param props - The failure and its form.
 * @param props.failure - What the server sent.
 * @param props.inline - The short form of a row of a list.
 * @returns The line.
 */
export function FailureLine({ failure, inline = false }: FailureLineProps): JSX.Element {
  const { t } = useTranslation();
  const code = (
    <a className="jobs-code" href={CODES_URL} target="_blank" rel="noopener noreferrer">
      {failureCode(failure.code)}
      <span className="visually-hidden"> {t("home.newTab")}</span>
    </a>
  );
  if (inline) {
    return (
      <span className="jobs-failure jobs-failure-inline">
        {code}: <VisibleText text={failure.cause} /> — <VisibleText text={failure.next_step} />
      </span>
    );
  }
  return (
    <p className="jobs-failure">
      {code}
      <span aria-hidden="true"> · </span>
      <span className="jobs-failure-label">{t("jobs.failure.cause")} </span>
      <VisibleText text={failure.cause} />{" "}
      <span className="jobs-failure-label">{t("jobs.failure.nextStep")} </span>
      <VisibleText text={failure.next_step} />
    </p>
  );
}
