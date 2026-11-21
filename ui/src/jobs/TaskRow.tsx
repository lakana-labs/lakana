/**
 * One task of a run: its row in the tasks table and, for a task that failed, was skipped or was
 * cancelled, a second line with why and the controls beside it.
 *
 * @packageDocumentation
 */
import { type JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { RunOut, TaskRunOut, TaskStatus } from "../api/jobs";
import { splitDotted, tablePath } from "../catalog/paths";
import { formatRows } from "../catalog/format";
import { SourceButton } from "../catalog/table/SourceButton";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import { FailureLine } from "./FailureLine";
import { formatDuration, secondsBetween } from "./format";
import "./jobStrings";
import { taskPath } from "./paths";
import { ShowRowsButton } from "./ShowRowsButton";
import { failureCode, taskStatusKind } from "./status";

/** The states that get a second line: the task did not succeed, or never ran. */
export const EXPLAINED: readonly TaskStatus[] = ["failed", "upstream_failed", "cancelled"];

/** How many cells a row has, which the second line spans. */
const COLUMNS = 7;

/** Properties of {@link TaskRow}. */
export interface TaskRowProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run the task belongs to. */
  readonly run: RunOut;
  /** The task. */
  readonly task: TaskRunOut;
}

/** Properties of {@link TaskControls}. */
export interface TaskControlsProps extends TaskRowProps {
  /** Whether Log is offered for a failed task; false on the task's own page. True by default. */
  readonly withLog?: boolean | undefined;
  /**
   * Whether each control's accessible name ends with the task's asset, so that a list of
   * controls of several tasks tells them apart; false on the task's own page. True by default.
   */
  readonly named?: boolean | undefined;
}

/**
 * A control's words, with the asset after them for screen readers when `named`.
 *
 * @param props - The words.
 * @param props.text - What the control says.
 * @param props.asset - The task's asset.
 * @param props.named - Whether the asset is added.
 * @returns The label.
 */
function Label({
  text,
  asset,
  named,
}: {
  readonly text: string;
  readonly asset: string;
  readonly named: boolean;
}): JSX.Element {
  return (
    <>
      {text}
      {named && (
        <>
          {" "}
          <span className="visually-hidden">
            <VisibleText text={asset} name />
          </span>
        </>
      )}
    </>
  );
}

/**
 * The controls beside a failure: Log for a task that ran and failed, Show source for a task that
 * builds a table, Open table, and Show rows for each failed check.
 *
 * @param props - The run and the task.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.task - The task.
 * @param props.withLog - Whether Log is offered.
 * @param props.named - Whether the controls' names end with the asset.
 * @returns The controls.
 */
export function TaskControls({
  environment,
  run,
  task,
  withLog = true,
  named = true,
}: TaskControlsProps): JSX.Element {
  const { t } = useTranslation();
  const place = splitDotted(task.asset);
  return (
    <div className="jobs-task-controls">
      {withLog && task.status === "failed" && (
        <Link className="button" to={taskPath(environment, run.id, task.asset)}>
          <Label text={t("jobs.task.log")} asset={task.asset} named={named} />
        </Link>
      )}
      {place !== null && task.kind === "asset" && (
        <SourceButton
          place={{ environment, ...place }}
          label={<Label text={t("jobs.task.showSource")} asset={task.asset} named={named} />}
          className="button"
        />
      )}
      {place !== null && (
        <Link className="button" to={tablePath(environment, place.namespace, place.name)}>
          <Label text={t("jobs.task.openTable")} asset={task.asset} named={named} />
        </Link>
      )}
      {task.checks
        .filter((check) => check.outcome === "failed")
        .map((check) => (
          <ShowRowsButton key={check.name} environment={environment} runId={run.id} check={check} />
        ))}
    </div>
  );
}

/**
 * One task: its state, its asset as the link to its page, how long it ran, the rows it wrote, its
 * attempt when it retried or failed, and its failure code, or why a waiting task waits. A task
 * that failed, never ran because of another, or was cancelled has a second line with its failure
 * and its controls.
 *
 * @param props - The run and the task.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.task - The task.
 * @returns The row, and its second line.
 */
export function TaskRow({ environment, run, task }: TaskRowProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const retried = task.max_attempts > 1 && (task.attempt > 1 || task.status === "failed");
  return (
    <>
      <tr>
        <td>
          <StatusLabel kind={taskStatusKind(task.status)} />
        </td>
        <td>
          <Link className="row-name" to={taskPath(environment, run.id, task.asset)}>
            <VisibleText text={task.asset} breakable name />
          </Link>
        </td>
        <td className="number">
          {task.started_at !== null &&
            task.finished_at !== null &&
            formatDuration(secondsBetween(task.started_at, task.finished_at), t)}
        </td>
        <td className="number">
          {task.rows_written !== null && formatRows(task.rows_written, i18n.language)}
        </td>
        <td>{retried && t("jobs.run.attempt", { n: task.attempt, max: task.max_attempts })}</td>
        <td>
          {task.failure !== null ? (
            <span className="jobs-code">{failureCode(task.failure.code)}</span>
          ) : (
            task.waiting_for !== null && <VisibleText text={task.waiting_for.message} />
          )}
        </td>
        <td>
          <span className="chevron" aria-hidden="true">
            ›
          </span>
        </td>
      </tr>
      {EXPLAINED.includes(task.status) && (
        <tr className="jobs-task-why">
          <td colSpan={COLUMNS}>
            {task.failure !== null && <FailureLine failure={task.failure} />}
            <TaskControls environment={environment} run={run} task={task} />
          </td>
        </tr>
      )}
    </>
  );
}
