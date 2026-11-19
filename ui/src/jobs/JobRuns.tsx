/**
 * A job's runs, newest first, filtered by state and period through the address, a page at a
 * time; each row selects its run on the job's page.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import type { RequestOptions } from "../api/client";
import { listJobRuns, type RunOut } from "../api/jobs";
import { Unknown } from "../catalog/cells";
import { ViewList } from "../catalog/views/ViewList";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { useWhen } from "../shell/useWhen";
import { StatusLabel } from "../status/StatusLabel";
import { formatDuration, secondsBetween } from "./format";
import "./jobStrings";
import { DEFAULT_PERIOD, type Period, PERIOD_CHOICES, readPeriod, sinceOf } from "./period";
import { runStatusKind } from "./status";
import { useRunPages } from "./useRunPages";

/** The states the list can be narrowed to; `all` is no narrowing. */
const STATUSES = ["all", "failed", "succeeded", "running", "queued", "cancelled"] as const;

/** A choice of the Status menu. */
type StatusChoice = (typeof STATUSES)[number];

/**
 * Reads the filter from the address; anything it does not know is the default.
 *
 * @param params - The address's query.
 * @returns The state and the period.
 */
function readFilter(params: URLSearchParams): { status: StatusChoice; period: Period } {
  const status = STATUSES.find((choice) => choice === params.get("status")) ?? "all";
  return { status, period: readPeriod(params.get("period")) };
}

/** Properties of {@link JobRuns}. */
export interface JobRunsProps {
  /** The job's environment. */
  readonly environment: string;
  /** The job's repository. */
  readonly repository: string;
  /** The job's name. */
  readonly job: string;
  /** The id of the selected run, marked in its row; null for none. */
  readonly selected: string | null;
  /** A number that changes when the first page must be read again, such as for a new run. */
  readonly revision: number;
  /** The selected run as last read, shown in its row in place of the listed one; null for none. */
  readonly current: RunOut | null;
  /** Told the rows each time they are read. */
  readonly onRows: (runs: readonly RunOut[]) => void;
}

/**
 * Renders the panel: the heading, the Status and Period menus, and the runs with their state,
 * when each was created (the link that selects it), how long it ran, what started it and how many
 * of its tasks failed. The first page is read again every 30 s while no further page was shown;
 * the selected run's row always shows the run as last read.
 *
 * @param props - The job, the selected run, the revision and what to tell of the rows.
 * @param props.environment - The job's environment.
 * @param props.repository - The job's repository.
 * @param props.job - The job's name.
 * @param props.selected - The selected run's id.
 * @param props.revision - Changes when the first page must be read again.
 * @param props.current - The selected run as last read.
 * @param props.onRows - Told the rows.
 * @returns The panel.
 */
export function JobRuns({
  environment,
  repository,
  job,
  selected,
  revision,
  current,
  onRows,
}: JobRunsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const [params, setParams] = useSearchParams();
  const { status, period } = readFilter(params);
  const key = JSON.stringify([environment, repository, job, status, period]);
  const load = useCallback(
    (cursor: string | null, options: RequestOptions) => {
      return listJobRuns(
        environment,
        repository,
        job,
        {
          status: status === "all" ? null : status,
          since: sinceOf(period),
        },
        cursor,
        options,
      );
    },
    [environment, repository, job, status, period],
  );
  const pages = useRunPages(key, load, revision);
  const rows = (pages.items ?? []).map((row) =>
    current !== null && row.id === current.id ? current : row,
  );
  useEffect(() => {
    if (pages.items !== undefined) {
      onRows(pages.items);
    }
  }, [pages.items, onRows]);

  const choose = (name: "status" | "period", value: string, fallback: string): void => {
    setParams((current) => {
      if (value === fallback) {
        current.delete(name);
      } else {
        current.set(name, value);
      }
      return current;
    });
  };
  const selectSearch = (id: string): string => {
    const next = new URLSearchParams(params);
    next.set("run", id);
    return `?${next.toString()}`;
  };

  return (
    <section className="flat-panel jobs-runs" aria-labelledby={heading}>
      <div className="jobs-panel-head">
        <h2 className="jobs-panel-heading" id={heading}>
          {t("jobs.runs.heading")}
        </h2>
        <div className="jobs-filters">
          <ChoiceMenu
            className="jobs-filter"
            label={t("jobs.runs.status")}
            choices={STATUSES.map((id) => ({ id, text: t(`jobs.runs.statuses.${id}`) }))}
            selected={status}
            onChoose={(id) => {
              choose("status", id, "all");
            }}
          />
          <ChoiceMenu
            className="jobs-filter"
            label={t("jobs.runs.period")}
            choices={PERIOD_CHOICES.map((id) => ({
              id,
              text: t(`jobs.runs.periods.${id}`),
            }))}
            selected={period}
            onChoose={(id) => {
              choose("period", id, DEFAULT_PERIOD);
            }}
          />
        </div>
      </div>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={t("jobs.runs.empty")}
        region={t("jobs.runs.region", { job })}
        rowsOpen
        scroll="jobs-runs-scroll"
        head={
          <tr>
            <th scope="col">{t("jobs.runs.columns.state")}</th>
            <th scope="col">{t("jobs.runs.columns.created")}</th>
            <th scope="col" className="number">
              {t("jobs.runs.columns.duration")}
            </th>
            <th scope="col">{t("jobs.runs.columns.trigger")}</th>
            <th scope="col">{t("jobs.runs.columns.tasks")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.open")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((run) => (
          <tr key={run.id}>
            <td>
              <StatusLabel kind={runStatusKind(run.status)} />
            </td>
            <td>
              <Link
                className="row-name"
                to={selectSearch(run.id)}
                aria-current={run.id === selected ? "true" : undefined}
              >
                <time dateTime={run.created_at}>{when(run.created_at)}</time>
              </Link>
            </td>
            <td className="number">
              {run.started_at === null || run.finished_at === null ? (
                <Unknown words={t("jobs.duration.notFinished")} />
              ) : (
                formatDuration(secondsBetween(run.started_at, run.finished_at), t)
              )}
            </td>
            <td>{t(`jobs.trigger.${run.trigger ?? "none"}`)}</td>
            <td>
              {run.failed_tasks > 0 &&
                t("jobs.runs.failedOf", { failed: run.failed_tasks, total: run.total_tasks })}
            </td>
            <td>
              <span className="chevron" aria-hidden="true">
                ›
              </span>
            </td>
          </tr>
        ))}
      </ViewList>
    </section>
  );
}
