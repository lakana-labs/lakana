/**
 * A run: its facts, its controls and its tasks, read again every two seconds while it is queued or
 * running. The job page shows the selected run under its runs; a run's page shows it alone.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router";

import { getRun, type RunOut, type TaskRunOut } from "../api/jobs";
import { formatRows } from "../catalog/format";
import { Page } from "../shell/Page";
import { useLivePoll } from "../shell/useLivePoll";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import { ControlNote, type ControlNoteValue } from "./ControlNote";
import { formatDuration, secondsBetween } from "./format";
import { Fact } from "./JobFacts";
import "./jobStrings";
import { jobPath, jobRunPath, taskPath } from "./paths";
import { LIVE_EVERY_MS } from "./poll";
import { isGone, PollProblem } from "./PollProblem";
import { Reading } from "./Reading";
import { CancelRunButton, ResumeRunButton } from "./RunControls";
import { failureCode, isActive, runStatusKind, taskStatusKind } from "./status";

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

/** Properties of {@link TaskRow}. */
interface TaskRowProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run's id. */
  readonly runId: string;
  /** The task. */
  readonly task: TaskRunOut;
}

/**
 * One task: its state, its asset as the link to its page, how long it ran, the rows it wrote, its
 * attempt when it retried or failed, and its failure code, or why a waiting task waits.
 *
 * @param props - The task and its run.
 * @param props.environment - The run's environment.
 * @param props.runId - The run's id.
 * @param props.task - The task.
 * @returns The row.
 */
function TaskRow({ environment, runId, task }: TaskRowProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const retried = task.max_attempts > 1 && (task.attempt > 1 || task.status === "failed");
  return (
    <tr>
      <td>
        <StatusLabel kind={taskStatusKind(task.status)} />
      </td>
      <td>
        <Link className="row-name" to={taskPath(environment, runId, task.asset)}>
          <VisibleText text={task.asset} />
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
  );
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
                <th scope="col">{t("jobs.run.columns.state")}</th>
                <th scope="col">{t("jobs.run.columns.task")}</th>
                <th scope="col" className="number">
                  {t("jobs.run.columns.duration")}
                </th>
                <th scope="col" className="number">
                  {t("jobs.run.columns.rows")}
                </th>
                <th scope="col">{t("jobs.run.columns.attempt")}</th>
                <th scope="col">{t("jobs.run.columns.why")}</th>
                <th scope="col">
                  <span className="visually-hidden">{t("list.open")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {run.tasks.map((task) => (
                <TaskRow key={task.asset} environment={environment} runId={run.id} task={task} />
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
 * @returns The run.
 */
function RunView({
  environment,
  runId,
  level,
  onRunChanged,
  note,
  onResumed,
}: RunViewProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const [ended, setEnded] = useState(false);
  const [changed, setChanged] = useState<Changed | null>(null);
  const load = useCallback(() => getRun(environment, runId), [environment, runId]);
  const poll = useLivePoll(load, { everyMs: LIVE_EVERY_MS, active: !ended });
  const read = poll.data;
  const run = changed !== null && changed.over === read ? changed.run : read;
  // A run that ended, or one that is gone, is not read again.
  if (!ended && ((read !== undefined && !isActive(read)) || isGone(poll))) {
    setEnded(true);
  }
  const told = useRef("");
  useEffect(() => {
    const now = run === undefined ? "" : `${run.status} ${String(run.cancel_requested)}`;
    if (run !== undefined && now !== told.current) {
      told.current = now;
      onRunChanged?.(run);
    }
  }, [run, onRunChanged]);

  const title =
    run === undefined
      ? t("jobs.run.untitled")
      : run.job === null
        ? t("jobs.run.titleOf", { when: when(run.created_at), repository: run.repository })
        : t("jobs.run.title", { when: when(run.created_at) });
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
            <div className="jobs-run-controls">
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
      <Page
        title={title}
        trail={[
          { to: "/jobs", label: t("areas.jobs.name") },
          { to: "/jobs", label: environment },
          ...(run === undefined || run.job === null
            ? []
            : [{ to: jobPath(environment, run.repository, run.job), label: run.job }]),
        ]}
      >
        <PollProblem poll={poll} />
        {content}
      </Page>
    );
  }
  return (
    <section className="flat-panel jobs-run" aria-labelledby={heading}>
      <h2 className="jobs-panel-heading" id={heading}>
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
  const [, setParams] = useSearchParams();
  const [note, setNote] = useState<RunNote | null>(null);
  return (
    <RunView
      key={runId}
      environment={environment}
      runId={runId}
      level={level}
      onRunChanged={onRunChanged}
      note={note?.runId === runId ? note.note : null}
      onResumed={(next) => {
        setNote({ runId: next.id, note: { kind: "resumed", text: t("jobs.resume.done") } });
        if (level === 1) {
          void navigate(jobRunPath(environment, next.id));
        } else {
          setParams((current) => {
            current.set("run", next.id);
            return current;
          });
        }
      }}
    />
  );
}
