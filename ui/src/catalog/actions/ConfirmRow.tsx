/**
 * The buttons that end a form dialog, and the status line under them.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useEffectEvent, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { PendingButton } from "../../shell/PendingButton";
import { type Attempt, AttemptStatus } from "../../sign-in/Attempt";

/** What {@link useConfirm} hands a dialog for its {@link ConfirmRow} and its Enter key. */
export interface Confirm {
  /** Whether the confirm button cannot be pressed. */
  readonly disabled: boolean;
  /** Whether a press waits for the dialog to learn whether the environment is protected. */
  readonly queued: boolean;
  /** Presses confirm: acts now, or once the wait is over; nothing while it cannot be pressed. */
  readonly press: () => void;
}

/** Options of {@link useConfirm}. */
export interface ConfirmOptions {
  /** Whether the dialog still waits to learn whether the environment is protected; no by default. */
  readonly waiting?: boolean;
  /**
   * Whether the wait ended without an answer (the check failed or was refused); no by default.
   * A press kept through such a wait is dropped, so the person decides again with what they see.
   */
  readonly failed?: boolean;
  /** Whether the action already runs, so a second press, such as Enter, does nothing. */
  readonly busy: boolean;
  /** Whether the confirm button cannot be pressed, such as for an empty required field. */
  readonly disabled: boolean;
  /** Starts the action. */
  readonly onConfirm: () => void;
}

/**
 * A dialog's confirm action. A press while the dialog still asks whether the environment is
 * protected is kept, shown as pending, and acted on once the answer is in, unless the answer
 * made the button unpressable (a protected environment needs a reason first) or no answer came
 * (the check failed or was refused): then the press is dropped and the button can be pressed
 * again, so the person decides with what they see. A press is never lost without a word.
 *
 * @param options - The wait, whether the button can be pressed, and the action.
 * @param options.waiting - Whether the dialog still waits.
 * @param options.failed - Whether the wait ended without an answer.
 * @param options.busy - Whether the action already runs.
 * @param options.disabled - Whether the confirm button cannot be pressed.
 * @param options.onConfirm - Starts the action.
 * @returns The state and the press, for the {@link ConfirmRow} and the dialog's Enter key.
 */
export function useConfirm({
  waiting = false,
  failed = false,
  busy,
  disabled,
  onConfirm,
}: ConfirmOptions): Confirm {
  const [queued, setQueued] = useState(false);
  const release = useEffectEvent(() => {
    if (!disabled && !failed) {
      onConfirm();
    }
  });
  const due = queued && !waiting;
  useEffect(() => {
    if (due) {
      release();
    }
  }, [due]);
  return {
    disabled,
    queued: queued && waiting,
    press: () => {
      if (disabled || busy) {
        return;
      }
      if (waiting) {
        setQueued(true);
      } else {
        onConfirm();
      }
    },
  };
}

/** Properties of {@link ConfirmRow}. */
export interface ConfirmRowProps {
  /** The attempt the confirm button starts; its busy state and its refusal show here. */
  readonly attempt: Attempt;
  /** The confirm button's label. */
  readonly label: string;
  /** The confirm action, from {@link useConfirm}. */
  readonly confirm: Confirm;
  /** Closes the dialog without acting. */
  readonly onCancel: () => void;
}

/**
 * Renders the confirm button, Cancel and the status under them: "Working…" while the attempt
 * runs, what a kept press waits for, and the server's sentence, word for word, when it refused.
 *
 * @param props - The attempt, the label, the confirm action and Cancel.
 * @param props.attempt - The attempt the confirm button starts.
 * @param props.label - The confirm button's label.
 * @param props.confirm - The confirm action.
 * @param props.onCancel - Closes the dialog without acting.
 * @returns The row.
 */
export function ConfirmRow({ attempt, label, confirm, onCancel }: ConfirmRowProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <div className="step-actions">
        <PendingButton
          className="button button-primary"
          pending={attempt.busy || confirm.queued}
          isDisabled={confirm.disabled}
          onPress={confirm.press}
        >
          {label}
        </PendingButton>
        <Button className="button" onPress={onCancel}>
          {t("dialog.cancel")}
        </Button>
      </div>
      <AttemptStatus
        attempt={confirm.queued ? { ...attempt, busy: true } : attempt}
        waiting={confirm.queued ? t("dialog.checkingProtection") : t("common.working")}
      />
    </>
  );
}
