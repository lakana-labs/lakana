/**
 * The question before a rollback: what it makes current again, and every job of the repository
 * it changes.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { getDeploy, listJobs, rollBack } from "../api/jobs";
import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ReadFailed } from "../catalog/ReadFailed";
import { useAfter } from "../queries/useAfter";
import { ActionDialog } from "../shell/ActionDialog";
import { EnvironmentName } from "../shell/EnvironmentPicker";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { useAttempt } from "../sign-in/Attempt";
import "./deployStrings";
import type { RollbackTarget } from "./rollbackable";
import { rollbackJobs } from "./rollbackJobs";

/** Properties of {@link RollbackDialog}. */
export interface RollbackDialogProps {
  /** The environment. */
  readonly environment: string;
  /** The deploy to roll back to: an earlier successful one, with a commit. */
  readonly deploy: RollbackTarget;
  /** Whether the environment is protected. */
  readonly protectedEnvironment: boolean;
  /** Told the id of the rollback the server accepted; the dialog then closes. */
  readonly onAccepted: (id: string) => void;
  /** Closes the dialog without rolling back. */
  readonly onCancel: () => void;
}

/** Properties of {@link Reading}. */
interface ReadingProps {
  /** What is read, in words. */
  readonly label: string;
}

/**
 * The status line of the dialog's reads: empty for a second, then what is read.
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
 * Renders the dialog. It reads the repository's current jobs and the target version's at once;
 * until both answered it says so after a second, and if either read fails it shows the server's
 * sentence with Try again: without both lists it offers no Roll back, so it never changes a job it
 * did not name. Then it says what the rollback makes current and lists the union of the two sets
 * of jobs, sorted, each marked as kept, removed by the rollback or back with it; a protected
 * environment is named so. Roll back sends one rollback to the deploy's commit and hands back the
 * id the server accepted; a refusal stays in the dialog with the server's sentence. While it is
 * sent, Escape and Cancel do nothing.
 *
 * @param props - The environment, the deploy, the protection and the handlers.
 * @param props.environment - The environment.
 * @param props.deploy - The deploy to roll back to.
 * @param props.protectedEnvironment - Whether the environment is protected.
 * @param props.onAccepted - Told the accepted rollback's id.
 * @param props.onCancel - Closes the dialog without rolling back.
 * @returns The dialog.
 */
export function RollbackDialog({
  environment,
  deploy,
  protectedEnvironment,
  onAccepted,
  onCancel,
}: RollbackDialogProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const { repository } = deploy;
  const reads = useResource(
    useCallback(
      () => Promise.all([listJobs(environment, repository), getDeploy(environment, deploy.id)]),
      [environment, repository, deploy.id],
    ),
  );
  const attempt = useAttempt();
  const commit = deploy.resolved_sha.slice(0, 7);
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: false,
    onConfirm: () => {
      attempt.run(async () => {
        const accepted = await rollBack(environment, repository, deploy.resolved_sha);
        onAccepted(accepted.id);
      });
    },
  });
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  const [currentJobs, target] = reads.data ?? [];
  const jobs =
    currentJobs === undefined || target === undefined || target.jobs === null
      ? null
      : rollbackJobs(
          currentJobs.map((job) => job.name),
          target.jobs,
        );
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">{t("jobs.deploys.dialog.title", { repository, environment })}</Heading>
      {protectedEnvironment && (
        <p className="jobs-dialog-protected">
          <EnvironmentName environment={{ name: environment, protected: true }} />{" "}
          <span>{t("jobs.deploys.dialog.protected", { environment })}</span>
        </p>
      )}
      <p>
        {t("jobs.deploys.dialog.text", {
          commit,
          when: when(deploy.created_at),
          who: deploy.submitted_by ?? t("jobs.deploys.deletedAccount"),
          repository,
          environment,
        })}
      </p>
      {reads.error !== null && <ReadFailed message={reads.error} onRetry={reads.reload} />}
      {reads.error === null && reads.data === undefined && (
        <Reading label={t("jobs.deploys.dialog.reading")} />
      )}
      {reads.error === null && reads.data !== undefined && jobs === null && (
        <p role="alert">{t("jobs.deploys.dialog.noTargetJobs")}</p>
      )}
      {reads.error === null && jobs !== null && jobs.length === 0 && (
        <p>{t("jobs.deploys.dialog.noJobs", { repository })}</p>
      )}
      {reads.error === null && jobs !== null && jobs.length > 0 && (
        <ul className="jobs-dialog-jobs" aria-label={t("jobs.deploys.dialog.jobs")}>
          {jobs.map(({ job, change }) => (
            <li key={job}>{t(`jobs.deploys.dialog.${change}`, { job })}</li>
          ))}
        </ul>
      )}
      {reads.error === null && jobs !== null ? (
        <ConfirmRow
          attempt={attempt}
          label={t("jobs.deploys.dialog.confirm")}
          confirm={confirm}
          onCancel={close}
        />
      ) : (
        <div className="step-actions">
          <Button className="button" onPress={close}>
            {t("dialog.cancel")}
          </Button>
        </div>
      )}
    </ActionDialog>
  );
}
