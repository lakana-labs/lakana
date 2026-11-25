/**
 * All jobs: every job of the environments the list shows, with its schedule, its last and next
 * run, Run and Pause or Resume.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { JobOut, JobStartOut } from "../api/jobs";
import { openFromCell } from "../catalog/cells";
import { HOW_TO_URL } from "../shell/docs";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import type { ListSortSpec } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import type { ControlNoteValue } from "./ControlNote";
import { RunNowButton, ScheduleButton } from "./JobControls";
import { isWaiting, WaitingLabel } from "./JobWaiting";
import "./jobStrings";
import "./listStrings";
import { ListSection, RowEnd } from "./ListSection";
import { jobPath, jobRunPath } from "./paths";
import { describeSchedule } from "./schedule";
import { runStatusKind } from "./status";
import { matchesSearch } from "./useJobsFilter";
import { useJobsOf } from "./useJobsOf";

/** The how-to that says how a job is declared and scheduled. */
const SCHEDULE_HOW_TO = `${HOW_TO_URL}run-a-job-on-a-schedule/`;

/** The columns the jobs sort by: by name unless the address says otherwise. */
const JOB_SORTS: ListSortSpec<"name" | "last_run" | "next_run"> = {
  fields: { name: "asc", last_run: "desc", next_run: "asc" },
  natural: { field: "name", order: "asc" },
};

/** Properties of {@link AllJobs}. */
export interface AllJobsProps {
  /** The environments whose jobs the list shows: the one chosen, or every listed one. */
  readonly environments: readonly string[];
  /** The words the list is searched for. */
  readonly query: string;
  /** A number that changes when the jobs must be read again at once. */
  readonly reloadKey: number;
  /** Told that a control acted, so the list is read again. */
  readonly onChanged: () => void;
  /** Told whether a read the person waits for is on its way, for the toolbar's Refresh. */
  readonly onBusy?: ((busy: boolean) => void) | undefined;
}

/** Properties of {@link Schedule}. */
interface ScheduleProps {
  /** The job. */
  readonly job: JobOut;
}

/**
 * A job's schedule in words with its zone, "paused" after it when paused (since when and by whom
 * on hover), or "by hand" for a job
 * without a schedule here.
 *
 * @param props - The job.
 * @param props.job - The job.
 * @returns The text.
 */
function Schedule({ job }: ScheduleProps): JSX.Element {
  const { t } = useTranslation();
  if (job.cron === null || job.timezone === null) {
    return <>{t("jobs.list.all.byHand")}</>;
  }
  const schedule = describeSchedule(job.cron, job.timezone, t);
  return (
    <>
      <span className={schedule.verbatim ? "jobs-verbatim" : undefined}>
        <VisibleText text={schedule.text} />
      </span>
      {job.paused && (
        <>
          {" · "}
          <PausedWord job={job} />
        </>
      )}
    </>
  );
}

/**
 * "paused", with since when and by whom in the shell's one tooltip on hover and on focus (the
 * tooltip makes it a Tab stop) and for a screen reader; the job's page says the same in its
 * facts. Without a time from the server, the word alone.
 *
 * @param props - The job.
 * @param props.job - The job, whose schedule is paused.
 * @returns The word.
 */
function PausedWord({ job }: ScheduleProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const word = t("jobs.list.all.paused");
  if (job.paused_at === null) {
    return <>{word}</>;
  }
  const sentence =
    job.paused_by === null
      ? t("jobs.list.all.pausedByDeleted", { when: when(job.paused_at) })
      : t("jobs.list.all.pausedBy", { when: when(job.paused_at), who: job.paused_by });
  return (
    <>
      <span className="jobs-paused" data-tip={sentence}>
        {word}
      </span>
      <span className="visually-hidden"> {sentence}</span>
    </>
  );
}

/**
 * Renders the section: the jobs of each environment, at most four read at once, every 30 s while
 * the tab is visible, sorted by name (or the column whose header was pressed) and narrowed by the search. Each
 * row opens the job's page from its name and shows its repository, environment, schedule, last
 * run (glyph and word, or "never") and next run ("Waiting" while a start waits, with what it
 * waits for in the shared tooltip), with Run and Pause or Resume when the server
 * lists them. An environment whose jobs cannot be read keeps one line with the server's sentence
 * and Try again; the others' rows stay.
 *
 * @param props - The environments, the search, the reload key and what to tell of a change.
 * @param props.environments - The environments whose jobs are shown.
 * @param props.query - The words searched for.
 * @param props.reloadKey - Changes when the jobs must be read again.
 * @param props.onChanged - Told that a control acted.
 * @param props.onBusy - Told whether a read the person waits for is on its way.
 * @returns The section.
 */
export function AllJobs({
  environments,
  query,
  reloadKey,
  onChanged,
  onBusy,
}: AllJobsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const sorting = useListSort(JOB_SORTS);
  const { rows, failed, poll } = useJobsOf(environments, reloadKey, sorting.choice);
  const { busy } = poll;
  useEffect(() => {
    onBusy?.(busy);
  }, [onBusy, busy]);
  const [note, setNote] = useState<ControlNoteValue | null>(null);
  // A job as the server answered a pause or a resume, until the jobs are read again.
  const [changed, setChanged] = useState<{
    readonly job: JobOut;
    readonly over: unknown;
  } | null>(null);
  const same = (a: JobOut, b: JobOut): boolean =>
    a.name === b.name && a.repository === b.repository && a.environment === b.environment;
  const shown = rows
    .map((job) =>
      changed !== null && changed.over === poll.data && same(changed.job, job) ? changed.job : job,
    )
    .filter((job) => matchesSearch(query, job.name, job.repository));
  const onStarted = (job: JobOut, result: JobStartOut): void => {
    if (result.run !== null) {
      setNote({ kind: "started", text: t("jobs.start.started", { job: job.name }) });
    } else if (result.waiting !== null) {
      setNote({
        kind: "waits",
        text: t("jobs.start.waits", { message: result.waiting.message }),
        to: jobRunPath(job.environment, result.waiting.waits_for_run_id),
      });
    }
    onChanged();
  };
  let empty: JSX.Element;
  if (query.trim() !== "") {
    empty = <p>{t("jobs.list.noMatch")}</p>;
  } else if (environments.length === 1) {
    empty = <p>{t("jobs.list.all.emptyOne", { environment: environments[0] })}</p>;
  } else {
    empty = (
      <p>
        {t("jobs.list.all.emptyAll")}{" "}
        <a href={SCHEDULE_HOW_TO} target="_blank" rel="noopener noreferrer">
          {t("jobs.list.all.howTo")}
          <span className="visually-hidden"> {t("home.newTab")}</span>
        </a>
      </p>
    );
  }
  return (
    <ListSection
      heading={t("jobs.list.all.heading")}
      poll={poll}
      loading={t("jobs.list.all.loading")}
      rowCount={shown.length}
      empty={failed.length === environments.length && environments.length > 0 ? null : empty}
      region={t("jobs.list.all.region")}
      scroll="jobs-all-scroll"
      note={note}
      before={failed.map((failure) => (
        <div className="read-failed" key={failure.environment}>
          <p role="alert">
            {t("jobs.list.all.readFailed", {
              environment: failure.environment,
              sentence: failure.message,
            })}
          </p>
          <button type="button" className="button" onClick={failure.retry}>
            {t("table.retry")}
          </button>
        </div>
      ))}
      head={
        <>
          <SortHeader {...sorting.header("name")}>{t("jobs.list.columns.name")}</SortHeader>
          <th scope="col">{t("jobs.list.columns.repository")}</th>
          <th scope="col">{t("jobs.list.columns.environment")}</th>
          <th scope="col">{t("jobs.list.columns.schedule")}</th>
          <SortHeader {...sorting.header("last_run")}>{t("jobs.list.columns.lastRun")}</SortHeader>
          <SortHeader {...sorting.header("next_run")}>{t("jobs.list.columns.nextRun")}</SortHeader>
        </>
      }
    >
      {shown.map((job) => (
        <tr
          key={JSON.stringify([job.environment, job.repository, job.name])}
          onClick={openFromCell}
        >
          <td>
            <Link className="row-name" to={jobPath(job.environment, job.repository, job.name)}>
              <VisibleText text={job.name} />
            </Link>
          </td>
          <td>
            <VisibleText text={job.repository} />
          </td>
          <td>
            <EnvironmentLabel environment={job.environment} />
          </td>
          <td>
            <Schedule job={job} />
          </td>
          <td>
            {job.last_run_status === null || job.last_run_at === null ? (
              t("jobs.list.all.never")
            ) : (
              <span className="jobs-last-run">
                <StatusLabel kind={runStatusKind(job.last_run_status)} />{" "}
                <time dateTime={job.last_run_at}>{when(job.last_run_at)}</time>
              </span>
            )}
          </td>
          <td>
            {isWaiting(job) ? (
              <WaitingLabel job={job} tip />
            ) : (
              !job.paused &&
              job.next_run_at !== null && (
                <time dateTime={job.next_run_at}>{when(job.next_run_at)}</time>
              )
            )}
          </td>
          <RowEnd>
            <RunNowButton
              environment={job.environment}
              job={job}
              inRow
              onStarted={(result) => {
                onStarted(job, result);
              }}
            />
            <ScheduleButton
              environment={job.environment}
              job={job}
              inRow
              onChanged={(next) => {
                setChanged({ job: next, over: poll.data });
                setNote({
                  kind: next.paused ? "paused" : "resumed",
                  text: t(next.paused ? "jobs.schedule.paused" : "jobs.schedule.resumed", {
                    job: next.name,
                  }),
                });
                onChanged();
              }}
            />
          </RowEnd>
        </tr>
      ))}
    </ListSection>
  );
}
