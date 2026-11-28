/**
 * A task's attempts: where each ran, how it ended and what it measured.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { TaskAttemptOut } from "../api/jobs";
import { Unknown } from "../catalog/cells";
import { formatBytes, formatRows } from "../catalog/format";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { formatDuration, secondsBetween } from "./format";
import "./jobStrings";
import { type ListSortSpec, sortRows } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { failureCode } from "./status";

/** The outcomes that have words of their own; another shows as the server sent it. */
const OUTCOMES = ["succeeded", "failed", "timed_out", "lost", "released", "cancelled"];

/** The columns, in order, and whether each holds a number. */
/** A column of the attempts. */
type AttemptField =
  | "attempt"
  | "worker"
  | "started"
  | "duration"
  | "outcome"
  | "rows"
  | "failure"
  | "memory"
  | "cpu"
  | "read";

/** The columns the attempts sort by, in the browser: each its own direction first. */
const ATTEMPT_SORT: ListSortSpec<AttemptField> = {
  fields: {
    attempt: "asc",
    worker: "asc",
    started: "desc",
    duration: "desc",
    outcome: "asc",
    rows: "desc",
    failure: "asc",
    memory: "desc",
    cpu: "desc",
    read: "desc",
  },
  natural: null,
  param: "attempts",
};

const COLUMNS: readonly (readonly [AttemptField, boolean])[] = [
  ["attempt", true],
  ["worker", false],
  ["started", false],
  ["duration", true],
  ["outcome", false],
  ["rows", true],
  ["failure", false],
  ["memory", true],
  ["cpu", true],
  ["read", true],
];

/** Properties of {@link Attempts}. */
export interface AttemptsProps {
  /** The task's asset, which names the table's region. */
  readonly asset: string;
  /** The attempts, as the server lists them. */
  readonly attempts: readonly TaskAttemptOut[];
}

/**
 * The attempts as a table in its own focusable scrolling region, so a narrow screen never scrolls
 * sideways: number, worker, start, duration, outcome, rows written, failure code, peak memory,
 * processor time and bytes read. Numbers are right-aligned; a figure that was not measured shows
 * the catalog's dash with the words "Not measured", never zero.
 *
 * @param props - The task's asset and its attempts.
 * @param props.asset - The task's asset.
 * @param props.attempts - The attempts.
 * @returns The table, or the sentence that there is none yet.
 */
export function Attempts({ asset, attempts }: AttemptsProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const when = useWhen();
  const sorting = useListSort(ATTEMPT_SORT);
  if (attempts.length === 0) {
    return <p>{t("jobs.attempts.none")}</p>;
  }
  const unmeasured = <Unknown words={t("jobs.attempts.notMeasured")} />;
  const bytes = (value: number | null): JSX.Element | string =>
    value === null ? unmeasured : formatBytes(value, i18n.language);
  return (
    <div
      className="list-scroll jobs-attempts-scroll"
      role="region"
      aria-label={t("jobs.attempts.region", { asset })}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list">
        <thead>
          <tr>
            {COLUMNS.map(([column, number]) => (
              <SortHeader
                key={column}
                {...sorting.header(column)}
                className={number ? "number" : undefined}
              >
                {t(`jobs.attempts.columns.${column}`)}
              </SortHeader>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortRows(attempts, sorting.sort, {
            attempt: (attempt) => attempt.attempt,
            worker: (attempt) => attempt.worker,
            started: (attempt) => attempt.started_at,
            duration: (attempt) =>
              attempt.finished_at === null
                ? null
                : secondsBetween(attempt.started_at, attempt.finished_at),
            // A running attempt has no outcome yet: it sorts last.
            outcome: (attempt) => attempt.outcome,
            rows: (attempt) => attempt.rows_written,
            failure: (attempt) => (attempt.failure === null ? null : attempt.failure.code),
            memory: (attempt) => attempt.peak_memory_bytes,
            cpu: (attempt) => attempt.cpu_seconds,
            read: (attempt) => attempt.bytes_read,
          }).map((attempt) => (
            // A released claim gives its number back, so two attempts may share it; their
            // starts tell them apart.
            <tr key={`${String(attempt.attempt)} ${attempt.started_at}`}>
              <td className="number">{attempt.attempt}</td>
              <td>
                <VisibleText text={attempt.worker} />
              </td>
              <td>{when(attempt.started_at)}</td>
              <td className="number">
                {attempt.finished_at === null ? (
                  <Unknown words={t("jobs.duration.notFinished")} />
                ) : (
                  formatDuration(secondsBetween(attempt.started_at, attempt.finished_at), t)
                )}
              </td>
              <td>
                {attempt.outcome === null ? (
                  t("jobs.attempts.outcomes.running")
                ) : OUTCOMES.includes(attempt.outcome) ? (
                  t(`jobs.attempts.outcomes.${attempt.outcome}`)
                ) : (
                  <VisibleText text={attempt.outcome} />
                )}
              </td>
              <td className="number">
                {attempt.rows_written === null
                  ? unmeasured
                  : formatRows(attempt.rows_written, i18n.language)}
              </td>
              <td>
                {attempt.failure !== null && (
                  <span className="jobs-code">{failureCode(attempt.failure.code)}</span>
                )}
              </td>
              <td className="number">{bytes(attempt.peak_memory_bytes)}</td>
              <td className="number">
                {attempt.cpu_seconds === null ? unmeasured : formatDuration(attempt.cpu_seconds, t)}
              </td>
              <td className="number">{bytes(attempt.bytes_read)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
