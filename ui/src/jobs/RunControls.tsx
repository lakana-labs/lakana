/**
 * The controls of a run: Cancel while it is active, Resume once it failed or was cancelled. The
 * run's detail shows them; a list of runs can show them in its rows.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { cancelRun, resumeRun, type RunOut, type TaskStatus } from "../api/jobs";
import { problemText } from "../sign-in/messages";
import { AskDialog, ControlButton, ControlNote } from "./ControlNote";
import "./jobStrings";
import { isActive } from "./status";

/** The task states a resume starts again. */
const RESUMED: readonly TaskStatus[] = ["failed", "upstream_failed", "cancelled"];

/** Properties of {@link CancelRunButton}. */
export interface CancelRunButtonProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run. */
  readonly run: RunOut;
  /** What the question calls the run: its job's name, or its repository's. */
  readonly name: string;
  /** Whether it stands in a row of a list: the label "Cancel", and the run's name in its own. */
  readonly inRow?: boolean;
  /** Told the run once the cancel was asked for. */
  readonly onChanged: (run: RunOut) => void;
  /**
   * Told the server's sentence when it refuses the cancel, such as for a run that has ended; the
   * question then closes. Without it the sentence stays in the question.
   */
  readonly onRefused?: (message: string) => void;
}

/**
 * Cancel, while the run is queued or running and the server lists `cancel`: it asks first and
 * says that finished tasks stay done. Once the cancel is asked for, "Cancelling…" stands in its
 * place until the run ends; after the person's own press it takes the focus, so the focus never
 * falls to the page.
 *
 * @param props - The environment, the run, its name and what to do with the changed run.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.name - What the question calls the run.
 * @param props.inRow - Whether it stands in a row of a list.
 * @param props.onChanged - Told the changed run.
 * @param props.onRefused - Told the server's sentence of a refusal.
 * @returns The button, the line, or nothing.
 */
export function CancelRunButton({
  environment,
  run,
  name,
  inRow = false,
  onChanged,
  onRefused,
}: CancelRunButtonProps): JSX.Element | null {
  const { t } = useTranslation();
  const [asking, setAsking] = useState(false);
  const pressed = useRef(false);
  const line = useRef<HTMLSpanElement>(null);
  const cancelling = run.cancel_requested && isActive(run);
  useEffect(() => {
    if (cancelling && pressed.current) {
      line.current?.focus();
    }
  }, [cancelling]);
  if (cancelling) {
    return (
      <span className="jobs-control" tabIndex={-1} ref={line}>
        <ControlNote note={{ kind: "cancelling", text: t("jobs.cancel.cancelling") }} />
      </span>
    );
  }
  if (!isActive(run) || !run.actions.includes("cancel")) {
    return null;
  }
  return (
    <>
      <ControlButton
        label={t(inRow ? "jobs.list.controls.cancel" : "jobs.cancel.button")}
        {...(inRow ? { accessibleName: t("jobs.list.controls.cancelName", { name }) } : {})}
        ask={() => {
          setAsking(true);
        }}
      />
      {asking && (
        <AskDialog
          title={t("jobs.cancel.title", { name })}
          text={t("jobs.cancel.text")}
          label={t("jobs.cancel.button")}
          act={async () => {
            let changed: RunOut;
            try {
              changed = await cancelRun(environment, run.id);
            } catch (failure: unknown) {
              if (onRefused === undefined) {
                throw failure;
              }
              onRefused(problemText(t, failure));
              return;
            }
            pressed.current = true;
            onChanged(changed);
          }}
          onClose={() => {
            setAsking(false);
          }}
        />
      )}
    </>
  );
}

/**
 * Whether a run can be resumed: it failed or was cancelled, a task of it is to be started again
 * (for a listed run, which comes without tasks: a task failed), and the server lists `resume`.
 *
 * @param run - The run.
 * @returns True when Resume is offered.
 */
function resumable(run: RunOut): boolean {
  // A listed run comes without its tasks; its count of failed tasks says the same.
  const again =
    run.tasks.length > 0
      ? run.tasks.some((task) => RESUMED.includes(task.status))
      : run.failed_tasks > 0;
  return (
    (run.status === "failed" || run.status === "cancelled") &&
    again &&
    run.actions.includes("resume")
  );
}

/** Properties of {@link ResumeRunButton}. */
export interface ResumeRunButtonProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run. */
  readonly run: RunOut;
  /** In a row of a list, what the button's own name calls the run: its job's or its repository's. */
  readonly name?: string;
  /** Told the new run, which starts the failed, upstream-failed and cancelled tasks again. */
  readonly onResumed: (run: RunOut) => void;
}

/**
 * Resume: starts the failed part of the run again at once as a new run. Not offered for a run
 * that succeeded, one with nothing to start again, or when the server does not list `resume`.
 *
 * @param props - The environment, the run and what to do with the new run.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.name - In a row of a list, what the button's name calls the run.
 * @param props.onResumed - Told the new run.
 * @returns The button, or nothing.
 */
export function ResumeRunButton({
  environment,
  run,
  name,
  onResumed,
}: ResumeRunButtonProps): JSX.Element | null {
  const { t } = useTranslation();
  if (!resumable(run)) {
    return null;
  }
  return (
    <ControlButton
      label={t("jobs.resume.button")}
      {...(name === undefined
        ? {}
        : { accessibleName: t("jobs.list.controls.resumeRunName", { name }) })}
      act={async () => {
        onResumed(await resumeRun(environment, run.id));
      }}
    />
  );
}
