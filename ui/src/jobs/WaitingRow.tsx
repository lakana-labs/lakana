/**
 * The starts that wait, as rows of the Runs tab: a start of a job that waits for the run holding
 * its project is no run yet, but it is the next one, so the list shows it above the runs.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { JobOut } from "../api/jobs";
import { openFromCell, Unknown } from "../catalog/cells";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./jobStrings";
import { isWaiting, WaitingLabel } from "./JobWaiting";
import { jobPath, jobRunPath } from "./paths";
import { type Period, sinceOf } from "./period";
import { readJobsOf } from "./readJobsOf";
import "./runStrings";

/** The filters of the Runs tab that a waiting start is held against. */
export interface WaitingFilter {
  /** The environments listed. */
  readonly environments: readonly string[];
  /** The State choice: a waiting start shows under `all` and `queued,running` only. */
  readonly status: string;
  /** The Trigger choice; `all` is any. */
  readonly trigger: string;
  /** The exact job name; empty is any. */
  readonly job: string;
  /** The Period: a start asked for before it is left out. */
  readonly period: Period;
}

/**
 * The waiting starts among some jobs that match the Runs tab's filters, newest request first. A
 * start whose time the server could not read is kept, as it surely waits now.
 *
 * @param jobs - The jobs read.
 * @param filter - The filters.
 * @returns The jobs whose start waits.
 */
function waitingStarts(
  jobs: readonly JobOut[],
  filter: Omit<WaitingFilter, "environments">,
): JobOut[] {
  if (!showsWaiting(filter.status)) {
    return [];
  }
  const since = sinceOf(filter.period);
  return jobs
    .filter(
      (job) =>
        isWaiting(job) &&
        (filter.trigger === "all" || job.waiting_trigger === filter.trigger) &&
        (filter.job === "" || job.name === filter.job) &&
        (since === null || job.waiting_since === null || job.waiting_since >= since),
    )
    .sort((a, b) => (b.waiting_since ?? "").localeCompare(a.waiting_since ?? ""));
}

/**
 * How long one environment's jobs may take before the Runs tab goes on without its waiting
 * starts. The first page of runs waits for these reads, so that runs and waiting starts are one
 * moment's answer; a slow environment must not hold the whole tab, its Refresh and Read again.
 */
const WAITING_READ_LIMIT_MS = 5_000;

/**
 * Whether the State filter can show a waiting start: All, or Queued or running.
 *
 * @param status - The State choice.
 * @returns True when the waiting starts are read.
 */
function showsWaiting(status: string): boolean {
  return status === "all" || status === "queued,running";
}

/**
 * Reads the jobs of the listed environments, at most four at once, and keeps the starts that
 * wait and match the filters. The Runs tab reads them with its first page, so the two are one
 * moment's answer: a start that has begun shows once, as its run, and never twice or not at all.
 * Nothing is read when the State filter shows no waiting start. A job list that cannot be read,
 * or that takes longer than {@link WAITING_READ_LIMIT_MS}, adds no row and no error here; the
 * Jobs tab says so.
 *
 * @param filter - The environments and the filters.
 * @returns The jobs whose start waits, newest request first.
 */
export async function readWaitingStarts(filter: WaitingFilter): Promise<JobOut[]> {
  if (!showsWaiting(filter.status)) {
    return [];
  }
  const answers = await readJobsOf(filter.environments, WAITING_READ_LIMIT_MS);
  // An environment whose jobs failed or came too late adds no row; the Jobs tab says so.
  const jobs = answers.flatMap((answer) => ("jobs" in answer ? answer.jobs : []));
  return waitingStarts(jobs, filter);
}

/** Properties of {@link WaitingRow}. */
export interface WaitingRowProps {
  /** The job whose start waits. */
  readonly job: JobOut;
}

/**
 * One waiting start in the Runs tab's columns: Waiting (with what it waits for in the shared
 * tooltip), the job, which opens its page, its environment, what asked for it, when, no duration
 * yet, and the run it waits for, linked, or that it starts within seconds.
 *
 * @param props - The job.
 * @param props.job - The job whose start waits.
 * @returns The row.
 */
export function WaitingRow({ job }: WaitingRowProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  return (
    <tr onClick={openFromCell}>
      <td>
        <WaitingLabel job={job} tip />
      </td>
      <td>
        <Link className="row-name" to={jobPath(job.environment, job.repository, job.name)}>
          <VisibleText text={job.name} />
        </Link>
      </td>
      <td>
        <EnvironmentLabel environment={job.environment} />
      </td>
      <td>
        {job.waiting_trigger === null ? (
          <Unknown words={t("jobs.waiting.unknown")} />
        ) : (
          t(`jobs.trigger.${job.waiting_trigger}`)
        )}
      </td>
      <td>
        {job.waiting_since === null ? (
          <Unknown words={t("jobs.waiting.unknown")} />
        ) : (
          <time dateTime={job.waiting_since}>{when(job.waiting_since)}</time>
        )}
      </td>
      <td className="number">
        <Unknown words={t("jobs.duration.notStarted")} />
      </td>
      <td>
        {job.waits_for_run_id === null ? (
          t("jobs.waiting.soon")
        ) : (
          <Link className="jobs-run-origin" to={jobRunPath(job.environment, job.waits_for_run_id)}>
            {t("jobs.waiting.forRun", { id: job.waits_for_run_id.slice(0, 8) })}
          </Link>
        )}
      </td>
      <td>
        <span className="chevron" aria-hidden="true">
          ›
        </span>
      </td>
    </tr>
  );
}
