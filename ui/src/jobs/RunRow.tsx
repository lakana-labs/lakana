/**
 * One row of the Runs tab: a run of any environment, what started it, when, how long it ran and
 * how far its tasks got.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { RunOut } from "../api/jobs";
import { useWhen } from "../shell/useWhen";
import { formatDuration, secondsBetween } from "./format";
import { jobRunPath } from "./paths";
import { RunCells } from "./RunCells";
import "./runStrings";
import { isActive } from "./status";

/**
 * The patterns a run was started with, from its `selection`; none when the server sent none.
 *
 * @param selection - The run's selection.
 * @returns The patterns, joined by a space, and the flags.
 */
function selectionOf(selection: RunOut["selection"]): {
  readonly patterns: string;
  readonly upstream: boolean;
  readonly downstream: boolean;
} {
  const patterns = Array.isArray(selection.patterns)
    ? selection.patterns.filter((pattern): pattern is string => typeof pattern === "string")
    : [];
  return {
    patterns: patterns.join(" "),
    upstream: selection.upstream === true,
    downstream: selection.downstream === true,
  };
}

/** Properties of {@link StartedBy} and {@link RunRow}. */
export interface RunRowProps {
  /** The run, as the list answers it. */
  readonly run: RunOut;
}

/**
 * What started a run: for a resume, "resume of" and the link to the run it continues; for a run
 * of no job started for a selection, its patterns with +upstream and +downstream; else the
 * trigger's word, and "By hand" for a run of no job.
 *
 * @param props - The run.
 * @param props.run - The run.
 * @returns The words.
 */
function StartedBy({ run }: RunRowProps): JSX.Element {
  const { t } = useTranslation();
  if (run.kind === "resume" && run.resumed_from_run_id !== null) {
    const id = run.resumed_from_run_id.slice(0, 8);
    return (
      <Link className="jobs-run-origin" to={jobRunPath(run.environment, run.resumed_from_run_id)}>
        {t("jobs.runsTab.resumeOf", { id })}
      </Link>
    );
  }
  // A job's own runs are selections too; only a run of no job was started for a selection by hand.
  if (run.kind === "selection" && run.job === null) {
    const selection = selectionOf(run.selection);
    const flags = [
      selection.upstream ? ` ${t("jobs.facts.upstream")}` : "",
      selection.downstream ? ` ${t("jobs.facts.downstream")}` : "",
    ].join("");
    return (
      <>
        {t("jobs.runsTab.selection", { patterns: selection.patterns })}
        {flags}
      </>
    );
  }
  return <>{t(`jobs.trigger.${run.trigger ?? "manual"}`)}</>;
}

/**
 * How far a run's tasks got: while it is active, how many ended; after, how many failed, or how
 * many there were when none failed.
 *
 * @param props - The run.
 * @param props.run - The run.
 * @returns The words.
 */
function Tasks({ run }: RunRowProps): JSX.Element {
  const { t } = useTranslation();
  if (isActive(run)) {
    return <>{t("jobs.runsTab.ofTasks", { ended: run.ended_tasks, total: run.total_tasks })}</>;
  }
  if (run.failed_tasks > 0) {
    return <>{t("jobs.runsTab.failedOf", { failed: run.failed_tasks, total: run.total_tasks })}</>;
  }
  return <>{t("jobs.runsTab.tasks", { count: run.total_tasks })}</>;
}

/**
 * Renders the row: the state in a glyph and a word, the name (its job, else its repository) as
 * the link to the run's page, the environment, what started it, when it was created, how long it
 * ran (so far, while it runs) and its tasks, then the quiet `›`.
 *
 * @param props - The run.
 * @param props.run - The run.
 * @returns The row.
 */
export function RunRow({ run }: RunRowProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  return (
    <tr>
      <RunCells run={run} />
      <td>
        <StartedBy run={run} />
      </td>
      <td>
        <time dateTime={run.created_at}>{when(run.created_at)}</time>
      </td>
      <td className="number">
        {run.started_at !== null &&
          formatDuration(secondsBetween(run.started_at, run.finished_at ?? undefined), t)}
      </td>
      <td>
        <Tasks run={run} />
      </td>
      <td>
        <span className="chevron" aria-hidden="true">
          ›
        </span>
      </td>
    </tr>
  );
}
