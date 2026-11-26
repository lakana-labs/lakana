/**
 * The question before a repository is undeployed from an environment: every job that goes, with
 * its schedule and a waiting start, what else goes with them and what stays; the repository's
 * name typed exactly.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useRef, useState } from "react";
import { Button, Form, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { type JobOut, listJobs, undeploy } from "../api/jobs";
import { TypedName, typedMatches } from "../access/TypedName";
import { ReadFailed } from "../catalog/ReadFailed";
import { useAfter } from "../queries/useAfter";
import { ActionDialog } from "../shell/ActionDialog";
import { useResource } from "../shell/useResource";
import { useAttempt } from "../sign-in/Attempt";
import "./deployStrings";

/** Properties of {@link UndeployDialog}. */
export interface UndeployDialogProps {
  /** The environment. */
  readonly environment: string;
  /** The repository to undeploy. */
  readonly repository: string;
  /** The service account its runs write as; null when none is bound. */
  readonly runAs: string | null;
  /** Whether the environment is protected. */
  readonly protectedEnvironment: boolean;
  /** Called once the server undeployed it; the dialog then closes. */
  readonly onDone: () => void;
  /** Closes the dialog without undeploying. */
  readonly onCancel: () => void;
}

/**
 * The states the server gives a job whose start is pending (`waiting` for the active run,
 * `waiting to start` once that run ended): the undeploy drops that start. `waiting_since` is no
 * sign of it, as the server leaves it null when the start's stored time cannot be read.
 */
const PENDING_START_STATES: readonly string[] = ["waiting", "waiting to start"];

/**
 * What goes with one job, in words: the job, its schedule when it has one here, and a start
 * that waits.
 *
 * @param job - The job.
 * @returns The key of its line.
 */
function jobLine(job: JobOut): string {
  const scheduled = job.cron !== null;
  const waiting = PENDING_START_STATES.includes(job.state);
  if (scheduled) {
    return waiting
      ? "jobs.deploys.undeployDialog.jobScheduledWaiting"
      : "jobs.deploys.undeployDialog.jobScheduled";
  }
  return waiting ? "jobs.deploys.undeployDialog.jobWaiting" : "jobs.deploys.undeployDialog.job";
}

/** Properties of {@link Reading}. */
interface ReadingProps {
  /** What is read, in words. */
  readonly label: string;
}

/**
 * The status line of the dialog's read: empty for a second, then what is read.
 *
 * @param props - The words.
 * @param props.label - What is read.
 * @returns The line.
 */
function Reading({ label }: ReadingProps): JSX.Element {
  const slow = useAfter();
  return <p role="status">{slow ? label : ""}</p>;
}

/**
 * Renders the dialog. It reads the repository's jobs in the environment; until they answered it
 * says so after a second, and if the read fails it shows the server's sentence with Try again
 * and offers no Undeploy, so it never removes a job it did not name. Then it lists what goes:
 * each job (with its schedule, and a start waiting for the active run), the run principal when
 * one is bound, the repository's glossary terms and its tables' health effects; and says what
 * stays, and that a bound service account must be bound again after a new deploy. Undeploy is
 * held, and says why, until the repository's name is typed exactly. A refusal (a run still
 * active, the deployment already gone) stays in the dialog in the server's words and takes the
 * focus. While the request is open, Escape and Cancel do nothing. Undeploy asks for no passkey
 * touch: it stops code and hands out nothing.
 *
 * @param props - The environment, the repository, its run principal, the protection and the
 *   handlers.
 * @param props.environment - The environment.
 * @param props.repository - The repository.
 * @param props.runAs - The service account its runs write as, or null.
 * @param props.protectedEnvironment - Whether the environment is protected.
 * @param props.onDone - Called once it is undeployed.
 * @param props.onCancel - Closes without undeploying.
 * @returns The dialog.
 */
export function UndeployDialog({
  environment,
  repository,
  runAs,
  protectedEnvironment,
  onDone,
  onCancel,
}: UndeployDialogProps): JSX.Element {
  const { t } = useTranslation();
  const jobs = useResource(
    useCallback(() => listJobs(environment, repository), [environment, repository]),
  );
  const attempt = useAttempt();
  const [typed, setTyped] = useState("");
  const alert = useRef<HTMLParagraphElement>(null);
  const matches = typedMatches(typed, repository);
  useEffect(() => {
    if (attempt.error !== null) alert.current?.focus();
  }, [attempt.error]);
  const close = (): void => {
    if (!attempt.busy) onCancel();
  };
  const listed = jobs.error === null ? jobs.data : undefined;
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (!matches || attempt.busy) return;
          attempt.run(async () => {
            await undeploy(environment, repository);
            onDone();
          });
        }}
      >
        <Heading slot="title">
          {t("jobs.deploys.undeployDialog.title", { repository, environment })}
        </Heading>
        {protectedEnvironment && (
          <p className="jobs-dialog-protected">
            <span className="env-protected" aria-hidden="true">
              ●
            </span>{" "}
            {t("jobs.deploys.dialog.protected", { environment })}
          </p>
        )}
        <p>{t("jobs.deploys.undeployDialog.text", { repository, environment })}</p>
        {jobs.error !== null && <ReadFailed message={jobs.error} onRetry={jobs.reload} />}
        {jobs.error === null && listed === undefined && (
          <Reading label={t("jobs.deploys.undeployDialog.reading")} />
        )}
        {listed !== undefined && (
          <>
            {listed.length === 0 && (
              <p>{t("jobs.deploys.undeployDialog.noJobs", { repository, environment })}</p>
            )}
            <ul className="jobs-dialog-jobs" aria-label={t("jobs.deploys.undeployDialog.gone")}>
              {listed.map((job) => (
                <li key={job.name}>{t(jobLine(job), { job: job.name })}</li>
              ))}
              {runAs !== null && (
                <li>{t("jobs.deploys.undeployDialog.runAs", { account: runAs })}</li>
              )}
              <li>{t("jobs.deploys.undeployDialog.glossary", { environment })}</li>
              <li>{t("jobs.deploys.undeployDialog.health")}</li>
            </ul>
            <p>
              {runAs === null
                ? t("jobs.deploys.undeployDialog.kept")
                : t("jobs.deploys.undeployDialog.keptRunAs", { account: runAs })}
            </p>
            <TypedName
              label={t("jobs.deploys.undeployDialog.type", { repository })}
              typed={typed}
              onTyped={setTyped}
              matches={matches}
              submit={t("jobs.deploys.undeployDialog.confirm")}
              held={t("jobs.deploys.undeployDialog.held")}
              busy={attempt.busy}
              onCancel={close}
            />
            <p className="step-status" role="status">
              {attempt.busy ? t("common.working") : ""}
            </p>
            {attempt.error !== null && (
              <p className="step-error" role="alert" tabIndex={-1} ref={alert}>
                {attempt.error}
              </p>
            )}
          </>
        )}
        {listed === undefined && (
          <div className="step-actions">
            <Button className="button" onPress={close}>
              {t("dialog.cancel")}
            </Button>
          </div>
        )}
      </Form>
    </ActionDialog>
  );
}
