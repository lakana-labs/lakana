/**
 * The controls of a job: Run now, and Pause schedule or Resume schedule. The job page shows them
 * beside its heading; a list of jobs can show them in its rows.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";

import { type JobOut, type JobStartOut, pauseJob, resumeJob, startJob } from "../api/jobs";
import { AskDialog, ControlButton } from "./ControlNote";
import "./jobStrings";

/** Properties of {@link RunNowButton}. */
export interface RunNowButtonProps {
  /** The job's environment. */
  readonly environment: string;
  /** The job. */
  readonly job: JobOut;
  /** Whether it is the page's one primary action; no by default (a row of a list). */
  readonly primary?: boolean;
  /** Told what the start did: the queued run, or the waiting start. */
  readonly onStarted: (result: JobStartOut) => void;
}

/**
 * Run now: starts a run of the job at once, offered only when the server lists `run` among the
 * job's actions. The focus stays on the button; a refusal shows the server's sentence under it.
 *
 * @param props - The environment, the job and what to do with the start.
 * @param props.environment - The job's environment.
 * @param props.job - The job.
 * @param props.primary - Whether it is the page's primary action.
 * @param props.onStarted - Told what the start did.
 * @returns The button, or nothing.
 */
export function RunNowButton({
  environment,
  job,
  primary = false,
  onStarted,
}: RunNowButtonProps): JSX.Element | null {
  const { t } = useTranslation();
  if (!job.actions.includes("run")) {
    return null;
  }
  return (
    <ControlButton
      label={t("jobs.runNow")}
      primary={primary}
      act={async () => {
        onStarted(await startJob(environment, job.repository, job.name));
      }}
    />
  );
}

/** Properties of {@link ScheduleButton}. */
export interface ScheduleButtonProps {
  /** The job's environment. */
  readonly environment: string;
  /** The job. */
  readonly job: JobOut;
  /** Told the job once its schedule was paused or resumed. */
  readonly onChanged: (job: JobOut) => void;
}

/**
 * Pause schedule, which asks first (a run that is running finishes), or Resume schedule, which
 * acts at once. Nothing for a job without a schedule here, or when the server does not list the
 * action. One button for both, so the focus stays on it when the schedule changes.
 *
 * @param props - The environment, the job and what to do with the changed job.
 * @param props.environment - The job's environment.
 * @param props.job - The job.
 * @param props.onChanged - Told the changed job.
 * @returns The button, or nothing.
 */
export function ScheduleButton({
  environment,
  job,
  onChanged,
}: ScheduleButtonProps): JSX.Element | null {
  const { t } = useTranslation();
  const [asking, setAsking] = useState(false);
  const scheduled = job.triggers.some((trigger) => trigger.kind === "schedule");
  if (!scheduled || !job.actions.includes(job.paused ? "resume" : "pause")) {
    return null;
  }
  return (
    <>
      {job.paused ? (
        <ControlButton
          label={t("jobs.schedule.resume")}
          act={async () => {
            onChanged(await resumeJob(environment, job.repository, job.name));
          }}
        />
      ) : (
        <ControlButton
          label={t("jobs.schedule.pause")}
          ask={() => {
            setAsking(true);
          }}
        />
      )}
      {asking && (
        <AskDialog
          title={t("jobs.schedule.pauseTitle", { job: job.name })}
          text={t("jobs.schedule.pauseText")}
          label={t("jobs.schedule.pause")}
          act={async () => {
            onChanged(await pauseJob(environment, job.repository, job.name));
          }}
          onClose={() => {
            setAsking(false);
          }}
        />
      )}
    </>
  );
}
