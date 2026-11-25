/**
 * The question before one version of an artifact is deleted, as
 * `lakana artifact versions delete` asks it, and what the delete freed and kept.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  type ArtifactVersionDeletedOut,
  type ArtifactVersionOut,
  deleteVersion,
  type TableRef,
} from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import { needsPasskey } from "../../sign-in/messages";
import { ConfirmRow, useConfirm } from "../actions/ConfirmRow";
import { ProtectedReason } from "../actions/ProtectedReason";
import { useProtected } from "../actions/useProtected";
import { formatBytes } from "../format";
import "./artifactStrings";
import { deletionWords } from "./deletion";

/** Properties of {@link DeleteVersionDialog}. */
export interface DeleteVersionDialogProps {
  /** The artifact. */
  readonly place: TableRef;
  /** The version to delete, as the list shows it. */
  readonly version: ArtifactVersionOut;
  /** Called by Close once the version is deleted; the caller reads the versions again. */
  readonly onDone: () => void;
  /** Closes the dialog without deleting anything. */
  readonly onCancel: () => void;
  /**
   * Called when the server says the version is gone or deleted already (404, 409); not for the
   * step-up's 409 of an account without a passkey (`NoPasskeyError`).
   */
  readonly onGone?: () => void;
}

/**
 * Renders the question with the version's size and file count. In a protected environment it
 * asks for the reason to break the protection first. From the browser the server asks for a
 * passkey touch, which the request runs before it sends again. A refusal, the server's or a
 * failed touch, stays in the dialog with its sentence. While the request is open, Escape and
 * Cancel do nothing. Once deleted, the dialog says what was freed and kept, with the focus, and
 * Close (or Escape) ends it.
 *
 * @param props - The artifact, the version and the handlers.
 * @param props.place - The artifact.
 * @param props.version - The version to delete.
 * @param props.onDone - Called by Close once the version is deleted.
 * @param props.onCancel - Closes the dialog without deleting.
 * @param props.onGone - Called when the server says the version is gone or deleted already.
 * @returns The dialog.
 */
export function DeleteVersionDialog({
  place,
  version,
  onDone,
  onCancel,
  onGone,
}: DeleteVersionDialogProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(place.environment);
  const [reason, setReason] = useState("");
  const [deleted, setDeleted] = useState<ArtifactVersionDeletedOut | null>(null);
  const { target: doneLine, arm: armFocus } = useFocusAfter<HTMLParagraphElement>(deleted);
  const dotted = `${place.namespace}.${place.name}`;
  const needsReason = protectedEnvironment === true;
  const missing = needsReason && reason.trim() === "";
  const remove = (): void => {
    attempt.run(async () => {
      try {
        const answer = await deleteVersion(
          place,
          version.version,
          needsReason ? reason.trim() : null,
        );
        armFocus();
        setDeleted(answer);
      } catch (error: unknown) {
        // The step-up's 409 for an account without a passkey says nothing about the version.
        if (
          error instanceof ApiError &&
          (error.status === 404 || error.status === 409) &&
          !needsPasskey(error)
        ) {
          onGone?.();
        }
        throw error;
      }
    });
  };
  const confirm = useConfirm({
    busy: attempt.busy,
    waiting: protectedEnvironment === undefined,
    failed: protectedEnvironment === null,
    disabled: missing,
    onConfirm: remove,
  });
  const submit = confirm.press;
  // While the delete or its passkey touch is pending, Escape, the close button and Cancel do nothing.
  const close = (): void => {
    if (deleted !== null) {
      onDone();
    } else if (!attempt.busy) {
      onCancel();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">
        {t(deleted === null ? "table.artifact.delete.title" : "table.artifact.delete.titleDone", {
          version: String(version.version),
          artifact: markedText(dotted),
        })}
      </Heading>
      {deleted === null ? (
        <>
          <p>
            {t("table.artifact.delete.text", {
              size: formatBytes(version.size_bytes, i18n.language),
              files: t("table.artifact.delete.files", { count: version.file_count }),
            })}
          </p>
          {needsReason && (
            <ProtectedReason
              environment={place.environment}
              value={reason}
              onChange={setReason}
              onSubmit={submit}
            />
          )}
          <ConfirmRow
            attempt={attempt}
            label={t("table.artifact.delete.confirm")}
            confirm={confirm}
            onCancel={close}
          />
        </>
      ) : (
        <>
          <p ref={doneLine} tabIndex={-1}>
            {deletionWords(t, i18n.language, dotted, deleted)}
          </p>
          <div className="step-actions">
            <Button className="button button-primary" onPress={onDone}>
              {t("table.artifact.delete.close")}
            </Button>
          </div>
        </>
      )}
    </ActionDialog>
  );
}
