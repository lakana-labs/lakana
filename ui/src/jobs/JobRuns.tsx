/**
 * A job's runs, newest first, filtered by state and period through the address, a page at a
 * time; each row selects its run on the job's page.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import type { RequestOptions } from "../api/client";
import { listJobRuns, type RunOut } from "../api/jobs";
import type { CursorPage } from "../api/manage";
import type { CursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { POLL_FAILURES_SHOWN, useLivePoll } from "../shell/useLivePoll";
import { problemText } from "../sign-in/messages";
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

/** No rows, the same array on every render. */
const NO_RUNS: readonly RunOut[] = [];

/** The first page before it was read. */
const NO_PAGE: CursorPage<RunOut> = { items: NO_RUNS, nextCursor: null };

/** The pages Show more read for one filter, after the first page they continue. */
interface More {
  /** The filter they belong to. */
  readonly key: string;
  /** The first page they continue, as it was when Show more was first pressed. */
  readonly first: CursorPage<RunOut>;
  /** Their rows. */
  readonly items: readonly RunOut[];
  /** The cursor of the next page; null after the last. */
  readonly next: string | null;
  /** Whether a page is on its way. */
  readonly busy: boolean;
  /** Why the latest page could not be read; null when it was. */
  readonly error: string | null;
  /** Whether a page arrived, so the list is longer than its first page. */
  readonly shown: boolean;
}

/**
 * Reads a job's runs: the first page as a background poll every 30 s (J2: never in a hidden tab,
 * at once when the tab shows again, waiting longer after a failure, the rows kept on screen and
 * the server's sentence only after three failures in a row), and the next pages on Show more.
 * While Show more reads, and once it read a page, the first page is not read again, so the list
 * never collapses or shifts under the person, and `revision` changes nothing; a Show more that
 * failed before any page arrived lets the poll go on. Another filter starts again from its first
 * page, and an answer for a filter no longer shown is dropped.
 *
 * @param key - The filter; a new one starts again from the first page.
 * @param load - Reads a page from a cursor, null for the first, as a background poll or not.
 * @param revision - A number that changes when the first page must be read again at once.
 * @returns The pages, in the shape the list frame reads.
 */
function useRunPages(
  key: string,
  load: (cursor: string | null, options: RequestOptions) => Promise<CursorPage<RunOut>>,
  revision: number,
): CursorPages<RunOut> {
  const { t } = useTranslation();
  const [more, setMore] = useState<More | null>(null);
  if (more !== null && more.key !== key) {
    setMore(null);
  }
  const current = useRef(key);
  useEffect(() => {
    current.current = key;
  }, [key]);
  const extra = more !== null && more.key === key ? more : null;
  const first = useCallback(
    async () => ({ key, page: await load(null, { background: true }) }),
    [key, load],
  );
  const still = extra !== null && (extra.shown || extra.busy);
  const poll = useLivePoll(first, { everyMs: LIST_EVERY_MS, active: !still });
  const { reload } = poll;
  const seen = useRef(revision);
  useEffect(() => {
    if (seen.current !== revision) {
      seen.current = revision;
      // While Show more holds the list the poll is stopped, and this reads nothing.
      reload();
    }
  }, [revision, reload]);
  const data = poll.data?.key === key ? poll.data.page : undefined;
  // What Show more goes on from: the pages it read, or the first page as it is now.
  const from =
    extra?.shown === true
      ? extra
      : { first: data ?? NO_PAGE, items: NO_RUNS, next: data?.nextCursor ?? null, shown: false };
  const { first: firstPage, items: moreItems, next } = from;
  const items = useMemo(
    () => (data === undefined ? undefined : [...firstPage.items, ...moreItems]),
    [data, firstPage, moreItems],
  );
  const quiet = poll.failures < POLL_FAILURES_SHOWN;
  const loadMore = async (): Promise<void> => {
    const base = { key, first: firstPage, items: moreItems, next, shown: from.shown };
    setMore({ ...base, busy: true, error: null });
    let after: More;
    try {
      const page = await load(next, {});
      after = {
        ...base,
        items: [...moreItems, ...page.items],
        next: page.nextCursor,
        shown: true,
        busy: false,
        error: null,
      };
    } catch (failure: unknown) {
      after = { ...base, busy: false, error: problemText(t, failure) };
    }
    // An answer for a filter no longer shown is dropped.
    if (current.current === key) {
      setMore(after);
    }
  };
  return {
    items,
    error: data === undefined ? poll.error : (extra?.error ?? (quiet ? null : poll.error)),
    busy: (data === undefined && poll.error === null) || extra?.busy === true,
    hasMore: next !== null,
    loadMore,
    reload,
  };
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
