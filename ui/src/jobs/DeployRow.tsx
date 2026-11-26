/**
 * One row of the Deploys tab: a fact, with no page of its own, so it opens nothing; its one
 * control is Roll back to this… on an earlier version, or Undeploy… on the current one.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { DeployOut } from "../api/jobs";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusLabel } from "../status/StatusLabel";
import "./deployStrings";

/**
 * A deploy's commit as the list writes it: the first seven characters of the commit it
 * deployed, else the ref asked for; null when neither is known yet.
 *
 * @param deploy - The deploy.
 * @returns The commit, or null.
 */
function deployCommit(deploy: DeployOut): string | null {
  return deploy.resolved_sha?.slice(0, 7) ?? deploy.requested_ref;
}

/** Properties of {@link DeployRow}. */
export interface DeployRowProps {
  /** The deploy. */
  readonly deploy: DeployOut;
  /** Whether its version is the repository's current one. */
  readonly current: boolean;
  /** Opens the rollback's question; null where the rollback is not offered. */
  readonly onRollBack: (() => void) | null;
  /** Opens the undeploy's question; null (the default) where the undeploy is not offered. */
  readonly onUndeploy?: (() => void) | null;
  /**
   * The id of the note that says what still runs (a rollback, or the reads after an undeploy),
   * so the row's button waits, opens nothing and points at the note for why; null (the default)
   * while nothing runs. The button stays, so the focus it may hold stays too.
   */
  readonly heldBy?: string | null;
}

/** Properties of {@link RowButton}. */
interface RowButtonProps {
  /** Opens the button's question. */
  readonly onPress: () => void;
  /** The id of the note that holds it, or null. */
  readonly heldBy: string | null;
  /** The label. */
  readonly children: ReactNode;
}

/**
 * A row's button: held (`aria-disabled`, described by the note, pressing does nothing) while
 * {@link RowButtonProps.heldBy} names a note.
 *
 * @param props - The handler, the note and the label.
 * @param props.onPress - Opens the button's question.
 * @param props.heldBy - The id of the note that holds it, or null.
 * @param props.children - The label.
 * @returns The button.
 */
function RowButton({ onPress, heldBy, children }: RowButtonProps): JSX.Element {
  return (
    <Button
      className="button"
      {...(heldBy === null ? {} : { "aria-disabled": true, "aria-describedby": heldBy })}
      onPress={() => {
        if (heldBy === null) {
          onPress();
        }
      }}
    >
      {children}
    </Button>
  );
}

/**
 * Renders the row: the result in a glyph and a word (with the first line of a failed deploy's
 * message under it), the kind, the commit in monospace with its subject after it (cut with an
 * ellipsis when long) and "current" in words beside the newest run of the repository's current
 * version, the repository, who asked for it and when, and Roll back to
 * this… or Undeploy… where it is offered.
 *
 * @param props - The deploy, whether it is current, the openers and what holds them.
 * @param props.deploy - The deploy.
 * @param props.current - Whether it is current.
 * @param props.onRollBack - Opens the rollback's question.
 * @param props.onUndeploy - Opens the undeploy's question.
 * @param props.heldBy - The id of the note of what still runs, or null.
 * @returns The row.
 */
export function DeployRow({
  deploy,
  current,
  onRollBack,
  onUndeploy = null,
  heldBy = null,
}: DeployRowProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const commit = deployCommit(deploy);
  const firstLine = deploy.error_message?.split("\n")[0]?.trim() ?? "";
  return (
    <tr>
      <td>
        <StatusLabel kind={deploy.status} />
        {deploy.status === "failed" && (
          <p className="jobs-deploy-why">
            {firstLine === "" ? t("jobs.deploys.failedWithout") : <VisibleText text={firstLine} />}
          </p>
        )}
      </td>
      <td>{t(`jobs.deploys.kinds.${deploy.kind}`)}</td>
      <td className="jobs-commit-cell">
        {commit === null ? (
          t("jobs.deploys.noCommit")
        ) : (
          <code className="jobs-commit">
            <VisibleText text={commit} />
          </code>
        )}
        {deploy.commit_subject !== null && (
          <>
            {" · "}
            <span className="truncate">
              <VisibleText text={deploy.commit_subject} />
            </span>
          </>
        )}
        {current && <span className="jobs-current"> · {t("jobs.deploys.current")}</span>}
      </td>
      <td>
        <span className="row-name">
          <VisibleText text={deploy.repository} />
        </span>
      </td>
      <td>
        {deploy.submitted_by === null ? (
          <span className="jobs-deleted">{t("jobs.deploys.deletedAccount")}</span>
        ) : (
          <VisibleText text={deploy.submitted_by} />
        )}
      </td>
      <td>
        <time dateTime={deploy.created_at}>{when(deploy.created_at)}</time>
      </td>
      <td>
        {onRollBack !== null && (
          <RowButton onPress={onRollBack} heldBy={heldBy}>
            {t("jobs.deploys.rollBack")}{" "}
            {/* The name says which deploy, after the visible words (WCAG 2.5.3). */}
            <span className="visually-hidden">
              {t("jobs.deploys.rollBackWhat", {
                repository: deploy.repository,
                commit: commit ?? "",
              })}
            </span>
          </RowButton>
        )}
        {onUndeploy !== null && (
          <RowButton onPress={onUndeploy} heldBy={heldBy}>
            {t("jobs.deploys.undeploy")}{" "}
            <span className="visually-hidden">
              {t("jobs.deploys.undeployWhat", {
                repository: deploy.repository,
                environment: deploy.environment,
              })}
            </span>
          </RowButton>
        )}
      </td>
    </tr>
  );
}
