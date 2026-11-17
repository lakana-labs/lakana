/**
 * The question before a saved query is deleted.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { deleteSavedQuery, type SavedQueryOut } from "../api/savedQueries";
import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ActionDialog } from "../shell/ActionDialog";
import { useAttempt } from "../sign-in/Attempt";
import "./queryStrings";

/** Properties of {@link DeleteSavedDialog}. */
export interface DeleteSavedDialogProps {
  /** The saved query. */
  readonly saved: SavedQueryOut;
  /** Called once it is deleted; the caller decides where to go. */
  readonly onDone: () => void;
  /** Closes the dialog without deleting anything. */
  readonly onCancel: () => void;
}

/**
 * Renders the question "Delete the saved query {{name}}?". Delete sends one request; a refusal
 * stays in the dialog with the server's sentence. While the request is open, Escape and Cancel
 * do nothing.
 *
 * @param props - The saved query and the handlers.
 * @param props.saved - The saved query.
 * @param props.onDone - Called once it is deleted.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function DeleteSavedDialog({
  saved,
  onDone,
  onCancel,
}: DeleteSavedDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: false,
    onConfirm: () => {
      attempt.run(async () => {
        await deleteSavedQuery(saved.environment, saved.id);
        onDone();
      });
    },
  });
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">{t("queries.delete.title", { name: saved.name })}</Heading>
      <p>{t("queries.delete.text")}</p>
      <ConfirmRow
        attempt={attempt}
        label={t("queries.delete.confirm")}
        confirm={confirm}
        onCancel={close}
      />
    </ActionDialog>
  );
}
