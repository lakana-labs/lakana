/**
 * A job's runs, newest first, filtered by state and period through the address, a page at a
 * time; each row selects its run on the job's page.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import { listJobRuns, type RunOut } from "../api/jobs";
import { ViewList } from "../catalog/views/ViewList";
import { useLibraryPages } from "../queries/useLibraryPages";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { useWhen } from "../shell/useWhen";
import { StatusLabel } from "../status/StatusLabel";
import { formatDuration, secondsBetween } from "./format";
import "./jobStrings";
import { LIST_EVERY_MS } from "./poll";
import { runStatusKind } from "./status";

/** The states the list can be narrowed to; `all` is no narrowing. */
const STATUSES = ["all", "failed", "succeeded", "running", "queued", "cancelled"] as const;

/** A choice of the Status menu. */
type StatusChoice = (typeof STATUSES)[number];

/** The periods the list can cover, and their length in days; `all` has none. */
const PERIODS = { "24h": 1, "7d": 7, "30d": 30, all: null } as const;

/** A choice of the Period menu. */
type Period = keyof typeof PERIODS;

/** The period shown when the address names none. */
const DEFAULT_PERIOD: Period = "30d";

const DAY_MS = 86_400_000;

/**
 * Reads the filter from the address; anything it does not know is the default.
 *
 * @param params - The address's query.
 * @returns The state and the period.
 */
function readFilter(params: URLSearchParams): { status: StatusChoice; period: Period } {
  const status = STATUSES.find((choice) => choice === params.get("status")) ?? "all";
  const requested = params.get("period");
  const period = Object.keys(PERIODS).find((choice) => choice === requested) as Period | undefined;
  return { status, period: period ?? DEFAULT_PERIOD };
}

/**
 * Counts up every `everyMs` while `active` and the tab is visible: no timer runs in a hidden tab,
 * and when the tab shows again it counts at once and starts over.
 *
 * @param everyMs - The interval in milliseconds.
 * @param active - False stops the count.
 * @returns The count.
 */
function useEvery(everyMs: number, active: boolean): number {
  const [count, setCount] = useState(0);
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    const visible = (): boolean => document.visibilityState === "visible";
    function arm(): void {
      clearTimeout(timer);
      if (visible()) {
        timer = setTimeout(fire, everyMs);
      }
    }
    function fire(): void {
      setCount((previous) => previous + 1);
      arm();
    }
    const onVisibility = (): void => {
      if (visible()) {
        fire();
      } else {
        clearTimeout(timer);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    arm();
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [everyMs, active]);
  return count;
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
  /** A number that changes when the list must be read again, such as after a run changed. */
  readonly revision: number;
  /** Told the rows each time they are read. */
  readonly onRows: (runs: readonly RunOut[]) => void;
}

/**
 * Renders the panel: the heading, the Status and Period menus, and the runs with their state,
 * when each was created (the link that selects it), how long it ran, what started it and how many
 * of its tasks failed. The first page is read again every 30 s while no further page was shown.
 *
 * @param props - The job, the selected run, the revision and what to tell of the rows.
 * @param props.environment - The job's environment.
 * @param props.repository - The job's repository.
 * @param props.job - The job's name.
 * @param props.selected - The selected run's id.
 * @param props.revision - Changes when the list must be read again.
 * @param props.onRows - Told the rows.
 * @returns The panel.
 */
export function JobRuns({
  environment,
  repository,
  job,
  selected,
  revision,
  onRows,
}: JobRunsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const [params, setParams] = useSearchParams();
  const { status, period } = readFilter(params);
  const key = JSON.stringify([environment, repository, job, status, period]);
  const [moreFor, setMoreFor] = useState<string | null>(null);
  const tick = useEvery(LIST_EVERY_MS, moreFor !== key);
  const load = useCallback(
    (cursor: string | null) => {
      const days = PERIODS[period];
      return listJobRuns(
        environment,
        repository,
        job,
        {
          status: status === "all" ? null : status,
          since: days === null ? null : new Date(Date.now() - days * DAY_MS).toISOString(),
        },
        cursor,
      );
    },
    [environment, repository, job, status, period],
  );
  const pages = useLibraryPages(key, revision + tick, load);
  const rows = pages.items ?? [];
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
            choices={(Object.keys(PERIODS) as Period[]).map((id) => ({
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
        pages={{
          ...pages,
          loadMore: () => {
            setMoreFor(key);
            return pages.loadMore();
          },
        }}
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
              {run.started_at !== null &&
                run.finished_at !== null &&
                formatDuration(secondsBetween(run.started_at, run.finished_at), t)}
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
