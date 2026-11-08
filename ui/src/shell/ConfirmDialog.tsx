/**
 * The confirmation before an action that cannot be undone: what it does, then do it or cancel.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { ActionDialog } from "./ActionDialog";

/** Properties of {@link ConfirmDialog}. */
export interface ConfirmDialogProps {
  /** The question, naming what the action touches. */
  readonly title: string;
  /** The consequence, in words. */
  readonly children: ReactNode;
  /** The label of the button that does it, such as "Remove the passkey". */
  readonly confirmLabel: string;
  /** Does the action; a refusal stays in the dialog with its cause. */
  readonly onConfirm: () => Promise<void>;
  /** Closes the dialog without doing anything; focus returns to the button that opened it. */
  readonly onCancel: () => void;
  /** Called once the action is done. */
  readonly onDone: () => void;
}

/**
 * Renders the dialog. Focus moves into it when it opens; Cancel and Escape send nothing.
 *
 * @param props - The question, the consequence and the handlers.
 * @param props.title - The question.
 * @param props.children - The consequence.
 * @param props.confirmLabel - The label of the button that does it.
 * @param props.onConfirm - Does the action.
 * @param props.onCancel - Closes without doing anything.
 * @param props.onDone - Called once the action is done.
 * @returns The dialog.
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  onDone,
}: ConfirmDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  return (
    <ActionDialog onClose={onCancel}>
      <Heading slot="title">{title}</Heading>
      {children}
      <div className="step-actions">
        <Button
          className="button button-primary"
          isDisabled={attempt.busy}
          onPress={() => {
            attempt.run(async () => {
              await onConfirm();
              onDone();
            });
          }}
        >
          {confirmLabel}
        </Button>
        <Button className="button" onPress={onCancel}>
          {t("dialog.cancel")}
        </Button>
      </div>
      <AttemptStatus attempt={attempt} waiting={t("common.working")} />
    </ActionDialog>
  );
}
