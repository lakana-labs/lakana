/**
 * The buttons that end a form dialog, and the status line under them.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { PendingButton } from "../../shell/PendingButton";
import { type Attempt, AttemptStatus } from "../../sign-in/Attempt";

/** Properties of {@link ConfirmRow}. */
export interface ConfirmRowProps {
  /** The attempt the confirm button starts; its busy state and its refusal show here. */
  readonly attempt: Attempt;
  /** The confirm button's label. */
  readonly label: string;
  /** Whether the confirm button waits for something besides the attempt. */
  readonly waiting?: boolean;
  /** Whether the confirm button cannot be pressed yet. */
  readonly disabled: boolean;
  /** Starts the action. */
  readonly onConfirm: () => void;
  /** Closes the dialog without acting. */
  readonly onCancel: () => void;
}

/**
 * Renders the confirm button, Cancel and the attempt's status: "Working…" while it runs and the
 * server's sentence, word for word, when it refused.
 *
 * @param props - The attempt, the label and the handlers.
 * @param props.attempt - The attempt the confirm button starts.
 * @param props.label - The confirm button's label.
 * @param props.waiting - Whether the confirm button waits for something besides the attempt.
 * @param props.disabled - Whether the confirm button cannot be pressed yet.
 * @param props.onConfirm - Starts the action.
 * @param props.onCancel - Closes the dialog without acting.
 * @returns The row.
 */
export function ConfirmRow({
  attempt,
  label,
  waiting = false,
  disabled,
  onConfirm,
  onCancel,
}: ConfirmRowProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <div className="step-actions">
        <PendingButton
          className="button button-primary"
          pending={attempt.busy || waiting}
          isDisabled={disabled}
          onPress={onConfirm}
        >
          {label}
        </PendingButton>
        <Button className="button" onPress={onCancel}>
          {t("dialog.cancel")}
        </Button>
      </div>
      <AttemptStatus attempt={attempt} waiting={t("common.working")} />
    </>
  );
}
