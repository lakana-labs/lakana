/**
 * The question before New query clears changes that are not saved.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { ActionDialog } from "../shell/ActionDialog";
import "./queryStrings";

/** Properties of {@link NewQueryDialog}. */
export interface NewQueryDialogProps {
  /** Clears the editor and starts the new query. */
  readonly onConfirm: () => void;
  /** Closes the dialog and keeps everything as it was. */
  readonly onCancel: () => void;
}

/**
 * Renders the question "Start a new query?". Discard changes starts it; Cancel and Escape keep
 * the text, and the focus returns to where it was (New query, or the editor after the keys).
 * Nothing is sent to the server, so neither button waits.
 *
 * @param props - The handlers.
 * @param props.onConfirm - Starts the new query.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function NewQueryDialog({ onConfirm, onCancel }: NewQueryDialogProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <ActionDialog onClose={onCancel}>
      <Heading slot="title">{t("queries.new.title")}</Heading>
      <p>{t("queries.new.text")}</p>
      <div className="step-actions">
        <Button className="button button-primary" onPress={onConfirm}>
          {t("queries.new.confirm")}
        </Button>
        <Button className="button" onPress={onCancel}>
          {t("dialog.cancel")}
        </Button>
      </div>
    </ActionDialog>
  );
}
