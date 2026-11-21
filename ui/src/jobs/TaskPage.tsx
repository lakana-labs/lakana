/**
 * A task's page: its state and why it failed, its attempts and its log, followed while it runs.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import type { RunOut, TaskRunOut } from "../api/jobs";
import { Page } from "../shell/Page";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import { Attempts } from "./Attempts";
import { FailureLine } from "./FailureLine";
import { Fact } from "./JobFacts";
import "./jobStrings";
import { LogView } from "./LogView";
import { jobRunPath } from "./paths";
import { Gone, isGone, PollProblem } from "./PollProblem";
import { Reading } from "./Reading";
import { isActive, taskStatusKind } from "./status";
import { EXPLAINED, TaskControls } from "./TaskRow";
import { runTitle, runTrail, useRun } from "./useRun";
import { useTaskLog } from "./useTaskLog";

/** Properties of {@link Panel}. */
interface PanelProps {
  /** The panel's heading. */
  readonly heading: string;
  /** What the panel holds. */
  readonly children: ReactNode;
}

/**
 * A flat panel named by its heading.
 *
 * @param props - The heading and the content.
 * @param props.heading - The heading.
 * @param props.children - The content.
 * @returns The panel.
 */
function Panel({ heading, children }: PanelProps): JSX.Element {
  const id = useId();
  return (
    <section className="flat-panel" aria-labelledby={id}>
      <h2 className="jobs-panel-heading" id={id}>
        {heading}
      </h2>
      {children}
    </section>
  );
}

/** Properties of {@link TaskFacts}. */
interface TaskFactsProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run. */
  readonly run: RunOut;
  /** The task. */
  readonly task: TaskRunOut;
}

/**
 * The task's state, its attempt, why it waits, and why it failed with the controls beside it.
 *
 * @param props - The run and the task.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.task - The task.
 * @returns The facts.
 */
function TaskFacts({ environment, run, task }: TaskFactsProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <dl className="jobs-facts-list">
        <Fact term={t("jobs.run.state")}>
          <StatusLabel kind={taskStatusKind(task.status)} />
        </Fact>
        {task.attempt > 0 && (
          <Fact term={t("jobs.taskPage.attempt")}>
            {t("jobs.taskPage.attemptOf", { n: task.attempt, max: task.max_attempts })}
          </Fact>
        )}
        {task.waiting_for !== null && (
          <Fact term={t("jobs.taskPage.waits")}>
            <VisibleText text={task.waiting_for.message} />
          </Fact>
        )}
      </dl>
      {task.failure !== null && <FailureLine failure={task.failure} />}
      {EXPLAINED.includes(task.status) && (
        <TaskControls
          environment={environment}
          run={run}
          task={task}
          withLog={false}
          named={false}
        />
      )}
    </>
  );
}

/** Properties of {@link TaskView}. */
interface TaskViewProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run's id. */
  readonly runId: string;
  /** The task's asset. */
  readonly asset: string;
}

/**
 * One task, mounted once per address: its run, read every two seconds while it is active, and its
 * log, followed until it is complete.
 *
 * @param props - The task.
 * @param props.environment - The run's environment.
 * @param props.runId - The run's id.
 * @param props.asset - The task's asset.
 * @returns The page.
 */
function TaskView({ environment, runId, asset }: TaskViewProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const poll = useRun(environment, runId);
  const log = useTaskLog(environment, runId, asset, {
    runEnded: poll.data !== undefined && !isActive(poll.data),
  });
  const run = poll.data;
  const task = run?.tasks.find((each) => each.asset === asset);
  // A run that is gone says so once, above; a task that is gone in a run that is there, here.
  let body: ReactNode = null;
  if (log.gone && !isGone(poll)) {
    body = <Gone message={log.error} />;
  } else if (!isGone(poll)) {
    body = (
      <div className="panel-stack">
        <Panel heading={t("jobs.facts.heading")}>
          {run === undefined
            ? poll.error === null && <Reading label={t("jobs.loadingTask")} />
            : task !== undefined && <TaskFacts environment={environment} run={run} task={task} />}
        </Panel>
        <Panel heading={t("jobs.taskPage.attempts")}>
          <Attempts asset={asset} attempts={log.attempts} />
        </Panel>
        <Panel heading={t("jobs.taskPage.log")}>
          <LogView environment={environment} runId={runId} asset={asset} log={log} />
        </Panel>
      </div>
    );
  }
  return (
    <Page
      title={asset}
      trail={[
        ...runTrail(environment, run, t),
        { to: jobRunPath(environment, runId), label: runTitle(run, t, when) },
      ]}
    >
      <PollProblem poll={poll} />
      {body}
    </Page>
  );
}

/**
 * Renders a task's page: Jobs › environment › job › run › task, its facts, its attempts and its
 * log. A run of no job leaves the job out and names its repository in the run's part.
 *
 * @returns The page of the task the address names.
 */
export function TaskPage(): JSX.Element {
  const { env = "", id = "", asset = "" } = useParams();
  return <TaskView key={`${env}/${id}/${asset}`} environment={env} runId={id} asset={asset} />;
}
