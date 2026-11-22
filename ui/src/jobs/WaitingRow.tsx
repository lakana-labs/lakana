/**
 * The starts that wait, as rows of the Runs tab: a start of a job that waits for the run holding
 * its project is no run yet, but it is the next one, so the list shows it above the runs.
 *
 * @packageDocumentation
 */
import { type JSX, useMemo } from "react";
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
import "./runStrings";
import { useJobsOf } from "./useJobsOf";

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
  if (filter.status !== "all" && filter.status !== "queued,running") {
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
 * Reads the jobs of the listed environments every 30 s and keeps the starts that wait. A job
 * list that cannot be read adds no row and no error here: the Jobs tab says so.
 *
 * @param filter - The filters.
 * @returns The jobs whose start waits.
 */
export function useWaitingStarts(filter: WaitingFilter): readonly JobOut[] {
  const { rows } = useJobsOf(filter.environments, 0);
  const { status, trigger, job, period } = filter;
  return useMemo(
    () => waitingStarts(rows, { status, trigger, job, period }),
    [rows, status, trigger, job, period],
  );
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
      <td>{t(`jobs.trigger.${job.waiting_trigger ?? "manual"}`)}</td>
      <td>
        {job.waiting_since === null ? (
          <Unknown words={t("jobs.duration.notStarted")} />
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
