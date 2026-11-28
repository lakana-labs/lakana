/**
 * A run: its facts, its controls and its tasks, read again every two seconds while it is queued or
 * running. The job page shows the selected run under its runs; a run's page shows it alone.
 *
 * @packageDocumentation
 */
import {
  type FocusEvent,
  type JSX,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import type { RunOut, TaskRunOut } from "../api/jobs";
import { Page } from "../shell/Page";
import { settingParam, useAddressParams } from "../shell/useAddressParams";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import { ControlNote, type ControlNoteValue } from "./ControlNote";
import { formatDuration, secondsBetween } from "./format";
import { Fact } from "./JobFacts";
import "./jobStrings";
import { jobRunPath, taskPath } from "./paths";
import { PollProblem } from "./PollProblem";
import { Reading } from "./Reading";
import { CancelRunButton, ResumeRunButton } from "./RunControls";
import { isActive, runStatusKind } from "./status";
import { TaskRow } from "./TaskRow";
import { type ListSortSpec, sortRows } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { runTitle, runTrail, useRun } from "./useRun";

/**
 * The task the run's Logs opens: the failed task that ended first, then by asset, as the server
 * orders "the first failure".
 *
 * @param run - The run.
 * @returns The task, or undefined when none failed.
 */
function firstFailed(run: RunOut): TaskRunOut | undefined {
  return run.tasks
    .filter((task) => task.status === "failed")
    .toSorted(
      (a, b) =>
        String(a.finished_at).localeCompare(String(b.finished_at)) ||
        a.asset.localeCompare(b.asset),
    )[0];
}

/** Properties of {@link RunBody}. */
interface RunBodyProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run. */
  readonly run: RunOut;
  /** The controls and the note above the tasks. */
  readonly controls: ReactNode;
}

/** The columns a run's tasks sort by, in the browser; without one, the run's own order. */
const TASK_SORT: ListSortSpec<"state" | "task" | "duration" | "rows" | "attempt"> = {
  fields: { state: "asc", task: "asc", duration: "desc", rows: "desc", attempt: "desc" },
  natural: null,
  param: "tasks",
};

/**
 * The run's facts, its controls, why it failed when no task did, and its tasks.
 *
 * @param props - The run and its controls.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.controls - The controls and the note.
 * @returns The body.
 */
function RunBody({ environment, run, controls }: RunBodyProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const active = isActive(run);
  const taskFailed = run.tasks.some((task) => task.status === "failed");
  const sorting = useListSort(TASK_SORT);
  return (
    <>
      <dl className="jobs-facts-list">
        <Fact term={t("jobs.run.state")}>
          <StatusLabel kind={runStatusKind(run.status)} />
        </Fact>
        <Fact term={t("jobs.run.trigger")}>{t(`jobs.trigger.${run.trigger ?? "none"}`)}</Fact>
        <Fact term={t("jobs.run.started")}>
          {run.started_at === null ? t("jobs.run.notStarted") : when(run.started_at)}
        </Fact>
        {run.finished_at !== null ? (
          <Fact term={t("jobs.run.ended")}>{when(run.finished_at)}</Fact>
        ) : (
          run.started_at !== null && (
            <Fact term={t("jobs.run.ended")}>
              {t("jobs.run.runningFor", {
                duration: formatDuration(secondsBetween(run.started_at), t),
              })}
            </Fact>
          )
        )}
        {run.triggered_by !== null && (
          <Fact term={t("jobs.run.triggeredBy")}>
            <VisibleText text={run.triggered_by} />
          </Fact>
        )}
        <Fact term={t("jobs.run.commit")}>
          <VisibleText text={run.commit_sha.slice(0, 7)} />
        </Fact>
        {active && (
          <Fact term={t("jobs.run.tasks")}>
            {t("jobs.run.progress", { ended: run.ended_tasks, total: run.total_tasks })}
          </Fact>
        )}
      </dl>
      {controls}
      {!taskFailed && run.error_message !== null && (
        <p className="jobs-why">{t("jobs.run.why", { message: run.error_message })}</p>
      )}
      {run.tasks.length === 0 ? (
        <p>{t("jobs.run.noTasks")}</p>
      ) : (
        <div
          className="list-scroll jobs-tasks-scroll"
          role="region"
          aria-label={t("jobs.run.tasksRegion")}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <table className="list">
            <thead>
              <tr>
                <SortHeader {...sorting.header("state")}>{t("jobs.run.columns.state")}</SortHeader>
                <SortHeader {...sorting.header("task")}>{t("jobs.run.columns.task")}</SortHeader>
                <SortHeader {...sorting.header("duration")} className="number">
                  {t("jobs.run.columns.duration")}
                </SortHeader>
                <SortHeader {...sorting.header("rows")} className="number">
                  {t("jobs.run.columns.rows")}
                </SortHeader>
                <SortHeader {...sorting.header("attempt")}>
                  {t("jobs.run.columns.attempt")}
                </SortHeader>
                {/* Why a task waits or failed is a sentence, with no order that means something. */}
                <th scope="col">{t("jobs.run.columns.why")}</th>
                <th scope="col">
                  <span className="visually-hidden">{t("list.open")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {sortRows(run.tasks, sorting.sort, {
                state: (task) => task.status,
                task: (task) => task.asset,
                duration: (task) =>
                  task.started_at === null || task.finished_at === null
                    ? null
                    : secondsBetween(task.started_at, task.finished_at),
                rows: (task) => task.rows_written,
                attempt: (task) => task.attempt,
              }).map((task) => (
                <TaskRow key={task.asset} environment={environment} run={run} task={task} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/** Properties of {@link RunDetail}. */
export interface RunDetailProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run's id. */
  readonly runId: string;
  /**
   * 1 on a run's own page, which the detail frames with its heading and breadcrumb; 2 on a job's
   * page, where the run is a panel under the runs.
   */
  readonly level: 1 | 2;
  /** Told the run when it is first read, and each time a read or a control changes its state. */
  readonly onRunChanged?: ((run: RunOut) => void) | undefined;
}

/** Properties of {@link RunView}. */
interface RunViewProps extends RunDetailProps {
  /** What a control did to this run, or null. */
  readonly note: ControlNoteValue | null;
  /** Told the new run a Resume started. */
  readonly onResumed: (run: RunOut) => void;
  /** Whether the run opens after a control that is gone now, so its heading takes the focus. */
  readonly takeFocus: boolean;
}

/** A run a control changed, and the read it changed: a newer read wins over it. */
interface Changed {
  /** The run as the control's answer has it. */
  readonly run: RunOut;
  /** The read shown when the control answered. */
  readonly over: RunOut | undefined;
}

/**
 * One run, mounted once per run: reads it now and every two seconds while it is queued or
 * running, never again once it has ended or is gone, and renders it in its frame.
 *
 * @param props - The run, its frame, its note and the handlers.
 * @param props.environment - The run's environment.
 * @param props.runId - The run's id.
 * @param props.level - 1 on a run's page, 2 on a job's page.
 * @param props.onRunChanged - Told the run as it changes.
 * @param props.note - What a control did to it.
 * @param props.onResumed - Told the new run of a Resume.
 * @param props.takeFocus - Whether its heading takes the focus.
 * @returns The run.
 */
function RunView({
  environment,
  runId,
  level,
  onRunChanged,
  note,
  onResumed,
  takeFocus,
}: RunViewProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // A run opened by Resume, whose button went with the old run: its heading takes the focus.
  useEffect(() => {
    if (takeFocus && document.activeElement === document.body) {
      headingRef.current?.focus();
    }
  }, [takeFocus]);
  // The control of this run that holds the focus. When it goes (Cancel once the cancel was asked
  // for, "Cancelling…" once the run ends), the heading takes the focus instead of the page; when
  // the person moves the focus away themselves, it is forgotten, so nothing is taken from them.
  const held = useRef<Element | null>(null);
  useEffect(() => {
    if (held.current?.isConnected === false && document.activeElement === document.body) {
      held.current = null;
      headingRef.current?.focus();
    }
  });
  const onControlsFocus = (event: FocusEvent): void => {
    held.current = event.target;
  };
  const onControlsBlur = (event: FocusEvent): void => {
    const { target, relatedTarget } = event;
    if (relatedTarget === null) {
      // Focus left for the page: a click on a blank part of it, or the control was removed. Only
      // a control still in the page was left by the person.
      setTimeout(() => {
        if (held.current === target && target.isConnected) {
          held.current = null;
        }
      });
    } else if (relatedTarget.closest('[role="dialog"]') === null) {
      held.current = null;
    }
  };
  const [changed, setChanged] = useState<Changed | null>(null);
  // A run that ended, or one that is gone, is not read again.
  const poll = useRun(environment, runId);
  const read = poll.data;
  const run = changed !== null && changed.over === read ? changed.run : read;
  const told = useRef("");
  useEffect(() => {
    const now = run === undefined ? "" : `${run.status} ${String(run.cancel_requested)}`;
    if (run !== undefined && now !== told.current) {
      told.current = now;
      onRunChanged?.(run);
    }
  }, [run, onRunChanged]);

  const title = runTitle(run, t, when);
  const logs = run === undefined ? undefined : firstFailed(run);
  const content =
    run === undefined ? (
      <>{poll.error === null && <Reading label={t("jobs.loadingRun")} />}</>
    ) : (
      <RunBody
        environment={environment}
        run={run}
        controls={
          <>
            {/* Only listens to the focus of the controls inside; the div itself takes no focus. */}
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
            <div className="jobs-run-controls" onFocus={onControlsFocus} onBlur={onControlsBlur}>
              <CancelRunButton
                environment={environment}
                run={run}
                name={run.job ?? run.repository}
                onChanged={(next) => {
                  setChanged({ run: next, over: read });
                }}
              />
              <ResumeRunButton environment={environment} run={run} onResumed={onResumed} />
              {logs !== undefined && (
                <Link className="button" to={taskPath(environment, run.id, logs.asset)}>
                  {t("jobs.run.logs")}
                </Link>
              )}
            </div>
            <ControlNote note={note} />
          </>
        }
      />
    );

  if (level === 1) {
    return (
      <Page title={title} headingRef={headingRef} trail={runTrail(environment, run, t)}>
        <PollProblem poll={poll} />
        {content}
      </Page>
    );
  }
  return (
    <section className="flat-panel jobs-run" aria-labelledby={heading}>
      <h2 className="jobs-panel-heading" id={heading} tabIndex={-1} ref={headingRef}>
        <VisibleText text={title} />
      </h2>
      <PollProblem poll={poll} />
      {content}
    </section>
  );
}

/** A note about what a control did, and the run it belongs to. */
interface RunNote {
  /** The run the note is about. */
  readonly runId: string;
  /** The note. */
  readonly note: ControlNoteValue;
}

/**
 * Reads the run, again every two seconds while it is queued or running and never once it has
 * ended, and renders it with Cancel, Resume and Logs. Resume opens the new run: on a job's page
 * as its selected run (`?run=`), else as its own page; the note that says so stays with it.
 *
 * @param props - The environment, the run, the level and what to tell of changes.
 * @param props.environment - The run's environment.
 * @param props.runId - The run's id.
 * @param props.level - 1 on a run's page, 2 on a job's page.
 * @param props.onRunChanged - Told the run as it changes.
 * @returns The run.
 */
export function RunDetail({
  environment,
  runId,
  level,
  onRunChanged,
}: RunDetailProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, update] = useAddressParams();
  const [note, setNote] = useState<RunNote | null>(null);
  return (
    <RunView
      key={runId}
      environment={environment}
      runId={runId}
      level={level}
      onRunChanged={onRunChanged}
      note={note?.runId === runId ? note.note : null}
      takeFocus={note?.runId === runId}
      onResumed={(next) => {
        setNote({ runId: next.id, note: { kind: "resumed", text: t("jobs.resume.done") } });
        if (level === 1) {
          void navigate(jobRunPath(environment, next.id));
        } else {
          update(settingParam("run", next.id));
        }
      }}
    />
  );
}
