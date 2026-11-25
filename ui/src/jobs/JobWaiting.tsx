/**
 * A job whose start waits: the "Waiting" pill, with what it waits for on hover and on focus.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { JobOut } from "../api/jobs";
import { markedText, VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import "./jobStrings";

/** The states of a job whose start waits: for the active run, or for the scheduler. */
const WAITING_STATES: readonly string[] = ["waiting", "waiting to start"];

/**
 * Whether a start of the job waits: for the run that holds its project, or, once that has
 * ended, for the scheduler to begin it.
 *
 * @param job - The job.
 * @param job.state - Its state.
 * @returns True while a start waits.
 */
export function isWaiting(job: Pick<JobOut, "state">): boolean {
  return WAITING_STATES.includes(job.state);
}

/** Properties of {@link WaitingLabel}. */
export interface WaitingLabelProps {
  /** The job, whose start waits. */
  readonly job: Pick<JobOut, "waiting_for">;
  /** Whether the sentence of what it waits for shows in the shared tooltip (in a list). */
  readonly tip?: boolean;
}

/**
 * The "Waiting" pill: the queued glyph (an empty ring) and the word. In a list its sentence (what
 * the start waits for) shows in the shell's one tooltip on hover and on focus (the tooltip makes
 * it a Tab stop), and a screen reader reads it whole.
 *
 * @param props - The job and whether to show the tooltip.
 * @param props.job - The job.
 * @param props.tip - Whether the sentence shows in the tooltip.
 * @returns The pill.
 */
export function WaitingLabel({ job, tip = false }: WaitingLabelProps): JSX.Element {
  const { t } = useTranslation();
  const label = <StatusLabel kind="queued" label={t("jobs.waiting.label")} />;
  const message = job.waiting_for?.message ?? "";
  if (!tip || message === "") {
    return label;
  }
  return (
    <span className="jobs-waits" data-tip={markedText(message)}>
      {label}
      <span className="visually-hidden">
        {" "}
        <VisibleText text={message} />
      </span>
    </span>
  );
}
