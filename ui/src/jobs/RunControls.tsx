/**
 * The controls of a run: Cancel while it is active, Resume once it failed or was cancelled. The
 * run's detail shows them; a list of runs can show them in its rows.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { cancelRun, resumeRun, type RunOut, type TaskStatus } from "../api/jobs";
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
  /** Told the run once the cancel was asked for. */
  readonly onChanged: (run: RunOut) => void;
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
 * @param props.onChanged - Told the changed run.
 * @returns The button, the line, or nothing.
 */
export function CancelRunButton({
  environment,
  run,
  name,
  onChanged,
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
        label={t("jobs.cancel.button")}
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
            const changed = await cancelRun(environment, run.id);
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
 * Whether a run can be resumed: it failed or was cancelled, a task of it is to be started again,
 * and the server lists `resume`.
 *
 * @param run - The run.
 * @returns True when Resume is offered.
 */
function resumable(run: RunOut): boolean {
  return (
    (run.status === "failed" || run.status === "cancelled") &&
    run.tasks.some((task) => RESUMED.includes(task.status)) &&
    run.actions.includes("resume")
  );
}

/** Properties of {@link ResumeRunButton}. */
export interface ResumeRunButtonProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run. */
  readonly run: RunOut;
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
 * @param props.onResumed - Told the new run.
 * @returns The button, or nothing.
 */
export function ResumeRunButton({
  environment,
  run,
  onResumed,
}: ResumeRunButtonProps): JSX.Element | null {
  const { t } = useTranslation();
  if (!resumable(run)) {
    return null;
  }
  return (
    <ControlButton
      label={t("jobs.resume.button")}
      act={async () => {
        onResumed(await resumeRun(environment, run.id));
      }}
    />
  );
}
